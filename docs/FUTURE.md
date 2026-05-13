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

## Other deferred items

(none yet — add here as they accrue)
