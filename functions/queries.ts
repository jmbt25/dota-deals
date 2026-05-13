/**
 * Shared D1 query helpers used by more than one endpoint.
 *
 * Per-endpoint files keep their own SQL for queries they don't share —
 * the goal isn't to centralize every query, just to avoid two
 * different copies of "build a list of WireScores for a date" drifting
 * apart between `/api/report/latest` and `/api/report/:date`.
 *
 * Every function in this module is `async` and takes the D1 binding
 * directly (`D1Database`), not a wrapper. The Pages D1 binding has a
 * native promise-based API that matches what we want; the wrapper
 * pattern from `dota_deals.storage.db.D1Connection` doesn't carry over
 * because we don't need rows-read budgeting on read-only API
 * endpoints invoked once per HTTP request.
 */

import {
  ALL_SIGNAL_NAMES,
  centsToUsdString,
  isoUtc,
  type SignalName,
  type WireDataQuality,
  type WireScore,
  type WireSignalPoint,
  type WireSignalSeries,
} from "./types";

/** Row shape for SELECTs against the `scores` table. D1 returns plain
 * dicts; we use this type to make access through it well-typed. */
interface ScoreRow {
  item_id: number;
  computed_for: string;
  buy_score: number;
  components_json: string;
  explanation: string;
  data_quality_json: string | null;
}

/** Row shape for the `items` SELECT used here. */
interface ItemRow {
  item_id: number;
  market_hash: string;
  name: string;
  category: string;
  hero: string | null;
}

/** Row shape for the `latest_observation` SELECT used here. */
interface LatestObservationRow {
  item_id: number;
  lowest_cents: number;
}

/** Row shape for a signals SELECT used by the item-detail endpoint. */
export interface SignalRow {
  signal_name: string;
  computed_for: string;
  value: number | null;
  metadata_json: string | null;
}

/** Find the most recent date that has at least one score row.
 * Returns `null` if the scores table is empty (warmup case). */
export async function mostRecentScoreDate(db: D1Database): Promise<string | null> {
  const row = await db
    .prepare("SELECT MAX(computed_for) AS d FROM scores")
    .first<{ d: string | null }>();
  return row?.d ?? null;
}

/** True iff at least one row in `scores` has `computed_for = on`. */
export async function dateHasScores(db: D1Database, on: string): Promise<boolean> {
  const row = await db
    .prepare("SELECT 1 AS hit FROM scores WHERE computed_for = ? LIMIT 1")
    .bind(on)
    .first<{ hit: number }>();
  return row !== null;
}

/** Build the WireScore list for one date, top-N by buy_score desc.
 *
 * Three queries (scores → items → latest_observation) with the second
 * two batched as IN-clauses over the IDs from the first. Same shape
 * as `_build_scores_for_date` in publish/builder.py.
 */
export async function buildScoresForDate(
  db: D1Database,
  on: string,
  topN: number,
): Promise<WireScore[]> {
  const scoreRows = (
    await db
      .prepare(
        `SELECT item_id, computed_for, buy_score, components_json,
                explanation, data_quality_json
         FROM scores
         WHERE computed_for = ?
         ORDER BY buy_score DESC, item_id ASC
         LIMIT ?`,
      )
      .bind(on, topN)
      .all<ScoreRow>()
  ).results;

  if (scoreRows.length === 0) return [];

  const itemIds = scoreRows.map((r) => r.item_id);
  const itemsById = await fetchItemsById(db, itemIds);
  const pricesById = await fetchLatestPricesFor(db, itemIds);

  const out: WireScore[] = [];
  for (const s of scoreRows) {
    const item = itemsById.get(s.item_id);
    if (item === undefined) {
      // FK guarantees this shouldn't happen, but a stale FK from a manual
      // delete shouldn't crash the endpoint either.
      continue;
    }
    const components = JSON.parse(s.components_json) as Record<
      SignalName,
      number | null
    >;
    const dataQuality = s.data_quality_json
      ? (JSON.parse(s.data_quality_json) as { null_signals?: string[] })
      : {};
    const cents = pricesById.get(s.item_id);
    const currentPrice = cents !== undefined ? centsToUsdString(cents) : null;
    out.push({
      item_id: item.item_id,
      market_hash_name: item.market_hash,
      name: item.name,
      category: item.category,
      hero: item.hero,
      current_price: currentPrice,
      computed_for: s.computed_for,
      buy_score: s.buy_score,
      components: {
        price_zscore: components.price_zscore ?? null,
        supply_velocity: components.supply_velocity ?? null,
        event_proximity: components.event_proximity ?? null,
        comparables_delta: components.comparables_delta ?? null,
      },
      explanation: s.explanation,
      null_signals: Array.isArray(dataQuality.null_signals)
        ? dataQuality.null_signals
        : [],
    });
  }
  return out;
}

