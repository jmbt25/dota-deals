/**
 * GET /api/items/:id — per-item detail page payload.
 *
 * Returns 404 if the item doesn't exist; 400 if the id isn't a
 * non-negative integer.
 *
 * The 30-day window (DETAIL_HISTORY_DAYS) and per-signal grouping
 * match `build_item_detail` in publish/builder.py. Daily prices come
 * from grouping `price_history.lowest_cents` by date and taking the
 * Python-style integer-floor median — same arithmetic as
 * `_median_cents` in storage/repositories.py, kept in lock-step in
 * `queries.ts::medianCents`.
 */

import { Hono } from "hono";

import {
  type SignalRow,
  groupSignalsIntoSeries,
  medianCents,
} from "../../queries";
import {
  type ApiError,
  type Env,
  type ItemDetail,
  type WireListingPoint,
  type WirePricePoint,
  DETAIL_HISTORY_DAYS,
  centsToUsdString,
  isoDate,
  isoUtc,
} from "../../types";

const ID_RE = /^\d+$/;

/** Exported so vitest can call `app.fetch(request, env)` directly. */
export const app = new Hono<{ Bindings: Env }>();

app.get("/api/items/:id", async (c) => {
  const idRaw = c.req.param("id");
  if (!ID_RE.test(idRaw)) {
    const body: ApiError = {
      error: "invalid_id",
      message: `Expected non-negative integer item id, got ${idRaw}`,
      status: 400,
    };
    return c.json(body, 400);
  }
  const itemId = Number.parseInt(idRaw, 10);

  const db = c.env.DB;
  const item = await db
    .prepare(
      `SELECT item_id, market_hash, name, category, hero, active
       FROM items WHERE item_id = ?`,
    )
    .bind(itemId)
    .first<ItemRow>();
  if (item === null) {
    const body: ApiError = {
      error: "not_found",
      message: `No item with id ${itemId}`,
      status: 404,
    };
    return c.json(body, 404);
  }

  const now = new Date();
  const asOf = isoDate(now);
  const startDate = isoDate(new Date(now.getTime() - (DETAIL_HISTORY_DAYS - 1) * 86400000));

  const dailyPrices = await fetchDailyPrices(db, itemId, startDate, asOf);
  const listings = await fetchListings(db, itemId, startDate, asOf);
  const signalRows = await fetchSignalRows(db, itemId, startDate, asOf);
  const signals = groupSignalsIntoSeries(signalRows);

  const body: ItemDetail = {
    schema_version: 1,
    generated_at: isoUtc(now),
    item_id: item.item_id,
    market_hash_name: item.market_hash,
    name: item.name,
    category: item.category,
    hero: item.hero,
    active: item.active === 1,
    daily_prices: dailyPrices,
    listings,
    signals,
  };
  return c.json(body);
});

interface ItemRow {
  item_id: number;
  market_hash: string;
  name: string;
  category: string;
  hero: string | null;
  active: number; // 0/1 in SQLite/D1
}

interface PriceObservationRow {
  utc_date: string;
  lowest_cents: number;
}

interface ListingRow {
  observed_at: string;
  listings_count: number;
}

async function fetchDailyPrices(
  db: D1Database,
  itemId: number,
  startDate: string,
  endDate: string,
): Promise<WirePricePoint[]> {
  const rows = (
    await db
      .prepare(
        `SELECT date(observed_at) AS utc_date, lowest_cents
         FROM price_history
         WHERE item_id = ? AND date(observed_at) BETWEEN ? AND ?
         ORDER BY observed_at`,
      )
      .bind(itemId, startDate, endDate)
      .all<PriceObservationRow>()
  ).results;

  // Group by date, compute per-day integer-floor median in JS (the
  // SQLite `MEDIAN` aggregate doesn't exist; the Python pipeline also
  // computes this in code — `medianCents` and `_median_cents` must
  // produce the same value for every input).
  const byDate = new Map<string, number[]>();
  for (const row of rows) {
    const arr = byDate.get(row.utc_date);
    if (arr === undefined) {
      byDate.set(row.utc_date, [row.lowest_cents]);
    } else {
      arr.push(row.lowest_cents);
    }
  }
  const sortedDates = [...byDate.keys()].sort();
  const out: WirePricePoint[] = [];
  for (const date of sortedDates) {
    const cents = medianCents(byDate.get(date) ?? []);
    if (cents !== null) {
      out.push({ date, lowest_price: centsToUsdString(cents) });
    }
  }
  return out;
}

async function fetchListings(
  db: D1Database,
  itemId: number,
  startDate: string,
  endDate: string,
): Promise<WireListingPoint[]> {
  const rows = (
    await db
      .prepare(
        `SELECT observed_at, listings_count
         FROM listing_history
         WHERE item_id = ? AND date(observed_at) BETWEEN ? AND ?
         ORDER BY observed_at`,
      )
      .bind(itemId, startDate, endDate)
      .all<ListingRow>()
  ).results;
  return rows.map((r) => ({
    observed_at: isoUtc(r.observed_at),
    listings_count: r.listings_count,
  }));
}

async function fetchSignalRows(
  db: D1Database,
  itemId: number,
  startDate: string,
  endDate: string,
): Promise<SignalRow[]> {
  const rows = (
    await db
      .prepare(
        `SELECT signal_name, computed_for, value, metadata_json
         FROM signals
         WHERE item_id = ? AND computed_for BETWEEN ? AND ?
         ORDER BY computed_for, signal_name`,
      )
      .bind(itemId, startDate, endDate)
      .all<SignalRow>()
  ).results;
  return rows;
}

export const onRequest: PagesFunction<Env> = (ctx) =>
  app.fetch(ctx.request, ctx.env);
