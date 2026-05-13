# Signals — operational guide

This is the day-to-day "I'm running the pipeline, what do I do?" reference.
The mathematical definitions live in [docs/SPEC.md](SPEC.md); read that for
*why* each signal exists. Read this for *how to operate it*.

## CLI

```bash
dota-deals signals compute             # today UTC
dota-deals signals compute --date 2026-05-12
dota-deals signals compute -d 2026-05-12
```

Defaults to today's UTC date if `--date` is omitted. The run reads from
`Settings.db_path` (set via `.env` or the env-var `DB_PATH`) and writes both
`signals` rows and one `runs` row tagged `kind='signals'`.

## Inspecting a run

```sql
-- The most recent signals run.
SELECT * FROM runs WHERE kind = 'signals' ORDER BY started_at DESC LIMIT 1;

-- Everything computed on a given date, with metadata.
SELECT item_id, signal_name, value, metadata_json
FROM signals
WHERE computed_for = '2026-05-12'
ORDER BY item_id, signal_name;

-- Per-signal coverage on a given date.
SELECT signal_name,
       COUNT(*) AS total,
       SUM(CASE WHEN value IS NULL THEN 1 ELSE 0 END) AS nulls
FROM signals
WHERE computed_for = '2026-05-12'
GROUP BY signal_name;
```

Every active item gets **four rows** for the run's date, one per signal,
regardless of how much usable data was available. A null `value` with a
`reason` in `metadata_json` is how the runner says "we processed this
(item, signal_name) but couldn't produce a number" — it is **not** a gap
and shouldn't be re-attempted from scratch.

## Recomputing a date

`signals` is idempotent on the `(item_id, computed_for, signal_name)`
primary key. Running for a date that already has signal rows is a no-op
for those rows; missing rows are filled in. A real recompute means
deleting first:

```sql
DELETE FROM signals WHERE computed_for = '2026-05-12';
```

…then re-running:

```bash
dota-deals signals compute --date 2026-05-12
```

## Interpreting null values

`metadata_json.reason` tells you which guard rejected the computation:

| Signal | `reason` | What it means |
|---|---|---|
| `price_zscore` | `insufficient_history` | Fewer than 30 distinct UTC days of price history before the as-of date. Wait for warmup. |
| `price_zscore` | `no_daily_price_for_as_of` | No `price_history` rows on the as-of date itself. Ingest probably hadn't run yet that day. |
| `price_zscore` | `flat_window_stddev_zero` | Window had zero variance; emits `0.0` (not null). |
| `supply_velocity` | `no_listing_history` / `insufficient_history` | Less than 14 days of listing observations. |
| `supply_velocity` | `too_few_recent_observations` / `too_few_reference_observations` | Fewer than 3 observations to take a median over at one of the two endpoints. |
| `supply_velocity` | `reference_count_zero` | The "30 days ago" listings count was zero — formula undefined. |
| `event_proximity` | `no_event_within_60d` | No upcoming event; emits `0.0` (not null). |
| `event_proximity` | `no_past_events_of_kind` | First time we've seen this event kind. Common in v1. |
| `event_proximity` | `insufficient_peers_with_history` | Category fallback couldn't find ≥ 3 peers with past-window data. |
| `comparables_delta` | `no_current_price` | Item has no `latest_observation` row (never ingested cleanly). |
| `comparables_delta` | `insufficient_peers` | Fewer than 3 same-category peers with a current price. |
| any | `computation_exception` | The signal's `compute()` raised something unexpected. `metadata_json.error_type` carries the exception class. **Investigate the logs.** |

`computation_exception` is the only "real" failure mode in the list — every
other entry is "data isn't there yet". Filter to those in your dashboards:

```sql
SELECT item_id, signal_name, metadata_json
FROM signals
WHERE computed_for = '2026-05-12'
  AND value IS NULL
  AND metadata_json LIKE '%computation_exception%';
```

## Warmup windows (practical guidance)

| Signal | Minimum history | Earliest useful date from cold start |
|---|---|---|
| `price_zscore` | 30 days of `price_history` | Day 30 |
| `supply_velocity` | 14 days of `listing_history` | Day 14 |
| `event_proximity` | 1 past equivalent event window (~ 1 year) | Year 2 (post-v1) |
| `comparables_delta` | 3 active peers with `latest_observation` | Whenever universe has been run once and ingest has touched ≥ 4 items per category |

So from a cold start: nothing useful for the first 14 days, partial scores
(supply only) days 14-29, partial scores (price + supply + maybe
comparables) day 30+, and event_proximity remains category-fallback-only
until v2.

## Failure modes in the logs

The runner logs via structlog at `ERROR` for storage failures (which abort
the run) and at `INFO/WARNING` for data-quality issues that just emit nulls.

| Log message | Origin | What to do |
|---|---|---|
| `signal compute raised; emitting null` | `signals.runner` | A signal raised an unexpected exception. The full traceback follows in the same event. `metadata_json.error_type` tells you which class. |
| `signals run finished` (status=`success`) | `signals.runner` | Healthy completion. |
| `signals run finished` (status=`partial`) | `signals.runner` | At least one item had at least one signal exception. Drill into individual `signal compute raised` events. |
| `DB error aborting signals run` | `signals.runner` | A `StorageError` made it past the per-item boundary. The runs row is marked `failed`; the exception propagates. Check the DB. |

The CLI exits with code `1` only when the run's final status is `failed`
(i.e., a DB-level abort). Partial runs exit `0` — they're the expected
state during warmup.

## Performance

Per-item compute is O(window-size) for `price_zscore` and `supply_velocity`,
O(past-events × peers) for `event_proximity`, and O(peers) for
`comparables_delta`. For 800 items × 4 signals at v1 cadence, a single
signals run on a developer laptop finishes in well under a minute.

If runs slow down notably, two likely culprits:

1. `v_daily_price` over a large history — consider materializing it
   periodically if `price_history` grows past low millions of rows.
2. `event_proximity` with many peers and several past events — bound by
   the rarity tag's universe size, which v1 caps below ~1000 items.

Neither is worth optimizing pre-emptively in v1.
