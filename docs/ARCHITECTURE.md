# dota-deals — Architecture

## Module layout

```
src/dota_deals/
├── __init__.py
├── config.py           # env loading, settings model (Pydantic Settings)
├── logging.py          # structlog setup; bind run_id, source, item_id
├── models/
│   ├── __init__.py
│   ├── market.py       # Pydantic models for Steam Market responses
│   ├── domain.py       # internal domain models (Item, PricePoint, Signal, BuyScore)
│   └── events.py       # Pydantic models for the events table
├── storage/
│   ├── __init__.py
│   ├── db.py           # connection mgmt, schema bootstrap, low-level helpers
│   ├── repositories.py # typed insert/query functions per table
│   └── schema.sql      # CREATE TABLE statements (single source of truth)
├── ingest/
│   ├── __init__.py
│   ├── steam.py        # async httpx client for Steam Market endpoints
│   ├── runner.py       # orchestrator: fan out, validate, persist, summarize
│   └── universe.py     # builds the list of items to track (arcanas + immortals)
├── signals/
│   ├── __init__.py
│   ├── price_zscore.py
│   ├── supply_velocity.py
│   ├── event_proximity.py
│   ├── comparables.py
│   └── runner.py       # computes all signals for all items for a given date
├── scoring/
│   ├── __init__.py
│   └── buy_score.py    # composite score, null handling, ranking
├── notifier/
│   ├── __init__.py
│   ├── stdout.py       # human-readable report
│   └── json_file.py    # machine-readable output for downstream frontend
└── cli/
    ├── __init__.py
    └── main.py         # Typer entry point: universe, ingest, signals, score, report
```

**Dependency rule.** A module may depend on any module to its left or above in
the natural pipeline order: `config` and `logging` are leaves; `models` is shared
ground; `storage` depends on models; `ingest` depends on models + storage;
`signals` depends on models + storage; `scoring` depends on models + storage +
signals; `notifier` depends on models + storage + scoring; `cli` depends on
everything. A module may not import from a module downstream of it.

## Public surface per module

- **`config`** exports `Settings` (Pydantic Settings model) and `load_settings()`.
  Reads from `.env`. No other module reads env vars directly. Fields include
  `db_path: Path`, `steam_concurrency: int = 2`, `request_timeout_s: float = 15.0`,
  `cooldown_429_s: float = 60.0`, `ingest_cadence_hours: int = 8`,
  `steam_currency_id: int = 1` (USD), `steam_country: str = "US"`,
  `log_format: Literal["console", "json"] = "console"`.
- **`logging`** exports `configure_logging(run_id: str, log_format: str) -> None`
  and `get_logger(name: str)`. Output goes to **stderr**. `log_format="console"`
  renders human-readable output; `log_format="json"` renders one JSON object per
  line. Bind context with `logger.bind(item_id=...)`.
- **`models.market`** exports `SteamPriceOverview`, `SteamListingsResponse`.
  These are wire-format models that strictly validate Steam's responses. Price
  fields parse Steam's localized currency strings (e.g., `"$3.45"`) into
  `int` cents at the model boundary.
- **`models.domain`** exports `Item`, `PricePoint`, `ListingPoint`, `Signal`,
  `BuyScore`, `RunSummary`. All money-typed fields are `int` (cents). These are
  internal types.
- **`storage.db`** exports `connect(path: Path) -> Connection`,
  `bootstrap_schema(conn) -> None`.
- **`storage.repositories`** exports typed functions:
  `insert_price_point(conn, point) -> None`,
  `upsert_latest_observation(conn, item_id, point) -> None`,
  `quarantine_record(conn, ...) -> None`,
  `recent_prices(conn, item_id, days) -> list[PricePoint]`,
  `insert_run(conn, run) -> None`, `update_run(conn, run_id, ...) -> None`, etc.
  All raise domain-specific exceptions, never `sqlite3.*` to callers.
