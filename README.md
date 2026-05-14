# dota-deals

Buy-signal analytics for the Steam Community Market — Dota 2 arcanas and
immortals. The Steam Market shows a price chart and nothing else; dota-deals
shows *why* an item is a good buy right now, with every score exposing its
component signals so you can disagree intelligently. v1 is a fault-tolerant
pipeline, not a trading strategy.

## How it works

1. **`universe refresh`** scrapes Steam's market search for the arcana and
   immortal rarity tags and upserts every result into the `items` table.
   Items not seen in three consecutive refreshes are deactivated;
   previously-deactivated items reappearing reactivate.
2. **`ingest`** hits two Steam endpoints per item — `/market/priceoverview`
   and `/market/listings/<appid>/<name>/render` — and writes per-poll rows
   to `price_history`, `listing_history`, and the `latest_observation`
   cache. Bounded async concurrency, retries, 429 cool-down.
3. **`signals compute`** runs four statistical signals per active item per
   UTC date (`price_zscore`, `supply_velocity`, `event_proximity`,
   `comparables_delta`) and writes one row per `(item, signal_name)` to
   `signals`.
4. **`score`** composes the four signals into a single `buy_score` with
   renormalization for null signals, picks a one-line explanation citing
   the strongest contributor, and writes to `scores`.
5. **`report`** reads the top-N from `scores` and renders to stdout
   (deterministic format) or atomic JSON.

Every stage writes a row to `runs` tagged with its `parent_run_id` so an
end-to-end batch is queryable as a single unit.

## Reliability features

Each pattern is implemented at a specific file location and pinned by a
test. If something looks like it broke, start from the linked test.

| Pattern | Implementation | Test that proves it |
|---|---|---|
| **Idempotent writes** — every `(item, observed_at)` (or `(item, signal_name, date)` / `(item, date)`) primary key writes once. Reruns are no-ops. | [`storage/repositories.py`](src/dota_deals/storage/repositories.py) (`INSERT OR IGNORE`) | [`test_ingest.py::test_runner_idempotent_double_run`](tests/test_ingest.py), [`test_signals_runner.py::test_idempotent_rerun_does_not_double_write`](tests/test_signals_runner.py), [`test_scoring_runner.py::test_idempotent_rerun_does_not_double_write`](tests/test_scoring_runner.py) |
| **Quarantine** — bad payloads land in a dead-letter table with the raw bytes, not silently dropped. | [`ingest/runner.py`](src/dota_deals/ingest/runner.py), [`storage/repositories.py::quarantine_record`](src/dota_deals/storage/repositories.py) | [`test_ingest.py::test_runner_validation_routes_to_quarantine`](tests/test_ingest.py) |
| **Retry/backoff** — 3 attempts with exponential backoff + jitter for timeouts, transport, and 5xx; 4xx is one-shot. | [`ingest/steam.py::_get_json`](src/dota_deals/ingest/steam.py) | [`test_ingest.py::test_client_timeout_retried_then_succeeds`](tests/test_ingest.py), `test_client_5xx_retried`, `test_client_4xx_not_retried` |
| **429 cool-down** — a 429 trips a process-global ready event; in-flight retries wait for it before re-issuing. Cool-down task has explicit `add_done_callback` so its exceptions don't disappear. | [`ingest/steam.py`](src/dota_deals/ingest/steam.py) (`_trigger_cooldown`, `_on_cooldown_done`) | [`test_ingest.py::test_client_429_extended_backoff`](tests/test_ingest.py) |
| **Polling-slot truncation** — `observed_at` is `floor(now.hour / cadence) * cadence`, minute/second zeroed. Re-runs in the same slot collide on the PK. | [`ingest/runner.py::slot_for`](src/dota_deals/ingest/runner.py) | [`test_ingest.py::test_slot_for_truncates_to_polling_slot`](tests/test_ingest.py) |
| **Partial-run propagation** — the scoring stage reads the latest ingest run's status for the date and surfaces "this score's underlying ingest was partial" per row. | [`scoring/runner.py`](src/dota_deals/scoring/runner.py), [`notifier/json_file.py`](src/dota_deals/notifier/json_file.py) | [`test_scoring_runner.py::test_partial_ingest_propagates_to_score_data_quality`](tests/test_scoring_runner.py), `test_item_missing_from_ingest_flagged_in_data_quality` |
| **3-strike deactivation** — only true 4xx (400-499 except 429) increments the strike counter. Exactly 3 strikes flips `active=0`. Universe refresh reactivates. | [`ingest/runner.py::_record_failure_strike`](src/dota_deals/ingest/runner.py), [`storage/repositories.py::upsert_item`](src/dota_deals/storage/repositories.py) (reactivation in `ON CONFLICT`) | [`test_ingest.py::test_deactivation_fires_at_exactly_three_strikes`](tests/test_ingest.py), [`test_universe.py::test_reactivation_when_item_reappears`](tests/test_universe.py) |
| **Resumable on crash** — if a prior run wrote part of the signals for a date, rerunning fills in the missing rows without disturbing the existing ones. | [`signals/runner.py`](src/dota_deals/signals/runner.py) (PK + `INSERT OR IGNORE`) | [`test_signals_runner.py::test_resumable_after_partial_prior_run`](tests/test_signals_runner.py) |
| **Per-signal exception isolation** — one signal's bug emits a null row with the exception class in metadata; other signals and other items continue. | [`signals/runner.py`](src/dota_deals/signals/runner.py) | [`test_signals_runner.py::test_per_item_per_signal_exception_isolated`](tests/test_signals_runner.py) |
| **Deterministic report format** — the stdout report is pinned byte-for-byte by a golden-string test so accidental drift surfaces immediately. | [`notifier/stdout.py`](src/dota_deals/notifier/stdout.py) | [`test_notifier_stdout.py::test_golden_stdout_two_items`](tests/test_notifier_stdout.py) |

