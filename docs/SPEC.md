# dota-deals — Product Spec

## What it is

A web product that surfaces the best buying opportunities on the Steam Community
Market for Dota 2 arcanas and immortals, with transparent reasoning behind every
recommendation.

The Steam Market shows a price chart and nothing else. dota-deals shows *why* an
item is a good buy right now: it's priced below its own historical baseline, its
supply is drying up, or its category historically appreciates in the window before
a major Dota event. Every recommendation exposes its underlying signals so users
can disagree intelligently.

## Target user

A Dota 2 player who already buys or sells items on the Steam Market and wants an
analytical edge. Specifically:

- Has a Steam account with market access
- Spends $5-$500 on Dota cosmetics over a year
- Reads patch notes and watches TI
- Currently makes buy/sell decisions on vibes or by manually checking price charts
- Would value 5-15 minutes of saved decision time per purchase

Not the target user: pure collectors who buy on aesthetics, traders moving $10k+
who already have private tooling, CS:GO/CS2 traders.

## MVP scope

- **Items:** every arcana and every immortal currently listed on the Steam Community
  Market. Estimated 500-800 distinct market entries.
- **Refresh cadence:** price and listing data refreshed every 8 hours by default
  (configurable via `Settings.ingest_cadence_hours`); signals recomputed nightly.
- **Output (v1):** a JSON file and a stdout report listing the top 20 "buy
  candidates" ranked by buy score, each with its component signal values and a
  one-line plain-English explanation. Web frontend is post-MVP.
- **History depth:** target 365 days of **forward-collected** price/listing history
  per item. No backfill. Steam's historical price endpoint is cookie-gated and out
  of scope for v1; history is built by running the pipeline. See "Signal warmup"
  below for the consequences.

## Signal warmup

The pipeline collects history forward only. Each signal has a minimum history
window before it can be computed for a given item:

| Signal | Minimum history | Behavior before warmup |
|---|---|---|
| `price_zscore` | 30 days of `price_history` | Emit null; item excluded from ranking. |
| `supply_velocity` | 14 days of `listing_history` | Emit null; item excluded from ranking. |
| `event_proximity` | At least one past equivalent event window (≈ 1 year) | Item-level signal: null. Category-level fallback used when ≥ 3 items in the category have past-window data. Full item-level computation is effectively post-v1. |
| `comparables_delta` | ≥ 3 active peers with current price | Emit null when peer set is too small. |

Practical consequence: from a cold start, the pipeline produces no scores for the
first 30 days; partial scores (price + supply only) thereafter; event-proximity
signals remain mostly null until the second TI cycle of operation. This is
documented in the README and the daily output's `data_quality` block.

## Out of scope for v1

- ML-based price prediction (signals are statistical and rule-based, not learned)
- Item categories beyond arcanas and immortals (couriers, sets, treasures, hero
  cosmetics, taunts, wards, etc.)
- CS:GO/CS2 or any non-Dota market
- User accounts, watchlists, alerts, email/push notifications
- Payment, paid tier, monetization
- Web UI (the pipeline outputs JSON; a frontend is a separate project)
- Predictions framed as advice ("you should buy") — we publish analysis, not
  recommendations to act

## Signals

The four signals below are computed per item per day. Each is normalized to a
[-1.0, 1.0] range where positive means "supports buying" and negative means
"discourages buying." All signal values are persisted so they can be backtested.

### Signal 1: Price vs. own history (`price_zscore`)

**Intuition.** Items revert toward their own historical baseline. An item priced
well below its 90-day median, with no fundamental reason, is a candidate.

**Input data.** `price_history` rows for the item over the past 365 days.

**Formula (plain English).** Compute the rolling median price over the last 90
days. Compute the standard deviation of daily price over the same window. Output
is `-(current_price - median_90d) / stddev_90d`, clipped to [-3, 3] and divided
by 3 to land in [-1, 1]. Negation makes "below baseline" positive.

**Strong reading.** A value > +0.5 means the current price is more than 1.5
standard deviations below the 90-day median. That's a notable discount.

**Failure modes to handle.**
- Item with < 30 days of history: emit null, not a guess. Item is excluded from
  ranking until it has history.
- stddev = 0 (totally flat price): emit 0.0, not infinity.
- Outlier-driven median: trimmed median (drop top/bottom 5% of observations)
  rather than raw median.

### Signal 2: Supply dynamics (`supply_velocity`)

**Intuition.** Listing count is a leading indicator of price. When supply drops
fast, price moves later. The market hasn't priced it in yet.

**Input data.** `listing_history` rows for the item over the past 60 days.

**Formula (plain English).** Compute `(listings_today - listings_30d_ago) /
listings_30d_ago` (relative change). Negate so that a supply drop is positive.
Clip to [-1, 1].

**Strong reading.** A value > +0.4 means listings have dropped by 40% or more in
the last 30 days. Significant supply contraction.