- **`ingest.steam`** exports `SteamMarketClient` (async context manager) with
  methods `fetch_price_overview(item_name) -> SteamPriceOverview` and
  `fetch_listings(item_name) -> SteamListingsResponse`. The client always sends
  `currency=settings.steam_currency_id&country=settings.steam_country` so prices
  are returned in USD.
- **`ingest.universe`** exports
  `refresh_universe(settings, run_id, parent_run_id=None) -> RunSummary`.
  Discovers arcanas and immortals by paging
  `https://steamcommunity.com/market/search/render?appid=570` with category-tag
  filters; upserts rows into `items`, updating `last_seen_at`. Items not seen in
  3 consecutive universe refreshes have `active` flipped to 0.
- **`ingest.runner`** exports `run_ingestion(items: list[str], settings, run_id:
  str, parent_run_id: str | None = None) -> RunSummary`.
- **`signals.runner`** exports `compute_signals_for(date: date, settings, run_id:
  str, parent_run_id: str | None = None) -> RunSummary`.
- **`scoring.buy_score`** exports `compute_buy_score(signals: list[Signal]) ->
  BuyScore | None` and `rank_top_n(scores, n) -> list[BuyScore]`.
- **`notifier.*`** exports `emit(scores: list[BuyScore], data_quality: dict,
  dest: Path | None) -> None`. The JSON emitter writes a `data_quality` block
  alongside the scores; see "Run-id lifecycle".

## Data flow (end to end)

```
CLI / scheduler
│
▼
ingest.runner.run_ingestion(items)
│
├──> ingest.steam (async, semaphore-bounded HTTP)
│         │
│         ▼
│    raw JSON responses
│         │
│         ▼
│    models.market validation (Pydantic strict)
│         │
│   ┌─────┴─────┐
│   │           │
│  valid     invalid
│   │           │
│   ▼           ▼
│ storage    storage
│ price_     quarantine
│ history    +
│ +          (counted in run)
│ latest_observation
│
▼
signals.runner.compute_signals_for(date)
│
├──> reads price_history, listing_history, events, items
├──> each signal module computes one signal per item
▼
storage.signals (one row per item per signal per date)
│
▼
scoring.buy_score.compute + rank_top_n
│
▼
notifier (stdout report + JSON file with data_quality block)
```

Each arrow is a function call boundary that's independently testable. No module
reaches across the diagram.

## SQLite schema

