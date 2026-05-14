# D1 storage layer

dota-deals' storage is Cloudflare D1, accessed over the public REST API.
The pipeline runs as a Python process on GitHub Actions and talks to D1
via async HTTP. As of Phase 10, the GHA workflow is committed to D1 —
the pipeline no longer pulls/pushes SQLite via R2, and no longer
generates static `public/data/` JSON. The frontend at `dotadeals.com`
serves whatever was committed at Phase 10 ship and stays stale until
Phase 12 wires it to the Worker that's built in Phase 11.

This doc is the operational reference. The migration narrative is
captured below as the eight-commit table; the surrounding sections
document how the live storage layer behaves.

## Migration narrative

The path from R2-synced SQLite + static-JSON publishing to async D1
+ Pages Functions took the commits below. Each cutover phase
produced at least one reality-only bug that no mocked test could
have caught; every such bug is pinned by a regression test or a
dedicated section of this doc.

| Commit | Phase | What landed | Reality-only catch |
|---|---|---|---|
| `232958d` | 9a | D1 HTTP client + schema migration + Pydantic envelopes + 23 wire tests | — |
| `c967f8d` | 9a fix | `wrangler.toml` so `migrations apply` finds the binding | smoke-test prerequisite (`No configuration file found`) |
| `90d2598` | 9b | Async storage layer alongside sync (no caller flipped yet) | — |
| `f71a900` | 9c-i | Cut ingest runner; converted `test_ingest`; D1FakeClient seam | — |
| `b07cb98` | 9c-i fix | Steam React-SSR migration broke `/render` JSON endpoint; pivot to `search/render?norender=1` with exact `hash_name` filter | quarantined HTML payloads against real Steam |
| `e5a2d89` | 9c-ii | Cut signals layer + `DataLookup` pre-fetch pattern (O(items × signals) HTTP calls → ~8 total) | D1 `/query` batch shape must be `{"batch": [...]}` not a bare array (HTTP 400 code 7400) |
| `44b5776` | 9c-iii | Cut scoring/publish/universe runners + bulk-read functions | D1 per-statement bound-parameter limit is 100, not the SQLite default of 999 (`too many SQL variables`) |
| `8527795` | 9c-iv | Delete sync code; rename `*_async` → unsuffixed; consolidate docs | — |
| Phase 10 | GHA workflow rewrite: D1 migrate step, ingest/signals/score against D1, R2 sync removed, publish step removed | `CLOUDFLARE_ACCOUNT_ID` missing → wrangler hits `/memberships` → 403 from account-scoped token (see `docs/DEPLOYMENT.md` failure-recovery row) |
| `fffbce8` | 11 | TypeScript Pages Functions at `/api/*` (5 endpoints + middleware + types + 38 vitest tests) | recurring Cloudflare build-token invalidation; first Pages-side deploy attempt hit `.mypy_cache` exceeding Pages' 25 MiB-per-file limit |
| `f74a074` | 12 | Frontend `fetch()` rewired from `/data/*.json` to `/api/*`; site live against real D1 at `dotadeals.com` | wrangler 4.x local `pages dev` 404s on the static frontend at `/` while serving `/api/*` correctly (production unaffected) |
| `fd3cf8f` | 12 follow-on | `.github/workflows/deploy.yml` workflow_dispatch deploy bypass | Cloudflare's Manila edge in extended maintenance blocked local `npm run deploy`; GH runners (US-based) hit a different edge |
| Phase 13 (this commit) | Delete `publish/`, `publish.r2`, `db pull`/`db push` CLI commands, R2 `Settings` fields, `public/data/`, `docs/PUBLISH.md`; promote `deploy.yml` → `deploy-frontend.yml` with `push` trigger | — |

The pattern across the phases: every cutover commit produced at
least one reality-only bug. The smoke-test discipline (each
cutover phase finished with a manual run against real D1, not just
the mocked test suite) is what caught them before the live cron
noticed. That workflow choice is worth more than any individual
technical decision in the migration.

The arc ended at `v2.0`: D1 is the only storage path, Pages
Functions is the only frontend data path, deploy is one
auto-triggering GHA workflow plus a manual fallback. No vestigial
paths remain in production code.

## Why D1

Three constraints pushed us off R2-synced SQLite:

1. **Concurrency.** With R2 sync, only one run can hold the database at
   a time. Ad-hoc tools (backfill scripts, one-off queries from a
   Worker, the eventual REST API) don't compose with a single-writer
   pattern.
2. **Frontend story.** The post-MVP plan serves item-detail pages from
   a Worker that queries the database directly. A Worker can read D1 in
   milliseconds; reading R2-SQLite would mean re-downloading the whole
   file per request.
3. **Operational surface.** D1 gives us `wrangler d1 execute` /
   `wrangler d1 migrations`, web-UI query inspection, and per-query
   metrics for free. The R2-SQLite story required hand-rolled push/pull
   plus a stuck-lock recovery story that never quite landed.

## Why D1

Three constraints pushed us off R2-synced SQLite:

