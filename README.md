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

The operational details (recompute recipes, data-quality fields,
warmup windows, failure modes in the logs) live in
[`docs/INGESTION.md`](docs/INGESTION.md), [`docs/UNIVERSE.md`](docs/UNIVERSE.md),
[`docs/SIGNALS.md`](docs/SIGNALS.md), and
[`docs/SCORING.md`](docs/SCORING.md).

### Frontend

The single-page frontend at [`public/index.html`](public/index.html) is
a vanilla-JS static site that `fetch()`es `data/*.json` siblings — no
build step, no framework, no API layer. The pipeline writes the JSON
files; Cloudflare Pages (Phase 8) serves them.

`fetch()` from `file://` is blocked by browsers, so serve `public/` over
HTTP locally:

```bash
cd public/
python -m http.server 8000
# visit http://localhost:8000
```

The frontend has five explicit states — LOADING, WARMUP, OPERATIONAL,
DEGRADED, ERROR — driven by `health.json.status` and `latest.json.scores`.
Hand-crafted JSON fixtures in
[`public/data/fixtures/`](public/data/fixtures/) let you exercise each
state without running the pipeline:

```bash
# operational
cp public/data/fixtures/latest-operational.json public/data/latest.json
cp public/data/fixtures/health-operational.json public/data/health.json
mkdir -p public/data/items
cp public/data/fixtures/items/1.json             public/data/items/1.json

# warmup (cold-start, no scores yet)
cp public/data/fixtures/latest-warmup.json   public/data/latest.json
cp public/data/fixtures/health-warmup.json   public/data/health.json

# degraded (ingest ran partial)
cp public/data/fixtures/latest-degraded.json public/data/latest.json
cp public/data/fixtures/health-degraded.json public/data/health.json

# error: delete or rename one of those files — the fetch will 404 and
# the frontend renders the error state with a Retry button
```

Refresh the browser after each swap. The wire-format contract is documented
in [`docs/PUBLISH.md`](docs/PUBLISH.md); the fixtures conform to it.

## Deployment

The system runs unattended on **GitHub Actions** (8-hourly cron in
[.github/workflows/pipeline.yml](.github/workflows/pipeline.yml)),
storing all data in **Cloudflare D1** via the public REST API. The
frontend is hosted on **Cloudflare Pages** at
[dotadeals.com](https://dotadeals.com) and is **intentionally stale
through Phases 10 – 12** of the migration: the pipeline no longer
generates `public/data/` JSON; Phase 11 builds a TypeScript Worker
that reads D1 directly; Phase 12 points the frontend at the Worker.
See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) for the "Known gap"
section that explains this in operational terms.

Full setup — Cloudflare API tokens, GitHub secrets, Pages connection,
the D1 schema migration commands, failure recovery — is in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). The storage-layer
architecture is in [docs/D1_MIGRATION.md](docs/D1_MIGRATION.md).

The scheduled cadence:

| Time (UTC) | What runs |
|---|---|
| `00:00` | wrangler d1 migrate + universe refresh + ingest |
| `08:00` | wrangler d1 migrate + ingest |
| `16:00` | wrangler d1 migrate + ingest + signals + score |

Manual runs via the Actions tab; a `skip_ingest` toggle lets you re-run
signals + score against existing data after a fix.

## Status

**v1 shipped, migration to Cloudflare D1 + Worker API in progress.**
Five pipeline stages implemented and running against D1 on the
scheduled cron. Mid-migration, the frontend is on a planned-stale
window through Phase 12 (see Deployment above and
[docs/D1_MIGRATION.md](docs/D1_MIGRATION.md) for the eight-commit
narrative). The respx-mocked test suite exercises the full pipeline;
real-Steam + real-D1 smoke tests at each cutover phase caught the
bugs no mock could (Steam React-SSR endpoint rename, D1 batch wire
shape, D1 100-variable limit) — see the D1_MIGRATION doc for the
list.

Warmup is real: from a cold start, the first 14 days produce no signals,
days 14–29 produce supply-only signals, and `event_proximity` stays
category-fallback-only until the second TI cycle of operation. SPEC.md
calls this out as the "Signal warmup" trade-off.

## Design docs

- [`docs/SPEC.md`](docs/SPEC.md) — product spec, the four signal formulas,
  the composite buy-score weights, success criteria.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — module boundaries, the
  SQLite schema, the error-handling table, the concurrency model.
- [`docs/SCORING.md`](docs/SCORING.md) — renormalization by example,
  data_quality fields, the `event_proximity = null` convention.

## What's not built

See [`docs/FUTURE.md`](docs/FUTURE.md) for the post-v1 list: hero-name
parsing, backtesting harness (success criterion #3), scheduler /
deployment wiring, web frontend, CS:GO/CS2 expansion. Each entry says
why it's deferred and what would have to be true to start it.
