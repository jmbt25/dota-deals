/**
 * Test setup helpers for the Pages Functions vitest suite.
 *
 * @cloudflare/vitest-pool-workers runs each test in a real Workers
 * isolate with miniflare's in-memory SQLite backing the `env.DB`
 * binding. The DB starts empty; tests apply the production schema
 * and a baseline data seed via the helpers below.
 *
 * Vite's `?raw` import suffix bundles the SQL files as strings at
 * test-build time, so the helpers don't need filesystem access at
 * runtime (which Workers don't have).
 *
 * Each test should do:
 *
 *   beforeEach(async () => {
 *     await applyMigrations();
 *     await seedBaseline();
 *   });
 *
 * …and use `env.DB` directly for any test-specific overrides.
 */

import { env } from "cloudflare:test";
// Vite's `?raw` import: read the file's contents as a string at
// test-build time. `migrations/0001_initial.sql` is the same schema
// `wrangler d1 migrations apply` deploys to production D1.
import schemaSql from "../../migrations/0001_initial.sql?raw";
import seedSql from "./seed.sql?raw";

/** Apply the production schema migration to the test D1. Idempotent
 * over a freshly-allocated miniflare D1, but a re-call against the
 * same DB would fail on the `INSERT INTO sqlite_sequence` rows the
 * schema doesn't create. Call once per test, not within `beforeAll`
 * across a suite. */
export async function applyMigrations(): Promise<void> {
  await execScript(env.DB, schemaSql);
}

/** Apply the baseline data seed described in `seed.sql`. */
export async function seedBaseline(): Promise<void> {
  await execScript(env.DB, seedSql);
}

/** Run a multi-statement SQL string against the given D1.
 *
 * D1's `db.exec()` is documented to handle multi-statement input but
 * has historically been finicky about comments and blank lines.
 * Splitting on `;` followed by newline and running each statement via
 * `prepare().run()` is the portable form — same approach the Python
 * test fake takes.
 */
async function execScript(db: D1Database, sql: string): Promise<void> {
  const statements = sql
    .split(/;\s*[\r\n]+/)
    .map(stripComments)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);

  for (const stmt of statements) {
    await db.prepare(stmt).run();
  }
}

/** Strip single-line `-- comment` lines from a SQL statement.
 *
 * Block comments (`/* ... *​/`) aren't used in our schema/seed so
 * we don't bother handling them. Keeps the splitter above robust
 * against the comments we DO use.
 */
function stripComments(stmt: string): string {
  return stmt
    .split(/[\r\n]+/)
    .filter((line) => !line.trim().startsWith("--"))
    .join("\n");
}