/** Build the `data_quality` block attached to a per-date report. */
export async function buildDataQuality(
  db: D1Database,
  on: string,
): Promise<WireDataQuality> {
  const latestIngest = await db
    .prepare(
      `SELECT run_id, status
       FROM runs
       WHERE kind = 'ingest' AND date(started_at) = ?
       ORDER BY started_at DESC LIMIT 1`,
    )
    .bind(on)
    .first<{ run_id: string; status: string }>();

  const missingRows = (
    await db
      .prepare(
        `SELECT i.market_hash AS h
         FROM items i
         WHERE i.active = 1
           AND NOT EXISTS (
             SELECT 1 FROM price_history p
             WHERE p.item_id = i.item_id AND date(p.observed_at) = ?
           )
         ORDER BY i.market_hash`,
      )
      .bind(on)
      .all<{ h: string }>()
  ).results;
  const missing = missingRows.map((r) => r.h);

  if (latestIngest === null) {
    return {
      ingest_status: "missing",
      ingest_run_id: null,
      missing_items: missing,
    };
  }
  return {
    ingest_status: latestIngest.status,
    ingest_run_id: latestIngest.run_id,
    missing_items: missing,
  };
}

/** Bulk fetch a small fixed-size set of items by id. The IN-clause
 * width is bounded by the caller — at v1 scale, this is the top-N
 * score set (≤ 20), well under any D1 parameter limit. */
async function fetchItemsById(
  db: D1Database,
  ids: readonly number[],
): Promise<Map<number, ItemRow>> {
  if (ids.length === 0) return new Map();
  const placeholders = ids.map(() => "?").join(",");
  const rows = (
    await db
      .prepare(
        `SELECT item_id, market_hash, name, category, hero
         FROM items WHERE item_id IN (${placeholders})`,
      )
      .bind(...ids)
      .all<ItemRow>()
  ).results;
  const map = new Map<number, ItemRow>();
  for (const row of rows) map.set(row.item_id, row);
  return map;
}

/** Bulk fetch latest_observation rows for the given item ids. */
async function fetchLatestPricesFor(
  db: D1Database,
  ids: readonly number[],
): Promise<Map<number, number>> {
  if (ids.length === 0) return new Map();
  const placeholders = ids.map(() => "?").join(",");
  const rows = (
    await db
      .prepare(
        `SELECT item_id, lowest_cents
         FROM latest_observation WHERE item_id IN (${placeholders})`,
      )
      .bind(...ids)
      .all<LatestObservationRow>()
  ).results;
  const map = new Map<number, number>();
  for (const row of rows) map.set(row.item_id, row.lowest_cents);
  return map;
}

/** Group raw signal rows by signal_name and emit a stable list with
 * one entry per name in the project's canonical order. Missing names
 * get an empty `points` array rather than being absent. */
export function groupSignalsIntoSeries(rows: readonly SignalRow[]): WireSignalSeries[] {
  const byName = new Map<string, WireSignalPoint[]>();
  for (const row of rows) {
    const point: WireSignalPoint = {
      date: row.computed_for,
      value: row.value,
    };
    const existing = byName.get(row.signal_name);
    if (existing === undefined) {
      byName.set(row.signal_name, [point]);
    } else {
      existing.push(point);
    }
  }
  return ALL_SIGNAL_NAMES.map((name) => ({
    signal_name: name,
    points: byName.get(name) ?? [],
  }));
}

/** Compute the Python-style integer-floor median of a sequence of
 * cents values. Matches `_median_cents` in
 * src/dota_deals/storage/repositories.py — when an aggregation
 * crosses the storage layer twice (once writing daily prices via the
 * Python pipeline, once reading them via this Worker) the medians
 * have to agree exactly, including the even-count `(a + b) // 2`
 * branch. */
export function medianCents(values: readonly number[]): number | null {
  if (values.length === 0) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const n = sorted.length;
  const mid = Math.floor(n / 2);
  if (n % 2 === 0) {
    const lo = sorted[mid - 1] as number;
    const hi = sorted[mid] as number;
    return Math.floor((lo + hi) / 2);
  }
  return sorted[mid] as number;
}

/** Pass-through datetime helper exported so endpoint files only need
 * to import from one place. (Trivially re-exporting from types.ts.) */
export { isoUtc };
