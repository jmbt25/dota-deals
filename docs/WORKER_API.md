# Worker API

The dota-deals Pages Functions API. Read-only over Cloudflare D1, deployed
as part of the same Cloudflare Pages project that serves the static
frontend at [dotadeals.com](https://dotadeals.com). No authentication
(data is public). No CORS (API and frontend share the same origin).

## Endpoint reference

All endpoints return JSON. Successful responses have a `schema_version`
field on the top-level envelope; errors return a structured
`{ error, message, status }` body with the matching HTTP status code.

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Operational status (warmup / degraded / operational), last successful run, data coverage, warmup countdown. |
| GET | `/api/report/latest` | Top-N buy scores for the most recent scored date. Returns warmup envelope (status=warmup, scores=[]) if no scored date exists. |
| GET | `/api/report/:date` | Top-N buy scores for `:date` (YYYY-MM-DD). 404 with JSON body if the date has no scores; 400 if the date is malformed. |
| GET | `/api/items/:id` | One item's metadata + 30 days of daily prices + listing counts + per-signal series. 404 if the item doesn't exist; 400 if the id isn't a non-negative integer. |
| GET | `/api/runs?limit=20` | Recent pipeline runs across all kinds, ordered by `started_at` desc. `limit` defaults to 20, clamps to [1, 100]. 400 if `limit` isn't a positive integer. Debug/ops view — not part of the user-facing surface. |

Response details (field shapes, nullable fields, USD-string format) live
in [functions/types.ts](../functions/types.ts), the single source of
truth for the wire format. (Pre-Phase-13 the contract was kept in
lock-step with the Python `publish/models.py` Pydantic models; that
module was deleted in the Phase 13 cleanup once the frontend stopped
reading static JSON.)

### Common headers

Every `/api/*` response carries:

- `Content-Type: application/json; charset=utf-8`
- `Cache-Control: no-store, max-age=0` — read paths over data that changes
  a few times per day; edge caching would create stale-data confusion
  that's not worth the saved latency at v1 scale.
- `X-Robots-Tag: noindex, nofollow` — API URLs aren't user-facing pages
  and shouldn't appear in search results.

Handlers can override `Cache-Control` by setting it on their response
before returning; the middleware only sets it when absent.

## Wire-format conventions

Defined in [`functions/types.ts`](../functions/types.ts); the rules
the rendering JS in `public/index.html` depends on:

- **Dates**: ISO `YYYY-MM-DD` strings.
- **Datetimes**: ISO 8601 with a trailing `Z` (UTC only).
- **Prices**: USD strings (`"12.34"`). Persisted as INTEGER cents
  internally; converted at the wire boundary by
  `centsToUsdString()`. Never floats in the wire payload — float
  rounding drift across stages was a pain point in Phase 6 that the
  string-cents pattern fixed.
- **Nullable fields**: `T | null`, explicit. The frontend's empty-state
  UI distinguishes "value is null" from "field absent".
- **Envelopes**: `schema_version: 1` on every top-level payload.
  Reserved for future wire-format migrations.

The contract is pinned by the vitest suite at
[`functions/__tests__/`](../functions/__tests__/): every endpoint has
a test that asserts the wire shape against the
`functions/__tests__/seed.sql` baseline. Drift between the rendering
JS and the API breaks the suite locally before it can ship.

## Local development

Prerequisites:

```bash
npm install          # one-time; installs hono, wrangler, vitest, types
```

### Run the API locally

```bash
npx wrangler pages dev
# /api/* routes are Functions, served from functions/api/*.ts.
# Probe them via curl: curl http://localhost:8788/api/health
```

Local D1 setup (first run only):

```bash
# Create a local shim D1 (wrangler keeps it in .wrangler/).
npx wrangler d1 migrations apply dota-deals --local

# Optional: seed it with the test baseline (4 items, scores, runs)
# so endpoints render non-trivially. The same SQL the vitest suite
# applies before every test.
npx wrangler d1 execute dota-deals --local --file=functions/__tests__/seed.sql
```

The `--local` flag points wrangler at the in-process SQLite shim instead
of remote D1. Production data is never touched by local dev.

### Local-dev static-asset quirk

As of wrangler 4.90.1, `wrangler pages dev` serves the Functions at
`/api/*` correctly but returns 404 on the static frontend at `/`.
Tried with the directory arg as `.`, `public`, and omitted —
all three reproduce. Production Pages deploys serve both static
and Functions correctly (verified end-to-end on the Phase 11 and
Phase 12 ships), so this is a dev-server quirk in wrangler 4.x
rather than a project issue. Workaround paths:

1. **For API work:** use `wrangler pages dev` and curl `/api/*`
   endpoints directly. Vitest is the better path for repeatable
   behavioural testing.
2. **For frontend visual work:** deploy to a preview URL via
   `npm run deploy` and inspect at the returned `*.pages.dev`
   URL. Each deploy gets a unique commit-prefixed subdomain so
   you can iterate without affecting the canonical
   `dotadeals.com`.

If a future wrangler release fixes the local static-asset
serving, both this section and the README's local-dev block
collapse to "just run `wrangler pages dev` and open localhost".

### Run the tests

```bash
npx vitest run            # one-shot
npx vitest                # watch mode
```

Tests run inside a real Workers isolate via `@cloudflare/vitest-pool-workers`.
Each test starts from a fresh in-memory D1 with the production schema
(from `migrations/0001_initial.sql`) applied, plus the baseline data
in `functions/__tests__/seed.sql`.

### Type-check

```bash
npx tsc --noEmit
```

Pre-commit: lint + format aren't wired through tooling yet because the
TypeScript surface is small and stable. If it grows, `npx eslint` and
`npx prettier --check .` are the next adds.

## Production deploy

The Cloudflare Pages dashboard build runs `npx wrangler pages deploy .
--project-name=dota-deals`, which picks up both `public/` (from
`pages_build_output_dir` in `wrangler.toml`) and `functions/` (Pages'
filesystem-routing convention) in one shot. Successful deploys mirror
the static frontend to the same domain that serves `/api/*` —
same origin, no CORS needed.

### Inspecting production logs

Pages Functions logs go to Cloudflare's dashboard automatically:

1. dash.cloudflare.com → **Workers & Pages**
2. Select the `dota-deals` project
3. **Functions** tab → **Real-time logs**

Each `/api/*` request emits two structured log lines: one
`api_uncaught_error` line when something throws (rare), and one
`api_request` line per response with `path`, `method`, `status`,
`duration_ms`. Filter on `event=api_uncaught_error` to surface
real failures.

### Inspecting D1 query metrics

dash.cloudflare.com → **Workers & Pages → D1 → dota-deals → Query Logs**
gives per-query timings and the offending SQL. For local repro, copy
the SQL and run it with `wrangler d1 execute dota-deals --local --command "..."`.

## Endpoint internals: where the SQL lives

Each handler file (`functions/api/<endpoint>.ts`) contains its own SQL,
inlined. Queries that are genuinely shared between endpoints —
"build a WireScore list for one date", "look up items by id batch" —
live in [functions/queries.ts](../functions/queries.ts) so the two
report endpoints don't drift.

The Python `publish/builder.py` was the original reference
implementation; it was deleted in Phase 13 once the frontend
stopped reading static JSON. The TypeScript queries here remain
line-for-line equivalent to what `builder.py` did against the
async storage layer — re-deriving them later from the test suite
or D1 schema is straightforward if needed.
