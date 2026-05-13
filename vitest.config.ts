/**
 * Vitest configuration wired through @cloudflare/vitest-pool-workers.
 *
 * Tests run inside a real Workers runtime via miniflare — same V8
 * isolate, same global APIs, same D1 binding shape as production
 * Pages Functions. The D1 binding is backed by an in-memory SQLite
 * that miniflare opens fresh for each test worker; tests apply the
 * `migrations/` schema explicitly in their `beforeEach` so each test
 * starts from a known state.
 *
 * `wrangler.toml` is the source of truth for the binding name and the
 * project-level compatibility date; we point miniflare at it rather
 * than redeclaring those here.
 */
import { defineWorkersProject } from "@cloudflare/vitest-pool-workers/config";

export default defineWorkersProject({
  test: {
    poolOptions: {
      workers: {
        wrangler: { configPath: "./wrangler.toml" },
        miniflare: {
          // Override the production D1 database with miniflare's
          // in-memory shim. The binding name (DB) stays the same so
          // the Functions code is unchanged between prod and test.
          d1Databases: { DB: "test" },
          compatibilityFlags: ["nodejs_compat"],
        },
      },
    },
    include: ["functions/__tests__/**/*.test.ts"],
  },
});
