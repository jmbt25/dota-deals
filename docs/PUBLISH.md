# Publish & R2 sync — operational guide

The pipeline emits three classes of JSON file from the SQLite DB into
`public/data/`, where Cloudflare Pages serves them as static assets. The
GitHub Actions workflow (Phase 8) glues the stages together; this doc
covers the publish + R2 layer in isolation.

## CLI

```bash
dota-deals db pull                                 # download SQLite from R2
dota-deals signals compute --date 2026-05-12
dota-deals score --date 2026-05-12
dota-deals publish --top 20                        # writes latest, health, today's history
dota-deals publish --top 20 --include-items        # plus per-item detail files
dota-deals db push                                 # upload SQLite back to R2
```

`db pull` and `db push` are designed to be the first and last steps of
the GHA workflow. They also work locally for ad-hoc debugging.

## Output files

`--out-dir` defaults to `public/data/`. All paths below are relative to it.

| File | Built by | When written |
|---|---|---|
| `latest.json` | `build_latest_report` | Every `publish` run. Even if there are no scores (warmup), the file is written with `status: "warmup"` and an empty `scores` list. |
| `health.json` | `build_health` | Every `publish` run. Operational status + warmup countdown. |
| `history/YYYY-MM-DD.json` | `build_historical_report` | When scores exist for today's UTC date. Skipped silently otherwise; the GHA workflow runs signals → score before publish, so today's history will exist on every healthy run. |
| `items/<item_id>.json` | `build_item_detail` | Only with `--include-items`. Written for every item in the top-N of `latest.json`. |

## Wire format

Decisions baked into the four wire models in
[`src/dota_deals/publish/models.py`](../src/dota_deals/publish/models.py).
They're the contract the frontend depends on; if you change them, bump
`schema_version`.

| Decision | Choice | Why |
|---|---|---|
| Field naming | `snake_case` end-to-end | Matches the Python idiom. The frontend camelizes once at the fetch boundary; the pipeline avoids the aliasing layer and its typo surface. |
| Datetimes | ISO 8601 with `Z` suffix (`"2026-05-13T20:00:00Z"`) | RFC 3339 / strict ISO. `datetime.isoformat()` emits `+00:00`; the writer's `default` hook normalizes to `Z`. |
| Dates | `YYYY-MM-DD` | No timezone needed; UTC is implicit elsewhere. |
| Null fields | Included with `null` value | Stable contract — clients never need to `field in payload` defensive-check. Payloads are small enough that the size cost is negligible. |
| Buy scores | Native float precision (no rounding) | Display rounding is the frontend's job. The wire keeps every bit of math precision so a future "raw scores" debug view doesn't need a pipeline change. |
| Prices | USD strings formatted from integer cents | Avoids float-display surprises in JS (`(34.5).toFixed(2) === "34.50"` works but `(0.1 + 0.2).toFixed(2)` betrays you). `100099` cents → `"1000.99"`. |

### Schema versioning

Every payload carries `"schema_version": 1`. **Bump on any breaking
change to the structure** — adding optional fields is non-breaking, but
renaming, removing, or changing a field's type is. Downstream consumers
gate on this number.

### Example: `latest.json`

```json
{
  "schema_version": 1,
  "generated_at": "2026-05-13T20:30:01Z",
  "report_date": "2026-05-13",
  "status": "operational",
  "data_quality": {
    "ingest_run_id": "f7e8…",
    "ingest_status": "success",
    "missing_items": []
  },
  "scores": [
    {
      "item_id": 42,
      "market_hash_name": "Inscribed Manifold Paradox",
      "name": "Inscribed Manifold Paradox",
      "category": "arcana",
      "hero": null,
      "current_price": "34.50",
      "computed_for": "2026-05-13",
      "buy_score": 0.62,
      "components": {
        "price_zscore": 0.85,
        "supply_velocity": 0.50,
        "event_proximity": null,
        "comparables_delta": 0.40
      },
      "explanation": "Priced below recent baseline",
      "null_signals": ["event_proximity"]
    }
  ]
}
```

### Example: `health.json`

