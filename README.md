# dota-deals

Buy-signal analytics for the Steam Community Market — Dota 2 arcanas and immortals.

The Steam Market shows a price chart and nothing else. dota-deals shows *why* an
item is a good buy right now: priced below its own 90-day baseline, supply
contracting, or category historically appreciates before a known event. Every
score exposes its component signals so you can disagree intelligently.

> **Status:** alpha. Scaffold only. The pipeline does not yet make real Steam
> requests or persist real data. See [docs/SPEC.md](docs/SPEC.md) and
> [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.

## What's in v1

- Async ingestion of price + listing snapshots from the Steam Community Market
  (every 8 hours).
- Four signals, computed nightly, weighted into a composite buy score:
  `price_zscore`, `supply_velocity`, `event_proximity`, `comparables_delta`.
- A JSON file + stdout report with the top 20 candidates and a one-line reason
  per pick.
- Forward-fill only — no backfill. From a cold start the pipeline produces no
  scores for the first 30 days; partial scores thereafter. See "Signal warmup"
  in [docs/SPEC.md](docs/SPEC.md).

## What's not in v1

- No ML price prediction. Signals are statistical and transparent.
- No web UI. Output is a JSON file; a frontend is a separate project.
- No CS:GO/CS2, no item categories beyond arcanas and immortals.

## Local development

Requires **Python 3.12** (pinned `>=3.12,<3.13`).

```bash
python -m venv .venv
. .venv/bin/activate           # or: .venv\Scripts\activate on Windows
pip install -e ".[dev]"

make check                     # lint + format check + typecheck + tests
```

Available `make` targets:

| Target | What it does |
|---|---|
| `install` | `pip install -e ".[dev]"` |
| `lint` | `ruff check .` |
| `typecheck` | `mypy src tests` |
| `test` | `pytest` |
| `check` | All of the above |
| `run` | Placeholder — the CLI is not yet wired up |

## Configuration

Copy `.env.example` to `.env` and edit. No secrets are required for v1 — Steam
Market endpoints are public.

## Project layout

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for module boundaries, the SQLite
schema, the error handling table, and the concurrency model.
