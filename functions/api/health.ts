/**
 * GET /api/health — operational status, drives the frontend banner.
 *
 * Mirrors `build_health` in src/dota_deals/publish/builder.py. Status
 * precedence is identical:
 *
 *   1. warmup     — no scored date exists yet
 *   2. degraded   — most recent ingest run today was 'partial'
 *   3. operational — everything else
 *
 * `warmup_estimate.days_remaining` is `null` once observations span
 * ≥ WARMUP_THRESHOLD_DAYS calendar days (price_zscore's window is the
 * longest of the four signals; satisfying it means every signal is in
 * principle computable).
 */

import { Hono } from "hono";

import { mostRecentScoreDate } from "../queries";
import {
  type Env,
  type Health,
  type PipelineStatus,
  type WireDataCoverage,
  type WireRunRef,
  type WireWarmupEstimate,
  WARMUP_THRESHOLD_DAYS,
  isoUtc,
} from "../types";

/** Hono app for this endpoint. Exported so vitest tests can call
 * `app.fetch(request, env)` directly without going through Pages'
 * filesystem routing. */
export const app = new Hono<{ Bindings: Env }>();

app.get("/api/health", async (c) => {
  const db = c.env.DB;
  const now = new Date();
  const today = isoDateUtc(now);

  const coverage = await buildDataCoverage(db, now);
  const warmup = buildWarmupEstimate(coverage);

  const scoredDate = await mostRecentScoreDate(db);
  const ingestStatusToday = await ingestStatusFor(db, today);
  const status: PipelineStatus = resolveStatus(
    scoredDate !== null,
    ingestStatusToday,
  );

  const lastRun = await latestSuccessfulRun(db);

  const body: Health = {
    schema_version: 1,
    generated_at: isoUtc(now),
    status,
    last_run: lastRun,
    data_coverage: coverage,
    warmup_estimate: warmup,
  };
  return c.json(body);
});

// ---- helpers (kept local; one-call sites) -----------------------------------

interface CountRow {
  n: number;
}

interface FirstAtRow {
  first_at: string | null;
}

interface RunRow {
  run_id: string;
  kind: string;
  finished_at: string | null;
  status: string;
}

async function buildDataCoverage(db: D1Database, now: Date): Promise<WireDataCoverage> {
  const tracked = await db
    .prepare("SELECT COUNT(*) AS n FROM items WHERE active = 1")
    .first<CountRow>();
  const withSignals = await db
    .prepare("SELECT COUNT(DISTINCT item_id) AS n FROM signals")
    .first<CountRow>();
  const firstAtRow = await db
    .prepare("SELECT MIN(observed_at) AS first_at FROM price_history")
    .first<FirstAtRow>();
  const firstAtRaw = firstAtRow?.first_at ?? null;
  const firstAt = firstAtRaw !== null ? isoUtc(firstAtRaw) : null;

  let daysOfHistory = 0;
  if (firstAtRaw !== null) {
    // Compute day-difference inclusive of both endpoints, same as the
    // Python builder's `(now.date() - first_at.date()).days + 1`.
    const firstDay = new Date(firstAtRaw).getTime();
    const nowDay = new Date(isoDateUtc(now)).getTime();
    const firstDayMidnightUtc = Math.floor(firstDay / 86400000) * 86400000;
    const nowDayMidnightUtc = Math.floor(nowDay / 86400000) * 86400000;
    daysOfHistory = Math.max(
      0,
      Math.floor((nowDayMidnightUtc - firstDayMidnightUtc) / 86400000) + 1,
    );
  }

  return {
    items_tracked: tracked?.n ?? 0,
    items_with_signals: withSignals?.n ?? 0,
    days_of_history: daysOfHistory,
    first_observation_at: firstAt,
  };
}

function buildWarmupEstimate(coverage: WireDataCoverage): WireWarmupEstimate {
  if (coverage.first_observation_at === null) {
    return { days_remaining: WARMUP_THRESHOLD_DAYS };
  }
  const remaining = WARMUP_THRESHOLD_DAYS - coverage.days_of_history;
  return { days_remaining: remaining <= 0 ? null : remaining };
}

async function ingestStatusFor(db: D1Database, on: string): Promise<string> {
  const row = await db
    .prepare(
      `SELECT status FROM runs
       WHERE kind = 'ingest' AND date(started_at) = ?
       ORDER BY started_at DESC LIMIT 1`,
    )
    .bind(on)
    .first<{ status: string }>();
  return row?.status ?? "missing";
}

function resolveStatus(scoresExist: boolean, ingestStatus: string): PipelineStatus {
  if (!scoresExist) return "warmup";
  if (ingestStatus === "partial") return "degraded";
  return "operational";
}

async function latestSuccessfulRun(db: D1Database): Promise<WireRunRef | null> {
  const row = await db
    .prepare(
      `SELECT run_id, kind, finished_at, status
       FROM runs WHERE status = 'success'
       ORDER BY finished_at DESC LIMIT 1`,
    )
    .first<RunRow>();
  if (row === null) return null;
  return {
    run_id: row.run_id,
    kind: row.kind,
    finished_at: row.finished_at !== null ? isoUtc(row.finished_at) : null,
    status: row.status,
  };
}

/** YYYY-MM-DD in UTC, matching what SQLite's `date()` function returns
 * for ISO 8601 timestamps with `+00:00` / `Z`. */
function isoDateUtc(at: Date): string {
  const y = at.getUTCFullYear();
  const m = (at.getUTCMonth() + 1).toString().padStart(2, "0");
  const d = at.getUTCDate().toString().padStart(2, "0");
  return `${y}-${m}-${d}`;
}

export const onRequest: PagesFunction<Env> = (ctx) =>
  app.fetch(ctx.request, ctx.env);
