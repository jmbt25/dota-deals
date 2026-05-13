/**
 * Tests for GET /api/runs.
 *
 * Ops/debug endpoint, not part of the frontend's user-facing surface.
 * Pins ordering (most recent first), the limit clamp, and the 400
 * shape for an invalid limit param.
 */

import { env } from "cloudflare:test";
import { beforeEach, describe, expect, it } from "vitest";

import { app } from "../api/runs";
import type { ApiError, RunsReport } from "../types";
import { applyMigrations, seedBaseline } from "./setup";

async function fetchRuns(qs = ""): Promise<{ status: number; body: RunsReport | ApiError }> {
  const url = `http://test/api/runs${qs}`;
  const res = await app.fetch(new Request(url), env);
  return { status: res.status, body: await res.json() as RunsReport | ApiError };
}

describe("GET /api/runs", () => {
  beforeEach(async () => {
    await applyMigrations();
    await seedBaseline();
  });

  it("returns all seed runs ordered by started_at desc", async () => {
    const { status, body } = await fetchRuns();
    expect(status).toBe(200);
    const report = body as RunsReport;
    expect(report.schema_version).toBe(1);
    expect(report.runs).toHaveLength(5); // seed has 5 runs
    // Verify monotonic-desc ordering.
    for (let i = 0; i < report.runs.length - 1; i++) {
      const a = report.runs[i]?.started_at ?? "";
      const b = report.runs[i + 1]?.started_at ?? "";
      expect(a >= b).toBe(true);
    }
    // Newest run is one of the 2026-05-13 runs.
    expect(report.runs[0]?.started_at.startsWith("2026-05-13")).toBe(true);
  });

  it("respects the limit query parameter", async () => {
    const { body } = await fetchRuns("?limit=2");
    const report = body as RunsReport;
    expect(report.runs).toHaveLength(2);
  });

  it("clamps limit at the MAX_LIMIT (100)", async () => {
    const { body } = await fetchRuns("?limit=10000");
    const report = body as RunsReport;
    // Seed only has 5 rows; we mostly verify no error.
    expect(report.runs.length).toBeLessThanOrEqual(5);
  });

  it("returns 400 for a non-numeric limit", async () => {
    const { status, body } = await fetchRuns("?limit=abc");
    expect(status).toBe(400);
    expect((body as ApiError).error).toBe("invalid_limit");
  });

  it("returns 400 for limit=0", async () => {
    const { status, body } = await fetchRuns("?limit=0");
    expect(status).toBe(400);
    expect((body as ApiError).error).toBe("invalid_limit");
  });

  it("serializes timestamps with Z suffix", async () => {
    const { body } = await fetchRuns();
    const report = body as RunsReport;
    for (const run of report.runs) {
      expect(run.started_at.endsWith("Z")).toBe(true);
      if (run.finished_at !== null) {
        expect(run.finished_at.endsWith("Z")).toBe(true);
      }
    }
  });

  it("includes all run-level fields the operations view needs", async () => {
    const { body } = await fetchRuns("?limit=1");
    const report = body as RunsReport;
    const run = report.runs[0];
    expect(run).toBeDefined();
    expect(run).toHaveProperty("run_id");
    expect(run).toHaveProperty("parent_run_id");
    expect(run).toHaveProperty("kind");
    expect(run).toHaveProperty("items_ok");
    expect(run).toHaveProperty("items_quarantined");
    expect(run).toHaveProperty("items_failed");
    expect(run).toHaveProperty("notes");
  });
});
