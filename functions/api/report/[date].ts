/**
 * GET /api/report/:date — frozen snapshot of a specific scored date.
 *
 * Returns 404 (structured JSON) if no scores exist for the date. This
 * is distinct from the latest endpoint's warmup behavior — the
 * historical endpoint is for "show me what happened on this day", so
 * "this day has no data" is a 404, not an empty-200.
 *
 * Mirrors `build_historical_report` in publish/builder.py. Date param
 * is validated against YYYY-MM-DD; malformed input returns 400 with
 * an error body.
 */

import { Hono } from "hono";

import { buildDataQuality, buildScoresForDate, dateHasScores } from "../../queries";
import {
  type ApiError,
  type Env,
  type HistoricalReport,
  isoUtc,
} from "../../types";

const TOP_N = 20;

/** YYYY-MM-DD — anchors on either end so partial matches are rejected. */
const DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

/** Exported so vitest can call `app.fetch(request, env)` directly. */
export const app = new Hono<{ Bindings: Env }>();

app.get("/api/report/:date", async (c) => {
  const date = c.req.param("date");

  if (!DATE_RE.test(date)) {
    const body: ApiError = {
      error: "invalid_date",
      message: `Expected YYYY-MM-DD, got ${date}`,
      status: 400,
    };
    return c.json(body, 400);
  }

  const db = c.env.DB;
  if (!(await dateHasScores(db, date))) {
    const body: ApiError = {
      error: "not_found",
      message: `No scores for date ${date}`,
      status: 404,
    };
    return c.json(body, 404);
  }

  const scores = await buildScoresForDate(db, date, TOP_N);
  const dataQuality = await buildDataQuality(db, date);

  const body: HistoricalReport = {
    schema_version: 1,
    generated_at: isoUtc(new Date()),
    report_date: date,
    data_quality: dataQuality,
    scores,
  };
  return c.json(body);
});

export const onRequest: PagesFunction<Env> = (ctx) =>
  app.fetch(ctx.request, ctx.env);
