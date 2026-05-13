# Deferred work

Items intentionally left out of v1 with explicit scope so a future contributor
(or future me) can pick them up cleanly.

## Hero-name parsing for `items.hero`

**Status:** every row inserted by `ingest.universe.refresh_universe` currently
has `items.hero = NULL`. The schema column exists; the universe stage just
isn't populating it.

**Why deferred:** Steam's market search response doesn't carry a structured
"hero" field. Inferring hero from `market_hash_name` (e.g., *"Inscribed
Manifold Paradox"* → *Phantom Assassin*) requires a curated lookup table —
that's content work, not engineering work. A regex-based shortcut would be
partially accurate, and partial accuracy is worse than honest `NULL` for a
signal that depends on this data (Signal 4, `comparables_delta`, would
silently use the wrong peer set).

**Why it's safe to defer:** Signal 4 already has a `hero IS NULL` fallback
documented in [docs/SPEC.md](SPEC.md): the peer set falls back to
"same-category items" when hero is null. Output stays honest, just less
targeted.

**Scope of the followup, when picked up:**

1. Curate a `dota_heroes.json` (or similar) mapping `market_hash_name` →
   `hero`. Sources to combine:
   * Steam Inventory schema (game files) — authoritative but requires Steam
     Web API access (which v1 explicitly avoids).
   * `liquipedia.net/dota2/Arcanas` — community-maintained, scrapeable.
   * The arcana subset is small (≈ 20-30 entries) and stable; hand-curation
     is realistic for v1.5.
2. Backfill `items.hero` with one SQL `UPDATE` per curated entry.
3. Verify the `comparables_delta` signal upgrades automatically: pick an
   item whose hero has ≥ 2 other arcanas, compute the signal before and
   after the backfill, confirm the peer set narrows to same-hero peers.
4. Optional: add a `universe refresh --refresh-hero-mapping` CLI flag that
   re-applies the lookup on existing rows.

Immortal items are noisier (most have no hero, some do). A reasonable v1.5
scope is **arcanas only**.

## Backtesting harness

**Status:** SPEC.md success criterion #3 ("on a 90-day forward-collected
window, items with strong price_zscore and/or supply_velocity show median
30-day forward price change ≥ 5 percentage points above the universe
median") is the only v1 criterion still unverified.

**Why deferred:** the harness needs forward-collected `price_history` over
at least 90 days plus 30 more for the "forward return" lookahead. v1 can't
test what v1 hasn't yet observed.

**What would have to be true to start it:**

1. The pipeline has run unattended for ≥ 120 days against real Steam, with
   `≥ 90 %` daily coverage on the active universe.
2. A clear separation between **the signal definition under test** (frozen
   in code) and **the parameters being swept** (window length, trim
   fraction, weights, etc.) so a single harness can score variants.
3. A persistent `backtests` table or directory of artifacts so historical
   runs can be compared. Schema decision will fall out of the first
   real run.

Scope when picked up: a `dota-deals backtest --signal price_zscore
--window-days 90 --forward-days 30` CLI that reads `price_history`,
re-computes the signal for each historical date, and reports the
forward-return distribution split by signal strength.

## Scheduler / deployment

**Status:** v1 is invoked manually via CLI. No cron, no systemd, no
GitHub Actions wiring.

**Why deferred:** deployment is a target-dependent concern, not a code
concern. v1 deliberately exits the pipeline at the CLI boundary so any
scheduler can drive it.

**What would have to be true:** pick a target (laptop cron / always-on
VPS / GitHub Actions). The scheduler should run, in order:
`universe refresh` (weekly) → `ingest` (every `ingest_cadence_hours`) →
`signals compute` (daily, post-midnight UTC) → `score` (daily, after
signals) → `report --out reports/YYYY-MM-DD.json` (daily).

## Web frontend

**Status:** the pipeline emits `reports/YYYY-MM-DD.json` and stdout text.
That's the user-facing surface today.

**Why deferred:** product design ≠ pipeline engineering. A frontend
imposes UX decisions (which signal to surface, how to render data_quality,
how to time-travel) that the pipeline shouldn't be shaped by.

**What would have to be true:** a designer or product owner sketches the
user flow (a Steam Market sidebar overlay? a static site? a Discord bot?),
picks a stack, and treats the JSON contract in `notifier.json_file` as
the integration boundary. The pipeline's `schema_version: 1` is the
versioning hook.

## CS:GO / CS2 (or non-Dota markets)

**Status:** v1 is Dota 2 only (`appid=570`). The schema has Dota-shaped
CHECK constraints (`category IN ('arcana', 'immortal')`, `event_kind IN
('ti', 'treasure_release', ...)`) and the rarity-tag mapping in
`ingest.universe` is hard-coded for Dota.

**Why deferred:** the audience for Dota and CS2 markets is very different
(scale, item types, event cycle), so the right product almost certainly
isn't "the same code with a new appid." Better to validate one market
end-to-end before generalizing.

**What would have to be true:** v1 ships, lives unattended for 90 days,
and the operator decides the category model generalizes. The schema
gains a `appid` column on `items`, the rarity tags become a per-appid
mapping, and `event.kind` becomes free-form text. Pieces of the
signal-computation layer should generalize as-is — that's the test of
whether the abstraction was correct.
