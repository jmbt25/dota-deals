/**
 * Wire-format types for the dota-deals API.
 *
 * Hand-maintained 1:1 with `src/dota_deals/publish/models.py`. The
 * Worker and the (deprecated, Phase 13-removable) Python publish layer
 * MUST emit identical JSON so the frontend's wire contract is preserved
 * across the Phase 11→12 cutover.
 *
 * KNOWN MAINTENANCE BURDEN: any change to publish/models.py must be
 * mirrored here in the same commit. Phase 11 deliberately doesn't
 * codegen one from the other — adding a generator costs more than it
 * saves at v1 scale. Re-evaluate post-v2 if either side drifts.
 *
 * Format conventions (also conveyed via the type aliases below):
 *
 * - Dates: ISO YYYY-MM-DD strings.
 * - Datetimes: ISO 8601 with a trailing `Z` (UTC only).
 * - Prices: USD strings (`"12.34"`), formatted from integer cents at
 *   the wire boundary. Never floats in the wire payload.
 * - Nullable fields: `T | null`, explicitly.
 * - Envelopes: `schema_version: 1` on every top-level payload.
 */

/** Pipeline-level operational state, surfaced via the health endpoint
 * and the per-report envelope. */
export type PipelineStatus = "operational" | "degraded" | "warmup";

/** Stage kinds the runs table accepts. Mirrors the CHECK constraint
 * in migrations/0001_initial.sql. */
export type RunKind = "ingest" | "universe" | "signals" | "scoring" | "notify";

/** Run lifecycle states. Same CHECK constraint as above. */
export type RunStatus = "running" | "success" | "partial" | "failed";

/** The four signal names. Same CHECK constraint as above. */
export type SignalName =
  | "price_zscore"
  | "supply_velocity"
  | "event_proximity"
  | "comparables_delta";

/** Ingest-run data quality bucket attached to every per-date report. */
export interface WireDataQuality {
  ingest_status: string; // "success" | "partial" | "failed" | "missing"
  ingest_run_id: string | null;
  missing_items: string[];
}

/** The four signal values that fed a buy score. Nulls included so the
 * frontend can render "this signal didn't contribute". */
export interface WireScoreComponents {
  price_zscore: number | null;
  supply_velocity: number | null;
  event_proximity: number | null;
  comparables_delta: number | null;
}

/** One scored item in a report, plus the metadata the frontend renders. */
export interface WireScore {
  item_id: number;
  market_hash_name: string;
  name: string;
  category: string; // "arcana" | "immortal"
  hero: string | null;
  current_price: string | null; // USD string, or null if no observation
  computed_for: string; // YYYY-MM-DD
  buy_score: number;
  components: WireScoreComponents;
  explanation: string;
  null_signals: string[];
}

/** GET /api/report/latest payload. Warmup-state-aware: returns
 * `status: "warmup"` and `scores: []` rather than 404 when no
 * scored date exists. */
export interface LatestReport {
  schema_version: 1;
  generated_at: string; // ISO 8601 with Z
  report_date: string | null; // YYYY-MM-DD, or null in warmup
  status: PipelineStatus;
  data_quality: WireDataQuality;
  scores: WireScore[];
}

/** GET /api/report/:date payload. Always carries a date (the endpoint
 * 404s if the date has no scores). */
export interface HistoricalReport {
  schema_version: 1;
  generated_at: string;
  report_date: string;
  data_quality: WireDataQuality;
  scores: WireScore[];
}

/** Compact pointer to a row in the `runs` table. */
export interface WireRunRef {
  run_id: string;
  kind: string;
  finished_at: string | null;
  status: string;
}

/** Pipeline-level coverage snapshot. Drives the frontend banner +
 * the warmup countdown. */
export interface WireDataCoverage {
  items_tracked: number;
  items_with_signals: number;
  days_of_history: number;
  first_observation_at: string | null;
}

/** How long until the longest signal-warmup window (30 days for
 * price_zscore) is satisfied. `null` once we're past it. */
export interface WireWarmupEstimate {
  days_remaining: number | null;
}

/** GET /api/health payload. */
export interface Health {
  schema_version: 1;
  generated_at: string;
  status: PipelineStatus;
  last_run: WireRunRef | null;
  data_coverage: WireDataCoverage;
  warmup_estimate: WireWarmupEstimate;
}

/** One day's median lowest price. */
export interface WirePricePoint {
  date: string; // YYYY-MM-DD
  lowest_price: string; // USD string
}

/** One listing-count observation. Listings are point-in-time, not
 * per-day-aggregated, so we carry the full observed_at timestamp. */
