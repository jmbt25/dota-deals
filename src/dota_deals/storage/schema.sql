-- dota-deals SQLite schema. Single source of truth — keep in sync with
-- docs/ARCHITECTURE.md. All prices are INTEGER cents (USD).

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