1. **Concurrency.** With R2 sync, only one run can hold the database at
   a time. Ad-hoc tools (backfill scripts, one-off queries from a
   Worker, the eventual REST API) don't compose with a single-writer
   pattern.
2. **Frontend story.** The post-MVP plan serves item-detail pages from
   a Worker that queries the database directly. A Worker can read D1 in
   milliseconds; reading R2-SQLite would mean re-downloading the whole
   file per request.
3. **Operational surface.** D1 gives us `wrangler d1 execute` /
   `wrangler d1 migrations`, web-UI query inspection, and per-query
   metrics for free. The R2-SQLite story required hand-rolled push/pull
   plus a stuck-lock recovery story that never quite landed.

## Setup

`wrangler` resolves the human-readable database name (`dota-deals`) to
its UUID via the checked-in `wrangler.toml` at the repo root. Without
that file, `wrangler d1 migrations apply` fails with "No configuration
file found."

One-time:

```bash
# DB already exists at id cbd9fdf6-127b-4295-aeb9-5c1ea9aca9a7,
# region ENAM. Captured for reproducibility.
wrangler d1 create dota-deals

# Apply the schema. Idempotent — safe to rerun.
wrangler d1 migrations apply dota-deals --remote

# Verify the tables landed.
wrangler d1 execute dota-deals --remote \
    --command "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
```

Locally, run against a wrangler-managed SQLite shim:

```bash
wrangler d1 migrations apply dota-deals --local
wrangler d1 execute dota-deals --local \
    --command "SELECT count(*) FROM items;"
```

Set these in `.env` (and as GitHub Actions secrets in Phase 10):

```
CLOUDFLARE_ACCOUNT_ID=<account id>
CLOUDFLARE_D1_DATABASE_ID=cbd9fdf6-127b-4295-aeb9-5c1ea9aca9a7
CLOUDFLARE_D1_API_TOKEN=<token with "D1: Edit" on this database>
```

The token only needs `D1: Edit` on the one database; do not reuse a
broader-scoped token here.

## Architecture

- **`storage/d1_client.py`** — HTTP boundary. Async client over the
  Cloudflare REST API with the typed exception hierarchy below,
  response-envelope Pydantic models, client-side batch chunking,
  hand-rolled retries with jittered backoff for transient/5xx errors,
  and `Retry-After`-honoring 429 handling. Tests in
  `tests/test_storage_d1_client.py` use respx to pin wire shape.
- **`storage/db.py`** — application-level `D1Connection` wrapper that
  accumulates `rows_read` / `rows_written` across the lifetime of a
  connection and logs a WARNING at close when totals exceed
  `Settings.d1_daily_budget_warn`. Also exports `connect()` as an
  `@asynccontextmanager` and the storage exception hierarchy
  (`StorageError`, `IntegrityViolation`, `SchemaError`).
- **`storage/repositories.py`** — typed async repository functions.
  Per-row writes for one-shots; batch writes (`insert_price_points`,
  `insert_signals`, etc.) for hot paths; bulk reads
  (`daily_prices_for_items`, `signals_for_items_on_date`) for the
  signal-runner's fetch-once / dispatch-many pattern.
- **`migrations/0001_initial.sql`** — the schema. Applied by
  `wrangler d1 migrations apply` to remote D1 and by
  `tests/_d1_fake.py::D1FakeClient` to the in-memory SQLite test
  fake. Every CREATE is `IF NOT EXISTS`, every migration is
  idempotent.