```sql
-- Items being tracked. Universe is built once and refreshed weekly.
CREATE TABLE IF NOT EXISTS items (
    item_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    market_hash    TEXT NOT NULL UNIQUE,                          -- Steam's market_hash_name
    name           TEXT NOT NULL,
    category       TEXT NOT NULL CHECK (category IN ('arcana', 'immortal')),
    hero           TEXT,                                           -- nullable; not every item ties to a hero
    first_seen_at  TEXT NOT NULL,                                  -- ISO-8601 UTC
    last_seen_at   TEXT,                                           -- updated each universe refresh
    active         INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE INDEX IF NOT EXISTS ix_items_category ON items(category);
CREATE INDEX IF NOT EXISTS ix_items_hero ON items(hero);

-- Per-poll price observations. All prices are INTEGER cents (USD).
CREATE TABLE IF NOT EXISTS price_history (
    item_id        INTEGER NOT NULL,
    observed_at    TEXT NOT NULL,                                  -- ISO-8601 UTC, truncated to the polling slot
    lowest_cents   INTEGER NOT NULL CHECK (lowest_cents > 0),
    median_cents   INTEGER CHECK (median_cents IS NULL OR median_cents > 0),
    volume_24h     INTEGER CHECK (volume_24h IS NULL OR volume_24h >= 0),
    PRIMARY KEY (item_id, observed_at),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

CREATE INDEX IF NOT EXISTS ix_price_observed_at ON price_history(observed_at);

-- Per-poll listing counts. Separate from price because cadence and source can differ.
CREATE TABLE IF NOT EXISTS listing_history (
    item_id        INTEGER NOT NULL,
    observed_at    TEXT NOT NULL,
    listings_count INTEGER NOT NULL CHECK (listings_count >= 0),
    PRIMARY KEY (item_id, observed_at),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

CREATE INDEX IF NOT EXISTS ix_listing_observed_at ON listing_history(observed_at);

-- Latest observation per item. Maintained alongside price_history/listing_history
-- on each ingest. Used by Signal 4 (comparables) and the notifier to avoid
-- scanning history for "what's the current price". Authoritative source remains
-- price_history; this is a cache.
CREATE TABLE IF NOT EXISTS latest_observation (
    item_id        INTEGER PRIMARY KEY,
    observed_at    TEXT NOT NULL,
    lowest_cents   INTEGER NOT NULL CHECK (lowest_cents > 0),
    listings_count INTEGER CHECK (listings_count IS NULL OR listings_count >= 0),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

-- Hand-curated Dota events.
CREATE TABLE IF NOT EXISTS events (
    event_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    kind           TEXT NOT NULL CHECK (kind IN ('ti', 'treasure_release', 'major_patch', 'frostivus', 'crownfall')),
    name           TEXT NOT NULL,
    start_date     TEXT NOT NULL,                                  -- ISO-8601 date
    end_date       TEXT,
    confidence     TEXT NOT NULL DEFAULT 'confirmed' CHECK (confidence IN ('confirmed', 'tentative')),
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS ix_events_start_date ON events(start_date);

-- Computed signal values, one row per item per signal per date.
CREATE TABLE IF NOT EXISTS signals (
    item_id        INTEGER NOT NULL,
    computed_for   TEXT NOT NULL,                                  -- ISO-8601 date the signal pertains to
    signal_name    TEXT NOT NULL CHECK (signal_name IN (
                       'price_zscore', 'supply_velocity',
                       'event_proximity', 'comparables_delta'
                   )),
    value          REAL,                                           -- NULL allowed; meaning "not computable today"
    metadata_json  TEXT,                                           -- e.g. {"fallback": "category-based"}
    PRIMARY KEY (item_id, computed_for, signal_name),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

CREATE INDEX IF NOT EXISTS ix_signals_computed_for ON signals(computed_for);
CREATE INDEX IF NOT EXISTS ix_signals_name_computed_for ON signals(signal_name, computed_for);

-- Records that failed validation. Never silently dropped.
CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    source         TEXT NOT NULL,                                  -- 'steam_price_overview' | 'steam_listings' | ...
    item_hash      TEXT,                                           -- when known
    raw_payload    TEXT NOT NULL,
    error_type     TEXT NOT NULL,
    error_message  TEXT NOT NULL,
    quarantined_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_quarantine_run_id ON quarantine(run_id);

-- One row per pipeline-stage run. Stages of one CLI invocation share parent_run_id.
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,                            -- UUID4
    parent_run_id     TEXT,                                        -- UUID4 shared across stages of one CLI invocation
    kind              TEXT NOT NULL CHECK (kind IN ('ingest', 'universe', 'signals', 'scoring', 'notify')),
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    status            TEXT NOT NULL CHECK (status IN ('running', 'success', 'partial', 'failed')),
    items_ok          INTEGER NOT NULL DEFAULT 0,
    items_quarantined INTEGER NOT NULL DEFAULT 0,
    items_failed      INTEGER NOT NULL DEFAULT 0,
    notes             TEXT
);

CREATE INDEX IF NOT EXISTS ix_runs_kind_started_at ON runs(kind, started_at);
CREATE INDEX IF NOT EXISTS ix_runs_parent_run_id ON runs(parent_run_id);
```

The primary keys on `price_history`, `listing_history`, and `signals` enforce
idempotency directly at the DB level. Re-running ingestion for the same item at
the same `observed_at` is a no-op via `INSERT OR IGNORE` (or
`ON CONFLICT DO NOTHING`). `latest_observation` uses upsert (`INSERT ... ON
CONFLICT(item_id) DO UPDATE`) so the cache always reflects the newest seen row.

