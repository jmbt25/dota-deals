/**
 * GET /api/runs — recent pipeline runs across all kinds.
 *
 * No Python counterpart in the publish layer: this is a debug/ops view,
 * not part of the frontend's user-facing surface. Lightweight by
 * design — just the runs table, most-recent-first, with a clamped
 * limit.
 *
 * Query params:
 *   limit  optional, default 20, clamped to [1, 100].
 */

import { Hono } from "hono";

import {
  type ApiError,
  type Env,
  type RunsReport,
  type WireRunDetailed,
  isoUtc,
} from "../types";

const DEFAULT_LIMIT = 20;
const MAX_LIMIT = 100;

/** Exported so vitest can call `app.fetch(request, env)` directly. */
export const app = new Hono<{ Bindings: Env }>();

app.get("/api/runs", async (c) => {
  const limitRaw = c.req.query("limit");
  let limit = DEFAULT_LIMIT;
  if (limitRaw !== undefined) {
    if (!/^\d+$/.test(limitRaw)) {
      const body: ApiError = {
        error: "invalid_limit",
        message: `Expected non-negative integer, got ${limitRaw}`,
        status: 400,
      };
      return c.json(body, 400);
    }
    const parsed = Number.parseInt(limitRaw, 10);
    if (parsed < 1) {
      const body: ApiError = {
        error: "invalid_limit",
        message: `Limit must be >= 1, got ${parsed}`,
        status: 400,
      };
      return c.json(body, 400);
    }
    limit = Math.min(parsed, MAX_LIMIT);
  }

  const db = c.env.DB;
  const rows = (
    await db
      .prepare(
        `SELECT run_id, parent_run_id, kind, started_at, finished_at,
                status, items_ok, items_quarantined, items_failed, notes
         FROM runs
         ORDER BY started_at DESC
         LIMIT ?`,
      )
      .bind(limit)
      .all<RunRow>()
  ).results;

  const runs: WireRunDetailed[] = rows.map((r) => ({
    run_id: r.run_id,
    parent_run_id: r.parent_run_id,
    kind: r.kind,
    started_at: isoUtc(r.started_at),
    finished_at: r.finished_at !== null ? isoUtc(r.finished_at) : null,
    status: r.status,
    items_ok: r.items_ok,
    items_quarantined: r.items_quarantined,
    items_failed: r.items_failed,
    notes: r.notes,
  }));

  const body: RunsReport = {
    schema_version: 1,
    generated_at: isoUtc(new Date()),
    runs,
  };
  return c.json(body);
});

interface RunRow {
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

export const onRequest: PagesFunction<Env> = (ctx) =>
  app.fetch(ctx.request, ctx.env);
