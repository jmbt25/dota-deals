# dota-deals

Buy-signal analytics for the Steam Community Market (Dota 2 arcanas and immortals). Async ingestion pipeline → signal computation → ranked daily picks.

See @docs/SPEC.md for product spec and signal definitions.
See @docs/ARCHITECTURE.md for module layout and data flow.

## Stack

Python 3.12 (pinned `>=3.12,<3.13`) · async/await throughout · httpx · Pydantic v2 (+ pydantic-settings) · Cloudflare D1 over HTTP REST with typed async repositories · Typer (CLI) · structlog · tenacity · pytest + pytest-asyncio + respx · ruff · mypy --strict

See @docs/D1_MIGRATION.md for the storage-layer architecture: HTTP client, D1Connection wrapper with rows-read budget accounting, bulk-read / batch-write repository functions, and the in-memory test fake that applies the same `migrations/0001_initial.sql` schema as production.

## Non-negotiables

- **Type everything.** Every function signature, every Pydantic model field. `mypy --strict` must pass before any commit.
- **Handle errors intentionally.** No bare `except`. Catch specific exceptions (`httpx.TimeoutException`, `httpx.HTTPStatusError`, `pydantic.ValidationError`, `D1QueryError`, `json.JSONDecodeError`, `FileNotFoundError`, `ValueError`). Every caught exception either: re-raises with added context, routes to the dead-letter table, or is logged at WARNING+ with structured context (item_id, source, attempt). Storage-layer integrity violations surface as `IntegrityViolation` from `dota_deals.storage.db`, not raw `D1QueryError` — the repository layer translates at the boundary.
- **Idempotent writes.** Every ingestion write uses `(item_id, observed_at)` uniqueness. Re-running a job must never double-insert.
- **Validate before persist.** Every external response goes through a Pydantic model before touching the DB. Invalid records → quarantine table, never silently dropped.
- **Prices as INTEGER cents.** All persisted prices are INTEGER cents (USD). Conversion to/from `Decimal` USD happens only at the Pydantic boundary (Steam responses in, display output out). Nothing inside the database or domain models touches `float` for money.
- **Structured logging.** Use `structlog`. Output goes to **stderr**. Human-readable rendering by default; set `LOG_FORMAT=json` for line-delimited JSON. Every log line includes `run_id`, `source`, and the entity ID (`item_id` or `event_id`) when applicable. No `print()`. No f-strings inside log calls — pass kwargs so structlog can render them.

## Workflow rules

- **Plan before coding.** For any task touching more than one module, write the plan first (as a comment, scratch file, or chat message) and confirm before implementing.
- **Tests are part of done.** A feature is not complete without tests covering: happy path, one validation failure, one transient failure (network), one persistence edge case (duplicate, missing FK).
- **Small commits, conventional messages.** `feat:`, `fix:`, `refactor:`, `test:`, `docs:`, `chore:`. One logical change per commit.
- **Run before push.** `ruff check . && ruff format --check . && mypy src && pytest` must pass locally.

## Code style

- Module names: lowercase, no underscores where avoidable (`ingest`, `signals`, `scoring`).
- Public functions return concrete types, not `Any`. If you reach for `Any`, stop and reconsider.
- Prefer `dataclasses(frozen=True)` or Pydantic models over dicts for any data crossing a module boundary.
- Async functions: if a function calls `await`, name it accordingly and ensure callers actually await it. No fire-and-forget without `asyncio.create_task` + explicit handling.
- Errors carry context: `raise ValueError(f"Item {item_id} has price={price}, expected > 0")` not `raise ValueError("bad price")`.

## What NOT to do

- Don't add LinkedIn-style scraping. Steam's public market endpoints only; respect rate limits.
- Don't predict prices with ML in v1. Signals are statistical + event-cycle based and transparent.
- Don't store API keys in code or `.env.example`. Use `.env` (gitignored) and document required vars in README.
- Don't write `except Exception:` anywhere. If you genuinely need a catch-all at a task boundary, use `except Exception as e: logger.exception("...", ...)` and re-raise or route to DLQ.
- Don't optimize prematurely. SQLite is fine for v1. Profile before switching.

## When stuck

Re-read @docs/SPEC.md for product intent. Re-read @docs/ARCHITECTURE.md for module boundaries. If the answer isn't there, ask before guessing — silent guesses are the biggest blocker.