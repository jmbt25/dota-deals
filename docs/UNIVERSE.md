# Universe discovery

The universe stage discovers every arcana and immortal currently listed on the
Steam Community Market for Dota 2 and upserts each into the `items` table.
Without it, the ingest stage has nothing to track.

## Endpoint

`GET https://steamcommunity.com/market/search/render?norender=1&appid=570&category_570_Rarity[]=<tag>&start=<n>&count=<n>&currency=1`

`norender=1` returns a JSON `results` array (with nested `asset_description`
metadata per item) instead of HTML — much cleaner to parse. The endpoint is
the noisier of the three Steam URLs the pipeline talks to.

### Rarity tag values

These are **undocumented Valve internals**. If Steam renames them the universe
stage stops returning results and the operator needs to discover the new
values via Steam's "Filter results" UI and update
[`src/dota_deals/ingest/universe.py`](../src/dota_deals/ingest/universe.py).

| Category | Tag |
|---|---|
| `arcana` | `tag_Rarity_Arcana` |
| `immortal` | `tag_Rarity_Immortal` |

## CLI

```bash
dota-deals universe refresh
```

No flags. Reads settings from `.env` like every other command. Writes a `runs`
row with `kind='universe'` and counts of items upserted / categories
quarantined / categories failed.

## Pagination

Each rarity is paged via `start` and `count` (default 100, which Steam quietly
caps at). The loop continues while `start < total_count` and the most recent
response carried at least one result. A safety ceiling of 200 pages per
category bails out if Steam ever returns a `total_count` that doesn't shrink.

## Upsert semantics

On each sighting, [`upsert_item`](../src/dota_deals/storage/repositories.py)
runs the equivalent of:

```sql
INSERT INTO items (market_hash, name, category, hero, first_seen_at,
                   last_seen_at, active, consecutive_ingest_4xx)
VALUES (?, ?, ?, ?, ?, ?, 1, 0)
ON CONFLICT(market_hash) DO UPDATE SET
    name = excluded.name,
    category = excluded.category,
    hero = excluded.hero,
    last_seen_at = excluded.last_seen_at,
    active = 1,
    consecutive_ingest_4xx = 0
```

Three implications:

- **Reactivation is automatic.** If an item was previously deactivated by the
  ingest stage (see below) and reappears in a universe sighting, `active`
  flips back to 1 and the strike counter resets to 0.
- **`first_seen_at` is preserved** across upserts.
- **`hero` is currently always set to `NULL`.** Hero parsing from item names
  is deferred; Signal 4 (comparables) already falls back to all-arcana peers
  when hero is null, so this is a v1-acceptable gap.

## Ingest's 3-strike deactivation

Wired into the ingest runner: every time the price-overview or listings
endpoint returns a **true 4xx** (any 400-499 except 429) for a given item, its
`consecutive_ingest_4xx` counter is incremented. When the counter reaches
**3** and the item is currently `active`, ingest flips `active = 0`.

Conditions that **do not** count as strikes:

- 429 (rate-limit — infrastructure, not the item's fault)
- 5xx
- timeouts / transport errors
- validation errors (those route to `quarantine`)
- a successful 200 with no current price (the item exists but isn't selling)

A successful ingest run clears `consecutive_ingest_4xx` back to 0 for that
item; ingest **does not** reactivate (that's the universe stage's job).

The threshold lives in
[`src/dota_deals/ingest/runner.py`](../src/dota_deals/ingest/runner.py) as
`_INGEST_DEACTIVATION_THRESHOLD = 3`.

## Failure modes handled

| Failure | Disposition | Counted as |
|---|---|---|
| HTTP retries exhausted (5xx, timeout, transport) | Category fails entirely; the other rarity still runs. | `items_failed` (per category, max 2). |
| HTTP 4xx (non-429) on a search request | Same — not retried, surfaces as `IngestError`. | `items_failed`. |
| Page body fails Pydantic validation (e.g., missing `market_hash_name` in an entry) | Category quarantined; the other rarity still runs. | `items_quarantined`. |
| Steam returns `success: false` mid-pagination | Stop paginating that category; the items already upserted stay. | Whatever happened before the bad page is counted as `items_ok`. |

The run status is `success` if every category completed cleanly, `partial`
otherwise, and `failed` only if the process crashed before `update_run` could
fire.

## Output

After a run:

- `items` has been upserted with every discovered arcana and immortal.
- `runs` has one row with `kind='universe'`, the counts above, and the
  `parent_run_id` linking it to the CLI invocation.

To inspect the latest run:

```sql
SELECT * FROM runs WHERE kind = 'universe' ORDER BY started_at DESC LIMIT 1;

SELECT category, COUNT(*) AS n_active
FROM items WHERE active = 1
GROUP BY category;
```
