"""Pre-fetched per-run data shared by every signal compute function.

The signal layer (Phase 9c-ii) follows a fetch-once / dispatch-many model:
:func:`build_data_lookup` issues a small fixed number of bulk D1 reads
(items, daily prices, listings, latest observations, events) up front,
the runner builds a :class:`DataLookup` from the results, and each signal's
compute function is a *pure* function that reads from the lookup.

This is the difference between "ships" and "exceeds rate limit by 3pm" —
the previous per-item-per-signal pattern issued ~3,200 D1 requests per
nightly run on a full universe; the new pattern issues 6-8 regardless of
universe size.

History window
--------------
:data:`_DAILY_PRICES_WINDOW_DAYS` is the rolling window of per-day median
prices the lookup holds. 95 days is a deliberate v1 trade: it covers
``price_zscore``'s 90-day baseline with slack, but does **not** reach back
to the most recent TI cycle. Past-event lookups in :mod:`event_proximity`
outside this window therefore find no data and the signal falls back to
its category-level path (or, when peers also have no usable past windows,
to ``value=None`` with ``reason=insufficient_peers_with_history``). This
matches the "Signal warmup" trade SPEC.md calls out for v1. Promote to
:class:`Settings.signals_history_window_days` once a second TI cycle of
data accumulates.

What's not in the lookup
------------------------
Two things the old sync code reached for that we deliberately skip:

* The signal-runner's own queries (active items, runs row management) —
  those go through the connection directly, not the lookup.
* Quarantine / failed-ingest bookkeeping — that's the runner's domain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from dota_deals.models.domain import Item, ItemCategory, LatestObservation, ListingPoint
from dota_deals.models.events import EventRecord
from dota_deals.storage.db_async import D1Connection
from dota_deals.storage.repositories_async import (
    active_items,
    daily_prices_for_items,
    latest_observations_all,
    next_event_within,
    past_events_of_kind,
    recent_listings_for_items,
)

# Width of the daily-price pre-fetch in calendar days. See the module
# docstring for the trade-off.
_DAILY_PRICES_WINDOW_DAYS = 95

# Width of the listings pre-fetch. Supply_velocity needs 30 days for its
# point comparison and a 14-day minimum-history check; 60 is the existing
# v1 default for the broader listings history that future signals may
# consume (e.g., a future "supply trend" signal could look at the full
# 60-day window for slope estimation).
_LISTINGS_WINDOW_DAYS = 60

# How far ahead in the future calendar to look for the next event. Beyond
# this, the convention is that no event is "in scope right now" and the
# signal renormalizes weights instead of fading to zero — see
# docs/SCORING.md.
_EVENT_LOOKAHEAD_DAYS = 60


@dataclass(frozen=True)
class DataLookup:
    """Immutable per-run view of everything the four signals need.

    Built once at the top of :func:`compute_signals_for` and passed to
    every signal compute call. Methods are pure dict lookups (or short
    linear scans over per-item lists), so signal computation cost is
    bounded by the math, not by the DB.

    Field semantics:

    * ``daily_prices`` — per ``item_id``, the list of
      ``(utc_date, median_lowest_cents)`` tuples sorted oldest-first
      over the last :data:`_DAILY_PRICES_WINDOW_DAYS`. An item with no
      observations in the window maps to ``[]`` (never absent from the
      dict — the bulk repo guarantees this).
    * ``listings`` — per ``item_id``, the list of :class:`ListingPoint`
      observations sorted oldest-first over the last
      :data:`_LISTINGS_WINDOW_DAYS`. Same empty-list contract as
      ``daily_prices``.
    * ``latest_observations`` — keyed by ``item_id``. Absent when the
      item has never been ingested.
    * ``items_by_id`` — every active item at lookup-build time.
      ``items_by_category`` is a derived index for peer lookups.
    * ``next_event`` — the closest event whose ``start_date`` falls in
      ``[as_of, as_of + _EVENT_LOOKAHEAD_DAYS]``, or ``None``.
    * ``past_events_by_kind`` — events keyed by ``kind``, with
      ``start_date < as_of``. Only kinds we'll actually look up are
      populated (today: the ``next_event``'s kind, if any).
    """

    as_of: date
    items_by_id: dict[int, Item]
    items_by_category: dict[ItemCategory, list[Item]]
    daily_prices: dict[int, list[tuple[date, int]]]
    listings: dict[int, list[ListingPoint]]
    latest_observations: dict[int, LatestObservation]
    next_event: EventRecord | None
    past_events_by_kind: dict[str, list[EventRecord]] = field(default_factory=dict)

    # ---- accessors ----

    def daily_prices_for(self, item_id: int) -> list[tuple[date, int]]:
        """Return per-day median series for ``item_id``; empty list if unknown."""
        return self.daily_prices.get(item_id, [])

    def daily_price_at(self, item_id: int, on: date) -> int | None:
        """Point lookup: median price for ``item_id`` on ``on``, or ``None``.

        Linear scan — the per-item series is ~95 entries at the v1
        window width, so a dict-of-dicts representation isn't worth the
        memory overhead for the cardinality involved.
        """
        for d, cents in self.daily_prices_for(item_id):
            if d == on:
                return cents
        return None

    def listings_for(self, item_id: int) -> list[ListingPoint]:
        """Return listing observations for ``item_id``; empty list if unknown."""
        return self.listings.get(item_id, [])

    def latest_observation_for(self, item_id: int) -> LatestObservation | None:
        return self.latest_observations.get(item_id)

    def latest_lowest_cents_for(self, item_id: int) -> int | None:
        obs = self.latest_observations.get(item_id)
        return obs.lowest_cents if obs is not None else None

    def item(self, item_id: int) -> Item | None:
        return self.items_by_id.get(item_id)

    def peers(self, category: ItemCategory, *, exclude_item_id: int | None = None) -> list[Item]:
        """Active items in ``category``; ``exclude_item_id`` is dropped if given.

        Used by ``comparables`` and ``event_proximity``'s category-level
        fallback. Returned list is sorted by ``item_id`` ascending so
        downstream loops are deterministic.
        """
        peers = self.items_by_category.get(category, [])
        if exclude_item_id is None:
            return list(peers)
        return [p for p in peers if p.item_id != exclude_item_id]

    def past_events(self, kind: str) -> list[EventRecord]:
        """Past events of ``kind`` (sorted most-recent-first), or ``[]``."""
        return self.past_events_by_kind.get(kind, [])


async def build_data_lookup(conn: D1Connection, as_of: date) -> DataLookup:
    """Pre-fetch every per-run input the four signals will consume.

    Issues five-to-six D1 bulk queries (active_items, daily_prices_for_items,
    recent_listings_for_items, latest_observations_all, next_event_within,
    plus past_events_of_kind when a next event exists). The
    :class:`D1Connection`'s rows-read accumulator records the cumulative
    cost; on close, an over-budget connection logs a WARNING so an
    accidentally-uncapped scan surfaces in operational telemetry.

    :param conn: open async D1 connection.
    :param as_of: UTC date the signals will be computed for. Windows are
        anchored relative to this.
    """
    items = await active_items(conn)
    item_ids = [i.item_id for i in items]

    items_by_id = {i.item_id: i for i in items}
    items_by_category: dict[ItemCategory, list[Item]] = {}
    for i in items:
        items_by_category.setdefault(i.category, []).append(i)

    daily = await daily_prices_for_items(
        conn,
        item_ids,
        days=_DAILY_PRICES_WINDOW_DAYS,
        as_of=as_of,
    )
    listings = await recent_listings_for_items(
        conn,
        item_ids,
        days=_LISTINGS_WINDOW_DAYS,
        as_of=as_of,
    )
    latest = await latest_observations_all(conn)

    next_evt = await next_event_within(conn, as_of, days_window=_EVENT_LOOKAHEAD_DAYS)

    past_events_by_kind: dict[str, list[EventRecord]] = {}
    if next_evt is not None:
        past_events_by_kind[next_evt.kind] = await past_events_of_kind(
            conn, next_evt.kind, before=as_of
        )

    return DataLookup(
        as_of=as_of,
        items_by_id=items_by_id,
        items_by_category=items_by_category,
        daily_prices=daily,
        listings=listings,
        latest_observations=latest,
        next_event=next_evt,
        past_events_by_kind=past_events_by_kind,
    )
