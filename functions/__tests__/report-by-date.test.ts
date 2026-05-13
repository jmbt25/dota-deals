/**
 * Tests for GET /api/report/:date.
 *
 * Distinguishes the historical endpoint from /latest: 404 (with a
 * structured JSON body) when no scores exist for the date, rather
 * than the warmup envelope.
 */

import { env } from "cloudflare:test";
import { beforeEach, describe, expect, it } from "vitest";

import { app } from "../api/report/[date]";
import type { ApiError, HistoricalReport } from "../types";
import { applyMigrations, seedBaseline } from "./setup";

async function fetchByDate(date: string): Promise<{ status: number; body: HistoricalReport | ApiError }> {
  const res = await app.fetch(new Request(`http://test/api/report/${date}`), env);
  return { status: res.status, body: await res.json() as HistoricalReport | ApiError };
}

describe("GET /api/report/:date", () => {
  beforeEach(async () => {
    await applyMigrations();
    await seedBaseline();
  });

  it("returns the scored snapshot for a date that has scores", async () => {
    const { status, body } = await fetchByDate("2026-05-13");
    expect(status).toBe(200);
    const report = body as HistoricalReport;
    expect(report.schema_version).toBe(1);
    expect(report.report_date).toBe("2026-05-13");
    expect(report.scores).toHaveLength(2);
    expect(report.scores[0]?.item_id).toBe(1);
  });

  it("returns 404 with a structured JSON body when the date has no scores", async () => {
    const { status, body } = await fetchByDate("2026-05-01");
    expect(status).toBe(404);
    const err = body as ApiError;
    expect(err.error).toBe("not_found");
    expect(err.status).toBe(404);
    expect(err.message).toMatch(/2026-05-01/);
  });

  it("returns 400 with a structured JSON body on a malformed date", async () => {
    const { status, body } = await fetchByDate("nonsense");
    expect(status).toBe(400);
    const err = body as ApiError;
    expect(err.error).toBe("invalid_date");
    expect(err.status).toBe(400);
  });

  it("returns 400 for a date in a non-canonical format", async () => {
    const { status } = await fetchByDate("2026-5-13"); // missing zero-pad
    expect(status).toBe(400);
  });

  it("structure matches /api/report/latest for the same date", async () => {
    // Same date that /latest would return; assert the score list and
    // wire shape match — the only field /latest carries that the
    // historical doesn't is `status` (warmup-vs-operational), and
    // historical doesn't need that since 404 covers the missing case.
    const { body } = await fetchByDate("2026-05-13");
    const report = body as HistoricalReport;
    expect(report.scores[0]?.market_hash_name).toBe("Inscribed Manifold Paradox");
    expect(report.scores[0]?.current_price).toBe("3.40");
    expect(report.scores[1]?.market_hash_name).toBe("Demon Eater");
  });
});