### Observed-at truncation

Every ingestion poll writes its observations with `observed_at` truncated to the
**polling slot**, not the wall-clock hour. For the default 8-hourly cadence the
slots are 00:00, 08:00, 16:00 UTC; for a 6-hourly cadence the slots are 00:00,
06:00, 12:00, 18:00 UTC. The slot is computed as
`floor(now.hour / cadence_hours) * cadence_hours`, then minute/second/microsecond
zeroed. Because `(item_id, observed_at)` is a primary key with idempotent inserts,
this guarantees that an accidental rerun within the same slot (e.g., a retry
after a partial Steam outage) is a no-op rather than a duplicate. The cadence
must divide 24 evenly — enforced in `Settings`.

### Daily price derivation

Signal 1 (`price_zscore`) operates on a daily price series, not raw 8-hourly poll
observations. The "daily price" for an item on a UTC date is the median of all
`lowest_cents` rows in `price_history` for that item on that date (typically
three observations per day at v1 cadence). The implementation reads this through
a parameterized SQL query in `storage.repositories.daily_prices(item_id, days)`;
it is not persisted. This insulates signal math from cadence changes.

### Run-id lifecycle

Every CLI invocation generates a `parent_run_id` (UUID4). Each pipeline stage
(`universe`, `ingest`, `signals`, `scoring`, `notify`) generates its own `run_id`
(UUID4) and writes a row to `runs` with both set. A nightly batch is thus
queryable end-to-end with `SELECT * FROM runs WHERE parent_run_id = ?`. The
notifier reads the most recent batch via `parent_run_id` to populate the
`data_quality` block in its output.

## Error handling policy by layer

| Layer | Exception type | Disposition |
|---|---|---|
| `ingest.steam` | `httpx.TimeoutException` | Retried with exponential backoff + jitter (3 attempts). Logged at WARNING with attempt count. After 3 failures: surface as `IngestError` to runner. |
| `ingest.steam` | `httpx.HTTPStatusError` 429 | Retried with *longer* backoff (start at 30s, double each attempt, max 4 attempts). Logged at WARNING. |
| `ingest.steam` | `httpx.HTTPStatusError` 5xx | Retried (3 attempts, exponential backoff). Logged at WARNING. |
| `ingest.steam` | `httpx.HTTPStatusError` 4xx (non-429) | Not retried. Logged at ERROR with status and item name. Surfaced as `IngestError`. 3xx redirects (e.g., to login) are treated as 4xx — Steam should not redirect for our public endpoints. |
| `ingest.steam` | `httpx.RequestError` (other transport) | Retried (3 attempts). Then surfaced. |
| `ingest.steam` | `json.JSONDecodeError` | Not retried. Quarantined with raw payload. |
| `ingest.steam` | `asyncio.CancelledError` | Propagated. The runner's SIGINT path handles cleanup. |
| `ingest.runner` | `pydantic.ValidationError` | Quarantined with raw payload and validation error message. Run continues. |
| `ingest.runner` | `IngestError` | Counted in `items_failed` for the run. Run continues. |
| `storage.*` | `sqlite3.IntegrityError` (uniqueness) | Treated as expected (idempotent re-run). Logged at DEBUG. |
| `storage.*` | `sqlite3.IntegrityError` (FK) | Logged at ERROR with context. Raised to runner. |
| `storage.*` | `sqlite3.OperationalError` | Logged at ERROR. Raised to runner; aborts the run (DB problem is not safe to ignore). |
| `signals.*` | `ValueError` (insufficient data) | Signal value is null for that item that day. Logged at INFO with reason. |
| `signals.*` | unexpected `Exception` | Logged at ERROR with full traceback and item_id. Signal is null. Run continues. |
| `scoring.*` | All-null signals for an item | Item excluded from ranking. Counted in run summary. |
| `notifier.*` | `OSError` / `PermissionError` (file write) | Logged at ERROR with destination path. Run marked 'failed'. |
| `cli` (top-level) | `KeyboardInterrupt` / `asyncio.CancelledError` | Marks run as 'failed' in `runs` table, exits cleanly with code 130. |
| `cli` (top-level) | unexpected `Exception` | Marks run as 'failed', logs full traceback, exits with code 1. |