**Failure modes to handle.**
- Item with < 14 days of listing history: emit null.
- Item with listings_30d_ago = 0: emit null (we can't compute relative change).
- Listing count outlier from a single bad scrape: use median of last 3
  observations for "today" and "30d ago" rather than point values.

### Signal 3: Event proximity (`event_proximity`)

**Intuition.** Items behave predictably around major Dota events. Arcanas tend to
appreciate in the 21-day window before TI. Items leaving a treasure rotation
appreciate as supply dries up. Items in a newly-released treasure depreciate as
supply floods in.

**v1 reality.** With forward-fill only and ≤ 365 days of history, the per-item
formulation below cannot be evaluated for most items until the second equivalent
event cycle. v1 ships the category-level fallback only; per-item history kicks in
opportunistically as data accumulates. Backtesting this signal is post-v1
(see Success criteria #3).

**Input data.** `events` table (manually curated), plus historical price behavior
of the item (or its category) in equivalent past windows.

**Formula (plain English).**
1. Find the next major event (TI, treasure release, major patch). If no event
   within 60 days, emit 0.0.
2. Compute days-until-event.
3. Look up the item's price behavior (or category-level behavior if item lacks
   history) in the same days-until-event window in past years.
4. Output is the median percentage price change in past equivalent windows,
   clipped to [-50%, +50%] and scaled to [-1, 1].

**Strong reading.** A value > +0.4 means historically this item or its category
appreciated 20%+ in the equivalent past window. Combined with current price
below baseline, this is the strongest setup we look for.

**Failure modes to handle.**
- No past data for this item in equivalent windows: fall back to category-level
  median. Flag the signal as "category-based" in the output.
- Item didn't exist in past TI cycles: category-based only.
- Event has ambiguous date (announced but date not finalized): use the announced
  date with a `confidence: tentative` flag carried into the output.

### Signal 4: Comparables (`comparables_delta`)

**Intuition.** Items in the same category (or for the same hero) trade in
reference to each other. An item priced way out of line with its peers is either
a mispricing or has a reason we should surface.

**Input data.** `items` table joined with current `price_history`.

**Formula (plain English).** Peer set is chosen as follows: if the item is an
arcana and `items.hero IS NOT NULL`, peers are same-hero arcanas (typically 1-2;
falls back to all arcanas if fewer than 3). Otherwise peers are same-category
items. From the peer set, compute the median current price. Output is
`-(item_price - peer_median) / peer_median`, clipped to [-1, 1]. Negation makes
"cheaper than peers" positive.

**Strong reading.** A value > +0.3 means priced 30%+ below peers. Worth a closer
look (but more likely to be a known-reason discount than a real opportunity, so
weighted lowest).

**Failure modes to handle.**
- Fewer than 3 peer items: emit null (no meaningful comparison).
- Peer median = 0 (shouldn't happen but defensively handle): emit null.

## Composite buy score

`buy_score = 0.35 * price_zscore + 0.35 * supply_velocity + 0.20 * event_proximity + 0.10 * comparables_delta`

Weights chosen by reasoning, not training:

- Price and supply each get 35% because they are the two most actionable signals
  and the least correlated with each other.
- Event proximity gets 20% because it's powerful when present but null most of
  the year.
- Comparables get 10% as a tiebreaker and sanity check.

These weights are explicit in code, not buried in a config file, and should be
revisited after the first 90 days of operation against real outcomes.

**Null handling.** If any signal is null, the score is computed from the
remaining signals with weights renormalized. Renormalization divides each
non-null signal's weight by the sum of non-null weights so the final weights
total 1.0. Concretely: if `event_proximity` and `comparables_delta` are both null,
`buy_score = (0.35 / 0.70) * price_zscore + (0.35 / 0.70) * supply_velocity`.
If 3+ signals are null, no score is emitted (item is excluded from ranking
that day).

**Display rule (non-negotiable).** Every published buy score must show its four
component values and a one-line explanation citing the strongest contributing
signal. Users see the reasoning, always. This is a product principle, not a nice
to have — black-box scores destroy trust the first time they're wrong.

## Success criteria

The MVP is considered successful when:

1. **Reliability.** The pipeline runs unattended for 14 consecutive days with no
   manual intervention. Failed individual item fetches go to quarantine without
   aborting the run. Recovery from a Steam outage (≥ 2 hours) is automatic.
2. **Coverage.** After 90 days of operation, ≥ 90% of the target item universe
   has a daily buy score computed (i.e., enough warmup to clear the windows in
   "Signal warmup" above).
3. **Signal quality.** On a 90-day forward-collected window, items with strong
   `price_zscore` (> +0.5) and/or strong `supply_velocity` (> +0.4) show median
   30-day forward price change at least 5 percentage points above the median for
   all items in the universe. Event-proximity backtesting is post-v1 (we don't
   yet have multi-year history). This is a low bar deliberately; we want to
   verify the signals contain *some* edge before shipping, not prove a trading
   strategy.
4. **Honesty in failure.** The published output for any day where signal coverage
   is degraded (Steam outage, partial ingestion) clearly says so. The output
   JSON carries a `data_quality` block listing missing items and the partial-run
   window. Users never see a confidently-presented score built on stale or
   incomplete data.

## Risks and unknowns

- **Steam may rate-limit or block aggressive polling.** We mitigate with low
  concurrency and exponential backoff, but a stricter policy from Valve could
  force per-IP rotation or a slower refresh cadence. Documented; not solved in
  v1.
- **The Dota item economy has been shrinking** since the Battle Pass ended. The
  MVP audience may be smaller than the analogous CS2 audience by 50-100x. We
  accept this trade for the lower competition.
- **Signal weights are guesses until validated.** The backtest in criterion 3 is
  the first real test. Be prepared to throw out weights, signals, or both.
- **Event calendar is hand-curated.** Maintenance burden is roughly 20 events per
  year. Acceptable; documented in the runbook.