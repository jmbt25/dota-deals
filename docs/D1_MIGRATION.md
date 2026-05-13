# D1 migration

We're moving dota-deals' storage backend from local SQLite (synced to R2
between GHA runs) to Cloudflare D1, accessed over the public REST API.
The pipeline stays a Python process on GitHub Actions; only the storage
layer changes. The frontend keeps reading the same JSON files in
`public/data/` for now — a TypeScript Worker that serves directly from
D1 lands in a later phase.

## Why D1

Three constraints push us off the R2-synced SQLite:

1. **Concurrency.** With R2 sync, only one run can hold the database at
   a time. As we add ad-hoc tools (backfill scripts, one-off queries
   from a Worker, the eventual REST API), a single-writer pattern stops
   scaling.
2. **Frontend story.** The post-MVP plan serves item detail pages from
   a Worker that queries the database directly. That Worker can read
   D1 in milliseconds; reading R2-SQLite would mean re-downloading the
   whole file per request.
3. **Operational surface.** D1 gives us `wrangler d1 execute` /
   `wrangler d1 migrations`, web-UI query inspection, and per-query
   metrics for free. The R2-SQLite story required hand-rolled push/pull
   plus a stuck-lock recovery story we never quite finished.

## Phasing

This migration is split into discrete commits to keep the test suite
green at every step:

| Phase | Scope | Status |
|---|---|---|
| 9a | D1 client (HTTP boundary), settings, schema migration, client tests | **this commit** |
| 9b | Repository layer rewrite (async, batch-aware) + DataLookup pre-fetch + runner refactor + test conversion | next |
| 10 | GHA workflow: drop R2 sync, point at D1 | follows 9b |
| 11 | TypeScript Worker that queries D1 directly | post-10 |
| 12 | Frontend talks to Worker; static JSON files retire | post-11 |
| 13 | Delete R2 client, `publish/` module, and committed `public/data/` | last |

What this commit (9a) lands:

- `src/dota_deals/storage/d1_client.py` — async HTTP client with the
  typed exception hierarchy below, response-envelope Pydantic models,
  client-side batch chunking, hand-rolled retries with jittered
  backoff for transient/5xx errors and ``Retry-After``-honoring 429
  handling.
- `migrations/0001_initial.sql` — full schema, idempotent (`IF NOT
  EXISTS` everywhere). One divergence from local SQLite: the
  `v_daily_price` view is gone (see "Median moves to Python" below).
- `tests/test_storage_d1_client.py` — respx-mocked HTTP tests.
- Five new `Settings` fields documented in `.env.example`.

What 9a does **not** touch:

- The current `storage.db` / `storage.repositories` / `storage.schema.sql`
  files. They remain the active code path for every test in the suite
  until 9b cuts over.
- Any runner, the CLI, the publish layer, the frontend.

## Setup

`wrangler` resolves a D1 database name (`dota-deals`) to its UUID via a
`wrangler.toml` at the repo root — checked in alongside this doc.
Without that file, `wrangler d1 migrations apply` fails with "No
configuration file found." The committed `wrangler.toml` only declares
the D1 binding for now; the Worker `name` / `main` fields start
mattering in Phase 11.

One-time, before phase 9b:

```bash
# Already done — DB exists at id cbd9fdf6-127b-4295-aeb9-5c1ea9aca9a7,
# region ENAM. Captured here for reproducibility.
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

Set these in `.env` (and as GitHub Actions secrets later, in phase 10):

```
CLOUDFLARE_ACCOUNT_ID=<account id>
CLOUDFLARE_D1_DATABASE_ID=cbd9fdf6-127b-4295-aeb9-5c1ea9aca9a7
CLOUDFLARE_D1_API_TOKEN=<token with "D1: Edit" on this database>
```

The token only needs `D1: Edit` on the one database; do not reuse a
broader-scoped token here.

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

All inherit from `D1Error` so a catch-all is still possible at the CLI
top level, but the typed hierarchy is the supported interface.

## Median moves to Python

The local SQLite schema had a `v_daily_price` view backed by a
user-registered `MEDIAN` aggregate. D1 doesn't expose
`create_aggregate` (or any other Python escape hatch), so we move the
per-day median into Python.

Concretely, in 9b:

- `daily_prices(item_id, days, as_of)` becomes a thin wrapper that
  fetches raw `price_history` rows, groups by `date(observed_at)` in
  Python, and computes the integer-floor median per group.
- The signal-runner pre-fetches all items' price history once per
  invocation into a `DataLookup`, so the median runs once per
  (item, date), not once per signal.

Cost estimate at v1 scale: 800 items × 3 obs/day × 90 days = 216k rows
read; the median fits in a single integer-sorted slice per
(item, date) bucket. Negligible relative to the HTTP round-trip
budget.

## Operational notes

- **Batch size.** `D1_MAX_BATCH_SIZE=100` is conservative; D1 tolerates
  larger batches but documents 100 as the practical ceiling. The
  client splits any batch above this transparently and stitches
  results in order.
- **Sequential sub-batches.** When a `batch()` call exceeds the size
  limit, sub-batches are issued sequentially, not in parallel. Each
  sub-batch is its own D1 transaction; parallelizing would break the
  illusion of one logical write being all-or-nothing for the caller
  (it would already be that way at sub-batch granularity, but the
  failure mode would be subtler).
- **Retries.** Three attempts for network/timeout/5xx, with full
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

## What's left

After 9b lands, this doc gets an "Operational pitfalls" section
populated by real incidents. The pitfalls are easier to write down
once we've stubbed our toes on them than predict ahead of time.