```json
{
  "schema_version": 1,
  "generated_at": "2026-05-13T20:30:01Z",
  "status": "operational",
  "last_run": {
    "run_id": "f7e8…",
    "kind": "ingest",
    "finished_at": "2026-05-13T20:00:14Z",
    "status": "success"
  },
  "data_coverage": {
    "items_tracked": 612,
    "items_with_signals": 540,
    "days_of_history": 48,
    "first_observation_at": "2026-03-26T08:00:00Z"
  },
  "warmup_estimate": {
    "days_remaining": null
  }
}
```

`status` precedence:

1. `"warmup"` — no scored date exists yet (cold start).
2. `"degraded"` — today's ingest run was `partial`.
3. `"operational"` — everything else.

`warmup_estimate.days_remaining` is `null` once observations span ≥ 30
calendar days (the longest signal-warmup window — once `price_zscore`
can compute, every signal is computable in principle).

## R2 sync model

Cloudflare R2 holds the canonical SQLite DB between scheduled runs. The
GHA workflow:

1. `dota-deals db pull` — downloads `dota_deals.db` to the runner's
   working directory.
2. … runs the pipeline stages …
3. `dota-deals db push` — uploads the modified DB back atomically:
   - `PUT dota_deals.db.tmp`
   - server-side `COPY dota_deals.db.tmp → dota_deals.db`
   - `DELETE dota_deals.db.tmp`

Step 3's two-phase write means the live `dota_deals.db` key is never
observed half-written. R2's server-side COPY is atomic at the destination.

### Settings

| Env var | Required? | Purpose |
|---|---|---|
| `R2_ENDPOINT` | yes | e.g. `https://<account>.r2.cloudflarestorage.com` |
| `R2_BUCKET` | yes | bucket name |
| `R2_ACCESS_KEY_ID` | yes | API token credential |
| `R2_SECRET_ACCESS_KEY` | yes | API token secret |
| `R2_DB_KEY` | optional (default `dota_deals.db`) | object key, useful for prod/staging separation |

If any of the four required fields is missing or empty, `R2Client`
construction raises `R2ConfigError` immediately — failures surface at
the CLI boundary, not deep inside an upload.

### Error model

The R2 client raises distinct exception types so the operator knows
whether to fix configuration or wait:

| Exception | Meaning | What to do |
|---|---|---|
| `R2ConfigError` | One or more required env vars unset. | Set them and re-run. |
| `R2CredentialsError` | Credentials rejected (`InvalidAccessKeyId`, `SignatureDoesNotMatch`, `AccessDenied`). | Rotate the R2 API token. |
| `R2BucketMissing` | Bucket doesn't exist. | Create it (Cloudflare dashboard) or fix the `R2_BUCKET` env var. |
| `R2ObjectMissing` | Object key doesn't exist. | For `db pull`, caught internally → empty file (first-run case). For other callers, fix the key. |
| `R2TransientError` | Network or 5xx, retries exhausted. | Wait, then re-run. |

Transient retries: 3 attempts with exponential backoff + jitter, capped
at 10 seconds. Mirrors the pattern in
[`ingest/steam.py`](../src/dota_deals/ingest/steam.py).

## Recovery procedures

### Corrupted DB sync

If a workflow run leaves the canonical R2 object in a bad state (e.g. the
upload completed but the SQLite file itself is corrupt because the
pipeline crashed mid-write):

**Option 1: re-run from scratch.** Delete the R2 object (Cloudflare
dashboard or `aws s3 rm s3://<bucket>/<key> --endpoint-url=<r2-endpoint>`).
The next `dota-deals db pull` will create an empty local file and the
pipeline will rebuild forward. Loses all history.

**Option 2: restore from R2 versioning.** If R2 versioning is enabled on
the bucket (recommended for production), use the dashboard to roll back
to the previous successful version. The pipeline picks up where it left
off on the next run.

R2 versioning is not enabled by default; the operator should turn it on
once the pipeline is running against real Steam.

### Stale `public/data/`

The GHA workflow git-commits `public/data/` after each publish. If a
commit ships a bad payload (wrong schema_version, missing field):

1. Revert the offending commit.
2. Re-run `dota-deals publish` locally against the synced DB.
3. Commit the corrected output.

Cloudflare Pages will pick up the revert automatically (it watches the
repo). Cache TTL for static assets is short; the bad version is
typically gone from edge within minutes.
