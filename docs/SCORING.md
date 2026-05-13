# Scoring & reporting — operational guide

This is the day-to-day "I have signals, now what?" reference. The score
formula and weights live in [docs/SPEC.md](SPEC.md); read that for *why*.
Read this for *how to operate it*.

## CLI

```bash
dota-deals score                       # compose scores for today UTC
dota-deals score --date 2026-05-12
dota-deals report                      # render top-20 to stdout for today UTC
dota-deals report --top 5
dota-deals report --date 2026-05-12 --top 20 --out reports/2026-05-12.json
```

`score` reads the four signal rows per item from the `signals` table for
the given UTC date and writes one row per item to the `scores` table.
`report` reads those score rows and renders them — stdout by default,
atomic-write JSON if `--out` is given.

## Renormalization, by example

Weights: 0.35 / 0.35 / 0.20 / 0.10 for price / supply / event / peers.

**All four signals present**: weighted sum.

```
price=+0.5, supply=+0.4, event=+0.3, peers=+0.2
score = 0.35·0.5 + 0.35·0.4 + 0.20·0.3 + 0.10·0.2
      = 0.175 + 0.14 + 0.06 + 0.02
      = +0.395
```

**One null** (e.g. event_proximity): the remaining three signals' weights
sum to 0.80; each gets multiplied by `1/0.80 = 1.25`.

```
price=+0.5, supply=+0.4, event=null, peers=+0.2
effective weights: 0.4375 / 0.4375 / — / 0.125
score = 0.4375·0.5 + 0.4375·0.4 + 0.125·0.2
      = 0.21875 + 0.175 + 0.025
      = +0.41875
```

The same underlying buy thesis scores higher with renormalization than it
would by treating event_proximity as 0.0 (which would give `0.395`). This
is intentional — the 20% weight represents event-driven pressure, and when
there's none in the window, the *remaining* signals should carry the
score, not be diluted by a forced zero.

**Two nulls**: same principle, weights renormalize over the two left.

**Three or more nulls**: no score is emitted. The item is excluded from
`scores` and counted in the scoring run's `items_failed`.

## The event_proximity = null convention

Most of the calendar year, no event is within the 60-day lookahead window.
The signal returns `null` in that case so renormalization picks up the
slack. SPEC.md (Phase 5 change) and this doc both note: an earlier
convention had it return `0.0`, which silently capped most-of-year scores
at 0.80 of their true magnitude. If you're reading old issues or commits
that reference `event_proximity == 0.0` from the "no event" path, that's
the previous behavior.

## How the one-line explanation is chosen

Every row in the report carries a plain-English `explanation`. It's the
template matching the signal with the **largest absolute renormalized
contribution**.

```
contribution(name) = effective_weight(name) × |signal_value(name)|
```

The signal with the highest contribution determines the template; the
*sign* of that signal's value picks the positive- or negative-direction
phrasing. Templates live in
[src/dota_deals/scoring/buy_score.py](../src/dota_deals/scoring/buy_score.py)
under `_EXPLANATIONS`.

| Signal | Positive direction | Negative direction |
|---|---|---|
| `price_zscore` | "Priced below recent baseline" | "Priced above recent baseline" |
| `supply_velocity` | "Listings contracting" | "Listings expanding" |
| `event_proximity` | "Historically appreciates before upcoming event" | "Historically depreciates before upcoming event" |
| `comparables_delta` | "Priced below same-category peers" | "Priced above same-category peers" |

Phrasing intentionally describes *what is true*, not *what to do*. SPEC.md
is firm on this: we publish analysis, not recommendations to act.

## data_quality fields

### Per-score (`scores.data_quality_json`)

| Field | Meaning |
|---|---|
| `null_signals` | List of signal names that contributed null for this item. Useful for filtering "scores I trust less". |
| `ingest_status` | Status of the most recent ingest run for this date: `"success"`, `"partial"`, `"failed"`, or `"missing"` (no ingest ran). |
| `item_missing_from_ingest` | `true` if this specific item has no `price_history` row on the score's date — its price data is stale. |

### Run-level (notifier output)

| Field | Meaning |
|---|---|
| `ingest_status` | Same as per-score, but at the report's date level. |
| `ingest_run_id` | UUID of the ingest run consulted. Use it to join with `runs` for full context. |
| `missing_items` | Sorted list of `market_hash` for active items with no observation on the date. Empty list = full coverage. |

Filter to confidently-trustworthy report rows:

```sql
SELECT item_id, buy_score, explanation
FROM scores
WHERE computed_for = '2026-05-12'
  AND data_quality_json NOT LIKE '%"null_signals":["%'  -- no null signals
  AND data_quality_json LIKE '%"ingest_status":"success"%'
ORDER BY buy_score DESC
LIMIT 20;
```

## Recomputing a date

`scores` is idempotent on `(item_id, computed_for)` via `INSERT OR IGNORE`.
Re-running `dota-deals score --date X` after some rows already exist is a
no-op for those rows and back-fills any missing ones.

To do a real recompute (e.g. because the underlying signals were
recomputed):

```sql
BEGIN IMMEDIATE;
DELETE FROM scores WHERE computed_for = '2026-05-12';
COMMIT;
```

…then `dota-deals score --date 2026-05-12`. Same race caveat as for
signals — stop any concurrent `score` invocation first. See
[docs/SIGNALS.md](SIGNALS.md) for the longer discussion.

## Reading the report

### stdout format

```
dota-deals report
date: 2026-05-12 UTC
data_quality: ok
top 3 buy candidates:

 1. score=+0.620 | item_id=42
    reason: Priced below recent baseline
    components: price=+0.85 supply=+0.50 event=null peers=+0.40
    data_quality: ingest_status='success', item_missing_from_ingest=False, null_signals=['event_proximity']

 2. ...
```

The format is deterministic so a snapshot test can pin it byte-for-byte.
If you want pretty output for a dashboard, the JSON file is the
better-shaped input.

### JSON shape

```json
{
  "schema_version": 1,
  "generated_at": "2026-05-12T20:30:01+00:00",
  "report_date": "2026-05-12",
  "data_quality": {
    "ingest_run_id": "…",
    "ingest_status": "success",
    "missing_items": []
  },
  "scores": [
    {
      "item_id": 42,
      "computed_for": "2026-05-12",
      "buy_score": 0.620,
      "components": {
        "price_zscore": 0.85,
        "supply_velocity": 0.50,
        "event_proximity": null,
        "comparables_delta": 0.40
      },
      "explanation": "Priced below recent baseline",
      "data_quality": {
        "null_signals": ["event_proximity"],
        "ingest_status": "success",
        "item_missing_from_ingest": false
      }
    }
  ]
}
```

`schema_version` lets downstream consumers detect format changes; bump
it on any breaking change to the structure.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Run completed (regardless of partial counts). |
| `1` | Scoring run status was `failed` (DB-level abort). |
| `130` | SIGINT / Ctrl-C. |
