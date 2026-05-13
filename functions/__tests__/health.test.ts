/**
 * Tests for GET /api/health.
 *
 * Pins the status precedence (warmup > degraded > operational), the
 * data-coverage counters, and the warmup-estimate countdown against
 * the same baseline seed every other test uses. Per-case overrides
 * (delete scores, mark today's ingest partial, etc.) go inline.
 */

import { env } from "cloudflare:test";
import { beforeEach, describe, expect, it } from "vitest";

import { app } from "../api/health";
import type { Health } from "../types";
import { applyMigrations, seedBaseline } from "./setup";

const URL = "http://test/api/health";

async function fetchHealth(): Promise<{ status: number; body: Health }> {
  const res = await app.fetch(new Request(URL), env);
  return { status: res.status, body: (await res.json()) as Health };
}

describe("GET /api/health", () => {
  beforeEach(async () => {
    await applyMigrations();
    await seedBaseline();
  });

  it("returns operational when scores exist and today's ingest succeeded", async () => {
    const { status, body } = await fetchHealth();
    expect(status).toBe(200);
    expect(body.schema_version).toBe(1);
    expect(body.status).toBe("operational");
    expect(body.data_coverage.items_tracked).toBe(3); // 3 active items in the seed
    expect(body.data_coverage.items_with_signals).toBe(3);
    expect(body.last_run).not.toBeNull();
    expect(body.last_run?.status).toBe("success");
  });

  it("returns warmup when no scores exist", async () => {
    // Drop the seeded scores; everything else stays.
    await env.DB.prepare("DELETE FROM scores").run();
    const { body } = await fetchHealth();
    expect(body.status).toBe("warmup");
  });

  it("returns degraded when the most recent ingest run today was partial", async () => {
    // Promote yesterday's partial run into today and remove today's
    // success run, so the latest ingest run for today is `partial`.
    // (The "today" used by /api/health is the current UTC date — we
    // bend the seed by re-dating the partial run.)
    const today = new Date().toISOString().slice(0, 10);
    await env.DB.prepare("DELETE FROM runs WHERE kind = 'ingest'").run();
    await env.DB.prepare(
      `INSERT INTO runs (run_id, parent_run_id, kind, started_at, finished_at,
                         status, items_ok, items_quarantined, items_failed, notes)
       VALUES (?, ?, 'ingest', ?, ?, 'partial', 2, 0, 1, NULL)`,
    )
      .bind(
        "run-today-partial",
        "parent-x",
        `${today}T16:00:00+00:00`,
        `${today}T16:02:00+00:00`,
      )
      .run();
    const { body } = await fetchHealth();
    expect(body.status).toBe("degraded");
  });

  it("warmup_estimate is null once observations span >= WARMUP_THRESHOLD_DAYS", async () => {
    // The seed's first observation is 2026-05-11. If WARMUP_THRESHOLD_DAYS
    // (30) days have elapsed since then by the time the test runs,
    // days_remaining is null. Otherwise it's positive. We seed an
    // extra-old observation so the threshold is unambiguously crossed.
    await env.DB.prepare(
      `INSERT INTO price_history (item_id, observed_at, lowest_cents)
       VALUES (1, '2025-01-01T00:00:00+00:00', 100)`,
    ).run();
    const { body } = await fetchHealth();
    expect(body.warmup_estimate.days_remaining).toBeNull();
    expect(body.data_coverage.days_of_history).toBeGreaterThanOrEqual(30);
  });

  it("warmup_estimate counts down when observations are recent", async () => {
    // Clear price_history so no observations exist; then insert a
    // single observation dated today. days_of_history = 1, so
    // days_remaining = WARMUP_THRESHOLD_DAYS - 1 = 29.
    await env.DB.prepare("DELETE FROM price_history").run();
    const today = new Date().toISOString();
    await env.DB.prepare(
      `INSERT INTO price_history (item_id, observed_at, lowest_cents)
       VALUES (1, ?, 100)`,
    )
      .bind(today)
      .run();
    const { body } = await fetchHealth();
    expect(body.warmup_estimate.days_remaining).toBe(29);
  });

  it("last_run reflects the most recent successful run regardless of kind", async () => {
    const { body } = await fetchHealth();
    expect(body.last_run).not.toBeNull();
    // The seed's scoring run finishes after the signals/ingest/universe
    // runs, so it should be the most recent success.
    expect(body.last_run?.run_id).toBe("run-scoring-today");
    expect(body.last_run?.kind).toBe("scoring");
  });

  it("returns counters at zero when the DB is empty", async () => {
    await env.DB.prepare("DELETE FROM scores").run();
    await env.DB.prepare("DELETE FROM signals").run();
    await env.DB.prepare("DELETE FROM listing_history").run();
    await env.DB.prepare("DELETE FROM price_history").run();
    await env.DB.prepare("DELETE FROM latest_observation").run();
    await env.DB.prepare("DELETE FROM items").run();
    await env.DB.prepare("DELETE FROM runs").run();

    const { body } = await fetchHealth();
    expect(body.status).toBe("warmup");
    expect(body.data_coverage.items_tracked).toBe(0);
    expect(body.data_coverage.items_with_signals).toBe(0);
    expect(body.data_coverage.days_of_history).toBe(0);
    expect(body.data_coverage.first_observation_at).toBeNull();
    expect(body.last_run).toBeNull();
    expect(body.warmup_estimate.days_remaining).toBe(30);
  });
});