export interface WireListingPoint {
  observed_at: string; // ISO 8601 with Z
  listings_count: number;
}

/** One day's value for one signal. */
export interface WireSignalPoint {
  date: string;
  value: number | null;
}

/** Per-signal series. The item-detail payload carries one of these per
 * signal name, in a fixed order. */
export interface WireSignalSeries {
  signal_name: string;
  points: WireSignalPoint[];
}

/** GET /api/items/:id payload. */
export interface ItemDetail {
  schema_version: 1;
  generated_at: string;
  item_id: number;
  market_hash_name: string;
  name: string;
  category: string;
  hero: string | null;
  active: boolean;
  daily_prices: WirePricePoint[];
  listings: WireListingPoint[];
  signals: WireSignalSeries[];
}

/** GET /api/runs payload. Phase 11-only addition — there's no Python
 * counterpart because the runs feed is a debug/ops view, not part of
 * the frontend's public surface. Kept lightweight on purpose. */
export interface RunsReport {
  schema_version: 1;
  generated_at: string;
  runs: WireRunDetailed[];
}

/** One row from the runs table, with all the operationally-useful
 * columns. Distinct from WireRunRef (which is compact, embedded in
 * Health). */
export interface WireRunDetailed {
  run_id: string;
  parent_run_id: string | null;
  kind: string;
  started_at: string;
  finished_at: string | null;
  status: string;
  items_ok: number;
  items_quarantined: number;
  items_failed: number;
  notes: string | null;
}

/** Standardized JSON error body. Returned by `_middleware.ts` for any
 * uncaught exception, and by individual handlers for expected error
 * cases (404, 400). */
export interface ApiError {
  error: string; // short, machine-parseable code (e.g. "not_found")
  message: string; // human-readable detail
  status: number; // mirrors HTTP status
}

/** Environment bindings exposed by Cloudflare Pages to every Function
 * invocation. The `DB` binding is the D1 database; declared in
 * wrangler.toml. */
export interface Env {
  DB: D1Database;
}

// ----------------------------- helpers ---------------------------------------

/** Format integer cents as a USD string with two decimals.
 *
 * Mirrors `cents_to_usd_string` in publish/models.py exactly, including
 * the negative-value rejection. Persisted prices are cents-as-INTEGER
 * throughout the system; the only place floats are allowed is here,
 * at the wire boundary, and we use integer arithmetic instead so
 * downstream comparisons don't drift on rounding.
 */
export function centsToUsdString(cents: number): string {
  if (!Number.isInteger(cents)) {
    throw new RangeError(`cents must be an integer, got ${cents}`);
  }
  if (cents < 0) {
    throw new RangeError(`cents must be >= 0, got ${cents}`);
  }
  const dollars = Math.floor(cents / 100);
  const remainder = cents % 100;
  return `${dollars}.${remainder.toString().padStart(2, "0")}`;
}

/** Format a `Date` (or D1-returned ISO string) as the wire-format
 * datetime: ISO 8601 with a `Z` suffix.
 *
 * Accepts strings as a convenience because D1 returns timestamp
 * columns as strings; this function normalizes either input to the
 * `Z`-suffixed form the Python builder emits.
 */
export function isoUtc(at: Date | string): string {
  if (typeof at === "string") {
    // D1 stores our timestamps as "YYYY-MM-DDTHH:MM:SS+00:00" (Python
    // datetime.isoformat() with a UTC offset). Convert the explicit
    // offset to the canonical Z so the wire format stays consistent
    // regardless of how the source row was inserted.
    return at.replace(/\+00:00$/, "Z");
  }
  return at.toISOString();
}

/** Format a `Date` as YYYY-MM-DD in UTC. */
export function isoDate(at: Date): string {
  const y = at.getUTCFullYear();
  const m = (at.getUTCMonth() + 1).toString().padStart(2, "0");
  const d = at.getUTCDate().toString().padStart(2, "0");
  return `${y}-${m}-${d}`;
}

/** Fixed signal name order — keeps the item-detail signals array
 * stable across responses. */
export const ALL_SIGNAL_NAMES: readonly SignalName[] = [
  "price_zscore",
  "supply_velocity",
  "event_proximity",
  "comparables_delta",
] as const;

/** Longest signal-warmup window in calendar days. Drives the
 * `WireWarmupEstimate.days_remaining` countdown. Must match
 * `_WARMUP_THRESHOLD_DAYS` in publish/builder.py. */
export const WARMUP_THRESHOLD_DAYS = 30;

/** Default history window for the item-detail endpoint, in days. */
export const DETAIL_HISTORY_DAYS = 30;