## How to run it

### Pipeline

Requires **Python 3.12** (pinned `>=3.12,<3.13`). No secrets needed — Steam
Market endpoints are public.

```bash
python -m venv .venv
. .venv/bin/activate            # or .venv\Scripts\activate on Windows
pip install -e ".[dev]"
cp .env.example .env            # then edit if your defaults differ
make check                      # ruff + ruff format + mypy --strict + pytest
```

Five CLI commands compose the end-to-end pipeline:

```bash
dota-deals universe refresh                       # populate the items table
dota-deals ingest --items items.txt               # fetch prices + listings
dota-deals signals compute --date 2026-05-12      # 4 signals × N items
dota-deals score --date 2026-05-12                # compose buy scores
dota-deals report --date 2026-05-12 --top 20 \
   --out reports/2026-05-12.json                  # JSON; omit --out for stdout
```

Each stage writes one row to `runs` with `kind` and a shared
`parent_run_id`. To watch what happened:

```sql
SELECT kind, status, items_ok, items_failed, started_at
FROM runs
ORDER BY started_at DESC
LIMIT 20;
```

### Frontend

The single-page frontend at [`public/index.html`](public/index.html)
is a vanilla-JS static site — no build step, no framework. It
`fetch()`es five endpoints from the same-origin Pages Functions
API (Phase 11) and renders one of five states (LOADING, WARMUP,
OPERATIONAL, DEGRADED, ERROR) based on the responses:

| Endpoint | Used for |
|---|---|
| `/api/health` | Status banner, last-run timestamp, warmup countdown |
| `/api/report/latest` | The top-N table on the operational view |
| `/api/report/:date` | Historical date-picker lookups |
| `/api/items/:id` | Lazy-loaded per-row expand panel (chevron click) |

The same HTML works at `dota-deals.pages.dev` and `dotadeals.com` —
endpoints are root-relative paths, no scheme or host hardcoded.

To run the API locally against a miniflare-shimmed D1:

```bash
npm install                # one-time, installs hono + wrangler + types
npx wrangler d1 migrations apply dota-deals --local   # one-time, applies schema
npx wrangler pages dev     # http://localhost:8788
```

The local D1 is empty by default. To exercise non-warmup state,
seed it from the test fixture:

```bash
npx wrangler d1 execute dota-deals --local --file=functions/__tests__/seed.sql
```

`curl http://localhost:8788/api/health` should then return
`status: "operational"` against the four-item baseline.

**Local-dev caveat:** `wrangler pages dev` (as of 4.90.1) serves
the Functions at `/api/*` correctly but 404s on the static
frontend at `/`. The production deploy serves both correctly —
this is a wrangler-4 dev-server quirk, not a project issue. For
visual frontend work today, the verification path is "deploy to a
preview URL via `npm run deploy`, view at the `*.pages.dev`
preview URL." A wrangler upstream fix would let `wrangler pages
dev` serve the full stack locally; until then, the API can be
exercised via curl as shown above and the frontend visually
against the preview deploy.

The wire-format contract for every endpoint is documented in
[`functions/types.ts`](functions/types.ts) and pinned by the vitest
suite at [`functions/__tests__/`](functions/__tests__/). The static
JSON publishing layer (Python `publish/` module, `public/data/`
fixtures) that originally produced this contract was deleted in
Phase 13; the types in TypeScript are now the only source of truth.

## Deployment

The system runs unattended on **GitHub Actions** (8-hourly cron in
[.github/workflows/pipeline.yml](.github/workflows/pipeline.yml)),
storing all data in **Cloudflare D1** via the public REST API. The
frontend is hosted on **Cloudflare Pages** at
[dotadeals.com](https://dotadeals.com) and reads from same-origin
**Pages Functions** (Phase 11) that query D1 directly — no static
JSON in the deploy path. Frontend code is unchanged in structure
from the Phase 7 vanilla-JS site; Phase 12 swapped its `fetch()`
URLs from `/data/*.json` to `/api/*` and the rest of the rendering
logic carries through.

Frontend deploys are **operator-triggered** via `npm run deploy`
(which calls `wrangler pages deploy public --project-name=dota-deals
--branch=main`). The Pages dashboard's Git auto-deploy is
deliberately off — recurring build-token invalidation made the
manual path more reliable.

The scheduled cadence:

| Time (UTC) | What runs |
|---|---|
| `00:00` | wrangler d1 migrate + universe refresh + ingest |
| `08:00` | wrangler d1 migrate + ingest |
| `16:00` | wrangler d1 migrate + ingest + signals + score |

Manual runs via the Actions tab; a `skip_ingest` toggle lets you re-run
signals + score against existing data after a fix.

## Status

**v1 shipped, migration to Cloudflare D1 + Worker API complete at
the storage and frontend-fetch layer.** Five pipeline stages
implemented and running against D1 on the scheduled cron. Five
Pages Functions endpoints serving the frontend at `/api/*`.
Frontend's `fetch()` calls point at the API as of Phase 12. The
respx-mocked test suite exercises the full pipeline; real-Steam +
real-D1 smoke tests at each cutover phase caught the bugs no mock
could (Steam React-SSR endpoint rename, D1 batch wire shape, D1
100-variable limit, Pages 25-MiB-per-file deploy limit, recurring
build-token invalidation).

Warmup is real: from a cold start, the first 14 days produce no signals,
days 14–29 produce supply-only signals, and `event_proximity` stays
category-fallback-only until the second TI cycle of operation.

## What's not built

Post-v1: hero-name parsing, backtesting harness (success criterion #3),
expanded item categories (couriers, sets, treasures), CS:GO/CS2
expansion.