The rule: errors are caught at the boundary that knows what to do with them.
Network errors are caught in `ingest.steam`. Validation errors are caught in
`ingest.runner` (because that's where the quarantine table is reachable). DB
integrity errors are caught in `storage` (because that's where the table-level
context lives). Unknown errors bubble to the CLI and abort the run with a
non-zero exit code.

No `except Exception` exists in the codebase except at exactly two boundaries:
the signal computation loop (so one item's bug doesn't kill all signals) and the
CLI top level (so we can log + exit cleanly).

## Concurrency model

- Ingestion uses an `asyncio.Semaphore(value=settings.steam_concurrency)` with a
  default of **2**. Steam Market's unofficial rate tolerance is roughly one
  request per second per IP; we err on the polite side and can dial up after
  observing real behavior.
- v1 ingest cadence is **8-hourly** (`settings.ingest_cadence_hours = 8`), giving
  comfortable headroom against rate limits for an 800-item universe with two
  endpoints per item per poll.
- Each request is wrapped in `asyncio.timeout(settings.request_timeout_s)`
  (default 15s).
- The tenacity retry policy is applied per request, with the semaphore released
  during sleeps so other items can proceed.
- 429 responses are special-cased: in addition to the longer backoff, they
  trigger a global "cool down" — the runner waits `settings.cooldown_429_s`
  (default 60s) before issuing more requests, regardless of which item triggered
  it.
- The runner shuts down cleanly on SIGINT (`KeyboardInterrupt` /
  `asyncio.CancelledError`): in-flight requests finish or time out, then the run
  is marked 'partial' or 'failed' and saved.

## Testing strategy

| Layer | Test type | Tools |
|---|---|---|
| `models.*` | Unit. Verify validation rejects bad inputs and accepts good ones. | pytest |
| `storage.repositories` | Unit + integration with a tmp SQLite file. Verify idempotency: insert same row twice, second is a no-op. Verify FK violations raise. | pytest, tmp_path fixture |
| `ingest.steam` | Unit with mocked HTTP. Test happy path, timeout-then-success, 429 backoff, 4xx no-retry, malformed JSON. | pytest-asyncio, respx |
| `ingest.runner` | Integration with mocked HTTP + real SQLite. Test: valid response persists; invalid response quarantines; partial run records correct counts. | pytest-asyncio, respx, tmp_path |
| `signals.*` | Unit with hand-constructed price/listing histories. Test: insufficient history returns null; flat price returns 0; clear discount returns positive. | pytest, freezegun for date control |
| `scoring.buy_score` | Unit. Test: weights sum correctly; null signal renormalizes; 3+ nulls returns None; ranking is stable. | pytest |
| End-to-end | One smoke test: run ingest + signals + scoring against a fully mocked Steam, verify a JSON report is produced and contains expected items. | pytest-asyncio, respx |

Test coverage target: 85%+ on `signals`, `scoring`, `storage.repositories`, and
the ingest error paths. The happy-path is the easy part; the tests that matter
are the failure ones.

## Out-of-process concerns (not in code yet)

- **Scheduling.** v1 is invoked manually via CLI. Production scheduling (cron,
  systemd timer, GitHub Actions) is a deployment concern, not a code concern.
- **Secrets.** None needed for v1 — Steam Market endpoints are public.
- **Hosting.** Pipeline runs anywhere with Python 3.12+. Output is a JSON file;
  hosting the eventual frontend is a separate decision.