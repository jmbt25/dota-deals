-- 0001_initial.sql — first D1 migration for dota-deals.
--
-- This is the D1 port of the SQLite schema in src/dota_deals/storage/schema.sql.
-- D1 is SQLite-flavored so most of the DDL transfers verbatim. Two notable
-- divergences from the local schema:
--
-- 1. The `v_daily_price` view is gone. It depended on a user-registered
--    Python MEDIAN aggregate that D1 (running on remote SQLite) has no way
--    to evaluate. The daily-price median computation moves to Python in the
--    repository layer (Phase 9b): the runner pulls raw price_history rows
--    grouped by date and computes the per-day median in-process. The cost
--    is negligible at v1 scale (≤ 800 items × ≤ 3 obs/day × 90-day window
--    is well under 220k rows, and the median runs at the boundary into the
--    DataLookup cache, not inside any per-signal hot loop).
--
-- 2. `IF NOT EXISTS` clauses are kept on every CREATE so the migration is
--    safely idempotent — wrangler tolerates rerunning the same migration
--    against a non-empty database, and that's how we want it for local
--    fresh-clone bootstrap.
--
-- Apply with: `wrangler d1 migrations apply <DATABASE_NAME>`
-- See docs/D1_MIGRATION.md for the full operational story.

-- Items being tracked. Universe is built once and refreshed weekly.
CREATE TABLE IF NOT EXISTS items (
    item_id                INTEGER PRIMARY KEY AUTOINCREMENT,
    market_hash            TEXT NOT NULL UNIQUE,
    name                   TEXT NOT NULL,
    category               TEXT NOT NULL CHECK (category IN ('arcana', 'immortal')),
    hero                   TEXT,
    first_seen_at          TEXT NOT NULL,
    last_seen_at           TEXT,
    active                 INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    consecutive_ingest_4xx INTEGER NOT NULL DEFAULT 0 CHECK (consecutive_ingest_4xx >= 0)
);

CREATE INDEX IF NOT EXISTS ix_items_category ON items(category);
CREATE INDEX IF NOT EXISTS ix_items_hero ON items(hero);

-- Per-poll price observations. All prices are INTEGER cents (USD).
CREATE TABLE IF NOT EXISTS price_history (
    item_id        INTEGER NOT NULL,
    observed_at    TEXT NOT NULL,
    lowest_cents   INTEGER NOT NULL CHECK (lowest_cents > 0),
    median_cents   INTEGER CHECK (median_cents IS NULL OR median_cents > 0),
    volume_24h     INTEGER CHECK (volume_24h IS NULL OR volume_24h >= 0),
    PRIMARY KEY (item_id, observed_at),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

CREATE INDEX IF NOT EXISTS ix_price_observed_at ON price_history(observed_at);

-- Per-poll listing counts.
CREATE TABLE IF NOT EXISTS listing_history (
    item_id        INTEGER NOT NULL,
    observed_at    TEXT NOT NULL,
    listings_count INTEGER NOT NULL CHECK (listings_count >= 0),
    PRIMARY KEY (item_id, observed_at),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

CREATE INDEX IF NOT EXISTS ix_listing_observed_at ON listing_history(observed_at);

-- Latest observation per item. Cache; authoritative source is price_history.
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
    start_date     TEXT NOT NULL,
    end_date       TEXT,
    confidence     TEXT NOT NULL DEFAULT 'confirmed' CHECK (confidence IN ('confirmed', 'tentative')),
    notes          TEXT
);

CREATE INDEX IF NOT EXISTS ix_events_start_date ON events(start_date);

-- Computed signal values, one row per item per signal per date.
CREATE TABLE IF NOT EXISTS signals (
    item_id        INTEGER NOT NULL,
    computed_for   TEXT NOT NULL,
    signal_name    TEXT NOT NULL CHECK (signal_name IN (
                       'price_zscore', 'supply_velocity',
                       'event_proximity', 'comparables_delta'
                   )),
    value          REAL,
    metadata_json  TEXT,
    PRIMARY KEY (item_id, computed_for, signal_name),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

CREATE INDEX IF NOT EXISTS ix_signals_computed_for ON signals(computed_for);
CREATE INDEX IF NOT EXISTS ix_signals_name_computed_for ON signals(signal_name, computed_for);

-- Composed buy scores per item per date.
CREATE TABLE IF NOT EXISTS scores (
    item_id           INTEGER NOT NULL,
    computed_for      TEXT NOT NULL,
    buy_score         REAL NOT NULL CHECK (buy_score >= -1.0 AND buy_score <= 1.0),
    components_json   TEXT NOT NULL,
    explanation       TEXT NOT NULL,
    data_quality_json TEXT,
    PRIMARY KEY (item_id, computed_for),
    FOREIGN KEY (item_id) REFERENCES items(item_id)
);

CREATE INDEX IF NOT EXISTS ix_scores_computed_for ON scores(computed_for);

-- Records that failed validation. Never silently dropped.
CREATE TABLE IF NOT EXISTS quarantine (
    quarantine_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT NOT NULL,
    source         TEXT NOT NULL,
    item_hash      TEXT,
    raw_payload    TEXT NOT NULL,
    error_type     TEXT NOT NULL,
    error_message  TEXT NOT NULL,
    quarantined_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_quarantine_run_id ON quarantine(run_id);

-- One row per pipeline-stage run. Stages of one CLI invocation share parent_run_id.
CREATE TABLE IF NOT EXISTS runs (
    run_id            TEXT PRIMARY KEY,
    parent_run_id     TEXT,
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
