/**
 * GET /api/report/latest — most recent scored date's top-N buy scores.
 *
 * Warmup-aware: when no scores exist, returns the warmup envelope with
 * `status: "warmup"`, `report_date: null`, `scores: []`. Frontend uses
 * the envelope to render an empty-state UI without a separate request.
 *
 * Mirrors `build_latest_report` in src/dota_deals/publish/builder.py.
 */

import { Hono } from "hono";

import {
  buildDataQuality,
  buildScoresForDate,
  mostRecentScoreDate,
} from "../../queries";
import {
  type Env,
  type LatestReport,
  type PipelineStatus,
  isoUtc,
} from "../../types";

/** Number of top-ranked scores returned. Same default as the Python
 * builder's `top_n=20`. */
const TOP_N = 20;

/** Exported so vitest can call `app.fetch(request, env)` directly. */
export const app = new Hono<{ Bindings: Env }>();

app.get("/api/report/latest", async (c) => {
  const db = c.env.DB;
  const now = new Date();
  const scoredDate = await mostRecentScoreDate(db);

  if (scoredDate === null) {
    const body: LatestReport = {
      schema_version: 1,
      generated_at: isoUtc(now),
      report_date: null,
      status: "warmup",
      data_quality: {
        ingest_status: "missing",
        ingest_run_id: null,
        missing_items: [],
      },
      scores: [],
    };
    return c.json(body);
  }

  const scores = await buildScoresForDate(db, scoredDate, TOP_N);
  const dataQuality = await buildDataQuality(db, scoredDate);
  const status: PipelineStatus =
    dataQuality.ingest_status === "partial" ? "degraded" : "operational";

  const body: LatestReport = {
    schema_version: 1,
    generated_at: isoUtc(now),
    report_date: scoredDate,
    status,
    data_quality: dataQuality,
    scores,
  };
  return c.json(body);
});

export const onRequest: PagesFunction<Env> = (ctx) =>
  app.fetch(ctx.request, ctx.env);