`tests/_d1_fake.py` is the in-memory drop-in for `D1Client`. Its
`__aenter__` opens a fresh `sqlite3.Connection(":memory:")` with
`isolation_level=None` (autocommit; the explicit `BEGIN`/`COMMIT` in
`batch()` matches D1's documented batch atomicity), then applies
every `[0-9]*.sql` file in `migrations/` in lex order — so the fake's
schema can't drift from production's. Repository tests run against the
fake; the D1 client itself is tested with respx HTTP mocks.

## Error model

`d1_client.py` exposes a small exception hierarchy so callers can
branch by disposition rather than parsing strings.

| Exception | When | Disposition |
|---|---|---|
| `D1ConfigError` | A required `CLOUDFLARE_*` setting is missing at construction. | Fail fast at process startup — fix the env, not the request. |
| `D1AuthError` | Cloudflare returned 401 or 403. | Don't retry. Rotate the token. |
| `D1NotFoundError` | 404 from the gateway — account or database id is wrong. | Don't retry. Fix the configuration. |
| `D1RateLimitError` | 429 after retries are exhausted. Carries `retry_after_s` from the header when present. | Backoff at the caller; consider whether the burst can be smoothed. |
| `D1QueryError` | D1 ran the request but `success=False`, or returned a 4xx other than 429. Carries the failing SQL + params. | Surface to logs; classify by code (e.g., 7500 = UNIQUE constraint). Constraint violations on idempotent inserts shouldn't reach this path — use `INSERT OR IGNORE`. |
| `D1TransportError` | Network error or 5xx after retries exhausted. | Caller decides: abort the run, retry after a longer pause, or surface as `partial`. |

The repository layer translates these onto the sync-compatible
`StorageError` / `IntegrityViolation` hierarchy at the boundary so
caller code (runners, builders) stays storage-agnostic.

## Median moves to Python

The local SQLite schema had a `v_daily_price` view backed by a
user-registered `MEDIAN` aggregate. D1 doesn't expose
`create_aggregate` (or any other Python escape hatch), so per-day
median lives in Python:

- `daily_prices(item_id, days, as_of)` fetches raw `price_history`
  rows, groups by `date(observed_at)` in Python, and computes the
  integer-floor median per group.
- The signal runner pre-fetches all items' price history once per
  invocation into a `DataLookup`, so the median runs once per
  (item, date), not once per signal.

Cost estimate at v1 scale: 800 items × 3 obs/day × 90 days = 216k rows
read; the median fits in a single integer-sorted slice per
(item, date) bucket. Negligible relative to the HTTP round-trip
budget.

## Operational notes

- **Batch size.** `D1_MAX_BATCH_SIZE=100` is conservative; D1 documents
  100 as the practical batch ceiling. The client splits any batch
  above this transparently and stitches results in order.
- **Variable limit.** D1 enforces a per-statement bound-parameter
  cap of 100 (much tighter than the upstream SQLite default of 999).
  Bulk reads chunk their IN clauses at `_BULK_QUERY_CHUNK_SIZE = 90`
  to leave headroom for up to 10 non-IN params. This was a real-D1
  surprise caught in Phase 9c-iii — the previous chunk size of 100
  worked against the test fake (which inherits SQLite's loose
  default) but tripped "too many SQL variables" on real D1 the
  moment the universe filled past 100 items.
- **Sequential sub-batches.** When a `batch()` call exceeds the size
  limit, sub-batches are issued sequentially. Each sub-batch is its
  own D1 transaction; parallelizing would lose the all-or-nothing
  semantics each transaction promises locally.
- **Wire shape for batches.** D1's `/query` endpoint accepts either
  `{"sql": ..., "params": ...}` for a single statement or
  `{"batch": [...]}` for multi-statement transactions. A bare top-
  level array returns HTTP 400 with code 7400 "Invalid input:
  Expected object, received array." Pinned by the
  `test_batch_sends_all_statements_in_one_request` test.
- **Retries.** Three attempts for network/timeout/5xx with full
  jitter on exponential backoff (1s → 2s → 4s, capped at 30s).
  Sleep is injectable for tests.
- **Rate-limit handling.** 429 honors `Retry-After` when present;
  otherwise falls back to 30s. The retry budget is the same three
  attempts as transport errors — D1's API gateway rarely rate-limits
  pipeline-scale traffic, so we don't burn extra attempts on it.
- **Bool coercion.** Pydantic-validated booleans go in as Python
  `bool`; the wire coerces them to `int` since D1's REST API binds
  ints more reliably than booleans. Caller code stays type-correct.

## Diagnostics

When something looks wrong in a scheduled run:

- D1 query log (Cloudflare dashboard → Workers & Pages → D1 → your db
  → Logs) shows every query with timings and the offending SQL.
- For local repro, copy the SQL out of a `D1QueryError`'s `.sql`
  attribute and run it with `wrangler d1 execute --local`.
- `EXPLAIN QUERY PLAN` works inside `wrangler d1 execute` and is the
  fastest way to confirm an index is being used.
- The `D1Connection`'s budget summary line at process exit logs
  cumulative rows-read/rows-written. WARNING fires when reads exceed
  `D1_DAILY_BUDGET_WARN` (default 1M); set to 0 in `.env` to silence
  during a deliberate full-table scan.

## Lessons the smoke tests taught us

Each phase of the migration produced a real-D1 bug that no mocked
test could catch. Each is now pinned by a regression test so the
lesson can't be unlearned:

1. **Phase 9a `/render` deprecation.** Steam migrated the Community
   frontend to React SSR; the legacy `/market/listings/<appid>/<name>/render`
   URL returns ~600KB HTML rather than JSON regardless of headers.
   Fix: `fetch_listings` now hits `/market/search/render?norender=1`
   with exact `hash_name` filtering. Test: `test_runner_listings_html_response_quarantines`.
2. **Phase 9c-ii batch wire shape.** D1's `/query` endpoint rejects a
   bare-array body with code 7400. The fix wraps batches as
   `{"batch": [...]}`. Test: `test_batch_sends_all_statements_in_one_request`
   pins the wrapping object.
3. **Phase 9c-iii variable limit.** D1 enforces 100 bound parameters
   per statement, not the SQLite default of 999. Bulk reads chunk at
   90 to leave headroom; the variable-limit comment in
   `repositories.py` keeps the lesson in code.

The pattern: every cutover phase produces at least one
reality-only bug. The smoke-test discipline is what catches them
before they reach a scheduled run.
