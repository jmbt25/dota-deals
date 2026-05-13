-- Baseline test data for the dota-deals Worker API tests.
--
-- Each test starts from a freshly-migrated, freshly-seeded D1
-- (via tests/setup.ts::seedBaseline). Per-test overrides go in the
-- test body using explicit INSERTs / UPDATEs.
--
-- Shape conventions (matched to the migration's CHECK constraints):
--
--   * Timestamps as ISO 8601 with explicit ``+00:00`` offset, the form
--     the Python pipeline writes. The API layer's ``isoUtc()`` helper
--     normalises these to ``Z`` on the wire, so test assertions that
--     compare wire-format datetimes must use the ``Z`` form.
--   * Dates as YYYY-MM-DD.
--   * Prices as INTEGER cents — wire conversion happens at the API
--     boundary, never in storage.
--
-- Pinned date is 2026-05-13. The seed gives:
--
--   * 4 items: two arcanas with full history, one immortal with
--     full history, one inactive item with nothing.
--   * 3 days of 8-hourly price+listing observations for items 1-3.
--   * `signals` rows for 2026-05-13 with a representative mix of
--     numeric and null values.
--   * `scores` for items 1 and 2 on 2026-05-13 (item 3 has too many
--     null signals → no score, exercising the "scored fewer than
--     all items" path).
--   * `runs` covering ingest/universe/signals/scoring with mixed
--     statuses so health/runs tests can verify status precedence.
--   * `latest_observation` cached for the three items with data.

-- ---- items ----

INSERT INTO items (item_id, market_hash, name, category, hero, first_seen_at, last_seen_at, active)
VALUES
  (1, 'Inscribed Manifold Paradox', 'Inscribed Manifold Paradox', 'arcana', 'Phantom Assassin',
   '2026-04-13T00:00:00+00:00', '2026-05-13T00:00:00+00:00', 1),
  (2, 'Demon Eater', 'Demon Eater', 'arcana', 'Shadow Fiend',
   '2026-04-13T00:00:00+00:00', '2026-05-13T00:00:00+00:00', 1),
  (3, 'Bones of Anggelos', 'Bones of Anggelos', 'immortal', NULL,
   '2026-04-13T00:00:00+00:00', '2026-05-13T00:00:00+00:00', 1),
  (4, 'Discontinued Bundle', 'Discontinued Bundle', 'arcana', NULL,
   '2026-04-13T00:00:00+00:00', '2026-04-13T00:00:00+00:00', 0);

-- ---- price_history (3 days x 3 obs per day = 9 rows per item) ----
-- Lowest_cents progression chosen so per-day medians are non-trivial:
--   item 1 day 13: [320, 330, 340] -> median 330
--   item 2 day 13: [510, 500, 490] -> median 500 (even sort: 490,500,510)
--   item 3 day 13: [101, 100, 99]  -> median 100

INSERT INTO price_history (item_id, observed_at, lowest_cents, median_cents, volume_24h)
VALUES
  -- item 1
  (1, '2026-05-11T00:00:00+00:00', 350, 355, 12),
  (1, '2026-05-11T08:00:00+00:00', 345, 349, 10),
  (1, '2026-05-11T16:00:00+00:00', 340, 345, 8),
  (1, '2026-05-12T00:00:00+00:00', 338, 340, 15),
  (1, '2026-05-12T08:00:00+00:00', 335, 338, 13),
  (1, '2026-05-12T16:00:00+00:00', 332, 335, 11),
  (1, '2026-05-13T00:00:00+00:00', 320, 325, 20),
  (1, '2026-05-13T08:00:00+00:00', 330, 332, 18),
  (1, '2026-05-13T16:00:00+00:00', 340, 342, 14),
  -- item 2
  (2, '2026-05-11T00:00:00+00:00', 520, 525, 5),
  (2, '2026-05-11T08:00:00+00:00', 515, 520, 4),
  (2, '2026-05-11T16:00:00+00:00', 510, 515, 6),
  (2, '2026-05-12T00:00:00+00:00', 505, 510, 7),
  (2, '2026-05-12T08:00:00+00:00', 500, 505, 5),
  (2, '2026-05-12T16:00:00+00:00', 495, 500, 3),
  (2, '2026-05-13T00:00:00+00:00', 510, 515, 9),
  (2, '2026-05-13T08:00:00+00:00', 500, 505, 7),
  (2, '2026-05-13T16:00:00+00:00', 490, 495, 8),
  -- item 3
  (3, '2026-05-11T00:00:00+00:00', 110, 115, 30),
  (3, '2026-05-11T08:00:00+00:00', 108, 112, 25),
  (3, '2026-05-11T16:00:00+00:00', 106, 110, 22),
  (3, '2026-05-12T00:00:00+00:00', 105, 108, 28),
  (3, '2026-05-12T08:00:00+00:00', 103, 106, 27),
  (3, '2026-05-12T16:00:00+00:00', 102, 105, 25),
  (3, '2026-05-13T00:00:00+00:00', 101, 103, 33),
  (3, '2026-05-13T08:00:00+00:00', 100, 102, 30),
  (3, '2026-05-13T16:00:00+00:00', 99, 101, 28);

-- ---- listing_history ----

INSERT INTO listing_history (item_id, observed_at, listings_count)
VALUES
  (1, '2026-05-11T00:00:00+00:00', 50),
  (1, '2026-05-11T08:00:00+00:00', 48),
  (1, '2026-05-11T16:00:00+00:00', 47),
  (1, '2026-05-12T00:00:00+00:00', 45),
  (1, '2026-05-12T08:00:00+00:00', 44),
  (1, '2026-05-12T16:00:00+00:00', 42),
  (1, '2026-05-13T00:00:00+00:00', 40),
  (1, '2026-05-13T08:00:00+00:00', 39),
  (1, '2026-05-13T16:00:00+00:00', 38),
  (2, '2026-05-11T00:00:00+00:00', 15),
  (2, '2026-05-11T08:00:00+00:00', 14),
  (2, '2026-05-11T16:00:00+00:00', 13),
  (2, '2026-05-12T00:00:00+00:00', 12),
  (2, '2026-05-12T08:00:00+00:00', 11),
  (2, '2026-05-12T16:00:00+00:00', 10),
  (2, '2026-05-13T00:00:00+00:00', 9),
  (2, '2026-05-13T08:00:00+00:00', 8),
  (2, '2026-05-13T16:00:00+00:00', 7),
  (3, '2026-05-11T00:00:00+00:00', 100),
  (3, '2026-05-11T08:00:00+00:00', 98),
  (3, '2026-05-11T16:00:00+00:00', 97),
  (3, '2026-05-12T00:00:00+00:00', 95),
  (3, '2026-05-12T08:00:00+00:00', 93),
  (3, '2026-05-12T16:00:00+00:00', 92),
  (3, '2026-05-13T00:00:00+00:00', 90),
  (3, '2026-05-13T08:00:00+00:00', 88),
  (3, '2026-05-13T16:00:00+00:00', 87);

-- ---- latest_observation (cache; matches the newest observed_at per item) ----

INSERT INTO latest_observation (item_id, observed_at, lowest_cents, listings_count)
VALUES
  (1, '2026-05-13T16:00:00+00:00', 340, 38),
  (2, '2026-05-13T16:00:00+00:00', 490, 7),
  (3, '2026-05-13T16:00:00+00:00', 99, 87);

-- ---- events (one upcoming) ----

INSERT INTO events (event_id, kind, name, start_date, end_date, confidence, notes)
VALUES
  (1, 'ti', 'The International 2026', '2026-08-15', '2026-08-25', 'confirmed', NULL);

-- ---- signals (one row per (item, signal_name) on 2026-05-13) ----
-- Item 1: all four numeric.
-- Item 2: two numeric, two null (insufficient peer history).
-- Item 3: three null (insufficient history overall) — too many nulls
--          to compose a score per SPEC.md's "3+ nulls → no score" rule.

INSERT INTO signals (item_id, computed_for, signal_name, value, metadata_json)
VALUES
  (1, '2026-05-13', 'price_zscore',      0.55, NULL),
  (1, '2026-05-13', 'supply_velocity',   0.42, NULL),
  (1, '2026-05-13', 'event_proximity',   0.10, '{"fallback": "category-based"}'),
  (1, '2026-05-13', 'comparables_delta', 0.15, NULL),
  (2, '2026-05-13', 'price_zscore',      0.30, NULL),
  (2, '2026-05-13', 'supply_velocity',   0.25, NULL),
  (2, '2026-05-13', 'event_proximity',   NULL, '{"reason": "no_event_in_window"}'),
  (2, '2026-05-13', 'comparables_delta', NULL, '{"reason": "too_few_peers"}'),
  (3, '2026-05-13', 'price_zscore',      NULL, '{"reason": "insufficient_history"}'),
  (3, '2026-05-13', 'supply_velocity',   NULL, '{"reason": "insufficient_history"}'),
  (3, '2026-05-13', 'event_proximity',   NULL, '{"reason": "no_event_in_window"}'),
  (3, '2026-05-13', 'comparables_delta', 0.05, NULL);

-- ---- scores (only items 1 and 2 cleared the nulls threshold) ----

INSERT INTO scores (item_id, computed_for, buy_score, components_json, explanation, data_quality_json)
VALUES
  (1, '2026-05-13', 0.42,
   '{"price_zscore": 0.55, "supply_velocity": 0.42, "event_proximity": 0.10, "comparables_delta": 0.15}',
   'priced 1.5 std below 90d median; supply contracting',
   '{"null_signals": []}'),
  (2, '2026-05-13', 0.20,
   '{"price_zscore": 0.30, "supply_velocity": 0.25, "event_proximity": null, "comparables_delta": null}',
   'discount vs own baseline; weights renormalized over remaining signals',
   '{"null_signals": ["event_proximity", "comparables_delta"]}');

-- ---- runs (cover ingest/universe/signals/scoring with mixed statuses) ----
-- Two ingest runs: yesterday partial, today success. Health endpoint
-- distinguishes these to surface 'degraded' when today's run was
-- partial.

INSERT INTO runs (run_id, parent_run_id, kind, started_at, finished_at, status,
                  items_ok, items_quarantined, items_failed, notes)
VALUES
  ('run-universe-1', 'parent-2026-05-01', 'universe',
   '2026-05-01T00:00:00+00:00', '2026-05-01T00:01:30+00:00',
   'success', 4, 0, 0, NULL),
  ('run-ingest-yesterday', 'parent-2026-05-12', 'ingest',
   '2026-05-12T16:00:00+00:00', '2026-05-12T16:02:00+00:00',
   'partial', 2, 0, 1, '1 item 4xx''d'),
  ('run-ingest-today', 'parent-2026-05-13', 'ingest',
   '2026-05-13T16:00:00+00:00', '2026-05-13T16:02:00+00:00',
   'success', 3, 0, 0, NULL),
  ('run-signals-today', 'parent-2026-05-13', 'signals',
   '2026-05-13T16:03:00+00:00', '2026-05-13T16:03:45+00:00',
   'success', 3, 0, 0, NULL),
  ('run-scoring-today', 'parent-2026-05-13', 'scoring',
   '2026-05-13T16:04:00+00:00', '2026-05-13T16:04:10+00:00',
   'success', 2, 0, 1, '1 item excluded: too many null signals');

-- ---- quarantine (a single row so the table isn't empty for ops tests) ----

INSERT INTO quarantine (run_id, source, item_hash, raw_payload, error_type,
                        error_message, quarantined_at)
VALUES
  ('run-ingest-yesterday', 'steam_price_overview', 'Mysterious Item',
   '{"success": true, "lowest_price": "garbage"}', 'ValidationError',
   'lowest_price could not be parsed', '2026-05-12T16:00:30+00:00');
