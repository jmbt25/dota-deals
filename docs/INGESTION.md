# Ingestion

The ingest stage fetches current price and listing data from Steam for every
item in a supplied list, validates each response, and writes the results to
`price_history`, `listing_history`, and `latest_observation`. Validation
failures land in `quarantine`; transport / HTTP failures are counted in the
run summary without aborting the run.

## Endpoints

| Endpoint | What it returns | Why we hit it |
|---|---|---|
| `GET /market/priceoverview/?appid=570&currency=1&market_hash_name=…` | `lowest_price`, `median_price`, `volume` (24h sales) | The cheap, reliable price source. Used for `price_history.lowest_cents`, `median_cents`, and `volume_24h`. |
| `GET /market/listings/570/<market_hash_name>/render?start=0&count=1&currency=1&country=US&language=english&format=json` | `total_count` (total listings) | The only public source for current listing count. `count=1` keeps the body minimal; we discard everything except `total_count`. This endpoint is noisier and rate-limits harder. |

Both calls always go out with `currency=1` (USD); non-USD strings fail
validation and route to quarantine rather than silently mis-storing amounts.

## CLI

```bash
dota-deals ingest --items items.txt
```

### `items.txt` format

Plain text, one Steam `market_hash_name` per line. Blank lines and lines
starting with `#` are ignored.

```
# Phantom Assassin
Inscribed Manifold Paradox

# Pudge
Demon Eater
```

Every item listed must already exist in the `items` table — populated by the
`universe` stage (Phase 4+). Items not in the table are counted as `failed` in
the run summary, with a clear warning log line.

## Concurrency and rate-limiting

- A semaphore limits in-flight HTTP requests to `Settings.steam_concurrency`
  (default `2`).
- Each request has a `Settings.request_timeout_s` timeout (default 15s).
- Any 429 trips a process-global cool-down of `Settings.cooldown_429_s`
  (default 60s) before the next request is issued, in addition to the failing
  request's own longer backoff (30s → 60s → 120s).
- v1 cadence is **8-hourly** (`Settings.ingest_cadence_hours`), giving
  comfortable headroom against Steam's unofficial rate-limit policy.

## Observed-at truncation

Every successful poll in a run writes its observations with the same
`observed_at` — the **polling slot** containing the wall-clock start time.
With the default 8-hourly cadence the slots are 00:00, 08:00, 16:00 UTC. The
primary key `(item_id, observed_at)` plus `INSERT OR IGNORE` makes a re-run
within the same slot a no-op rather than a duplicate write.

## Failure modes handled

| Failure | Disposition | Counted as |
|---|---|---|
| `httpx.TimeoutException` | Retried 3× with exponential backoff + jitter. | `failed` on exhaustion. |
| `httpx.RequestError` (transport) | Same as timeout. | `failed` on exhaustion. |
| HTTP 5xx | Same as timeout. | `failed` on exhaustion. |
| HTTP 429 | Retried 4× with longer backoff (30s, 60s, 120s, 240s) plus a global cool-down. | `failed` on exhaustion. |
| HTTP 4xx (non-429) | Not retried. Logged at ERROR. | `failed`. |
| HTTP 3xx (redirect) | Not retried. Logged at ERROR. (Steam shouldn't redirect for our public endpoints; a redirect typically means a login wall.) | `failed`. |
| `json.JSONDecodeError` | Not retried. Raw body persisted. | `quarantined`. |
| `pydantic.ValidationError` (e.g., un-parseable price string, missing `total_count`) | Raw body persisted with validation error message. | `quarantined`. |
| Item not present in `items` table | Logged at WARNING. | `failed`. |
| Item present, Steam returns `success=false` or no `lowest_price` | Logged at INFO ("no price data yet"). | `failed`. |

The run is marked **`success`** if every item ended `ok`, **`partial`** if at
least one item was `quarantined` or `failed`, and **`failed`** only if the
process itself crashed before `update_run` could fire.

## Output

After a run, three tables hold the result:

- `price_history` — one row per `(item_id, observed_at)` with `lowest_cents`,
  `median_cents`, `volume_24h`.
- `listing_history` — one row per `(item_id, observed_at)` with
  `listings_count`.
- `latest_observation` — one row per `item_id` with the most recent snapshot.
  Used by Signal 4 (comparables) and the notifier.

Plus one row in `runs`:

```sql
SELECT * FROM runs WHERE kind = 'ingest' ORDER BY started_at DESC LIMIT 1;
```

…which carries `status`, `items_ok`, `items_quarantined`, `items_failed`,
`started_at`, `finished_at`, and the `parent_run_id` linking it to the
overall CLI invocation.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Run completed (regardless of partial counts). |
| `1` | Run status was `failed` (process-level error). |
| `130` | SIGINT / Ctrl-C. |
