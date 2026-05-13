/**
 * Tests for GET /api/report/latest.
 *
 * Pins the warmup envelope shape, the wire-format ordering, the
 * data_quality block surfacing partial-ingest state, and the cents →
 * USD-string conversion at the boundary.
 */

import { env } from "cloudflare:test";
import { beforeEach, describe, expect, it } from "vitest";

import { app } from "../api/report/latest";
import type { LatestReport } from "../types";
import { applyMigrations, seedBaseline } from "./setup";

const URL = "http://test/api/report/latest";

async function fetchLatest(): Promise<{ status: number; body: LatestReport }> {
  const res = await app.fetch(new Request(URL), env);
  return { status: res.status, body: (await res.json()) as LatestReport };
}

describe("GET /api/report/latest", () => {
  beforeEach(async () => {
    await applyMigrations();
    await seedBaseline();
  });

  it("returns the most recent scored date's top-N", async () => {
    const { status, body } = await fetchLatest();
    expect(status).toBe(200);
    expect(body.schema_version).toBe(1);
    expect(body.report_date).toBe("2026-05-13");
    expect(body.status).toBe("operational");
    expect(body.scores).toHaveLength(2);
    // Ordered by buy_score desc — item 1 (0.42) before item 2 (0.20).
    expect(body.scores[0]?.item_id).toBe(1);
    expect(body.scores[1]?.item_id).toBe(2);
  });

  it("returns the warmup envelope when no scores exist", async () => {
    await env.DB.prepare("DELETE FROM scores").run();
    const { body } = await fetchLatest();
    expect(body.status).toBe("warmup");
    expect(body.report_date).toBeNull();
    expect(body.scores).toEqual([]);
    expect(body.data_quality.ingest_status).toBe("missing");
  });

  it("serializes prices as USD strings, not numbers", async () => {
    // The seeded latest_observation for item 1 is 340 cents → "3.40".
    const { body } = await fetchLatest();
    const score1 = body.scores.find((s) => s.item_id === 1);
    expect(score1).toBeDefined();
    expect(score1?.current_price).toBe("3.40");
    expect(typeof score1?.current_price).toBe("string");
  });

  it("returns null current_price when the item has no latest_observation row", async () => {
    await env.DB.prepare("DELETE FROM latest_observation WHERE item_id = 1").run();
    const { body } = await fetchLatest();
    const score1 = body.scores.find((s) => s.item_id === 1);
    expect(score1?.current_price).toBeNull();
  });

  it("includes nulls in components.* (frontend renders them explicitly)", async () => {
    const { body } = await fetchLatest();
    const score2 = body.scores.find((s) => s.item_id === 2);
    expect(score2).toBeDefined();
    // From the seed: item 2's event_proximity and comparables_delta
    // were null when scored; components must preserve those nulls.
    expect(score2?.components.event_proximity).toBeNull();
    expect(score2?.components.comparables_delta).toBeNull();
    expect(score2?.components.price_zscore).toBe(0.3);
    expect(score2?.components.supply_velocity).toBe(0.25);
    expect(score2?.null_signals).toEqual(["event_proximity", "comparables_delta"]);
  });

  it("status flips to degraded when latest ingest for the scored date was partial", async () => {
    // The scored date is 2026-05-13. The seed's ingest run for that
    // date is `success`. Replace it with a `partial` run.
    await env.DB.prepare("UPDATE runs SET status = 'partial' WHERE run_id = 'run-ingest-today'").run();
    const { body } = await fetchLatest();
    expect(body.status).toBe("degraded");
    expect(body.data_quality.ingest_status).toBe("partial");
    expect(body.data_quality.ingest_run_id).toBe("run-ingest-today");
  });

  it("data_quality.missing_items lists active items with no price_history on the report date", async () => {
    // Remove item 3's price observations for the report date.
    await env.DB.prepare(
      "DELETE FROM price_history WHERE item_id = 3 AND date(observed_at) = '2026-05-13'",
    ).run();
    const { body } = await fetchLatest();
    expect(body.data_quality.missing_items).toContain("Bones of Anggelos");
    // The inactive item shouldn't surface (only active items count).
    expect(body.data_quality.missing_items).not.toContain("Discontinued Bundle");
  });
});
