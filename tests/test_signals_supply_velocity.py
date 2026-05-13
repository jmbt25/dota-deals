"""Tests for :mod:`dota_deals.signals.supply_velocity`.

The signal is sensitive to two distinct windows (now and 30d ago), each
collapsed by ``median(last_3)``. The fixtures here exercise each window
boundary explicitly: missing observations at one endpoint, zero counts,
single-point outliers, exact 40 % drop.

Phase 9c-ii: pure function over a pre-fetched :class:`DataLookup`; the
tests build a list of :class:`ListingPoint` directly rather than seeding
``listing_history`` rows.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

from dota_deals.models.domain import Item, ListingPoint, Signal
from dota_deals.signals import supply_velocity
from dota_deals.signals.dataset import DataLookup

AS_OF = date(2026, 5, 13)
_ITEM_ID = 1


# ----------------------------- helpers -----------------------------------------


def _item(item_id: int = _ITEM_ID) -> Item:
    return Item(
        item_id=item_id,
        market_hash=f"item-{item_id}",
        name=f"item-{item_id}",
        category="arcana",
        hero=None,
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=None,
        active=True,
    )


def _three_slots(day: date) -> list[datetime]:
    """The three 8-hourly observation slots for ``day`` UTC."""
    return [
        datetime.combine(day, time(0), tzinfo=UTC),
        datetime.combine(day, time(8), tzinfo=UTC),
        datetime.combine(day, time(16), tzinfo=UTC),
    ]


def _listings_from(
    daily_counts: dict[date, list[int]], item_id: int = _ITEM_ID
) -> list[ListingPoint]:
    """Build a sorted-oldest-first :class:`ListingPoint` series from a
    ``{date: [counts at 00, 08, 16]}`` map.
    """
    points: list[ListingPoint] = []
    for day in sorted(daily_counts):
        for slot, count in zip(_three_slots(day), daily_counts[day], strict=True):
            points.append(ListingPoint(item_id=item_id, observed_at=slot, listings_count=count))
    return points


def _lookup(listings: list[ListingPoint]) -> DataLookup:
    return DataLookup(
        as_of=AS_OF,
        items_by_id={_ITEM_ID: _item()},
        items_by_category={"arcana": [_item()]},
        daily_prices={},
        listings={_ITEM_ID: listings},
        latest_observations={},
        next_event=None,
        past_events_by_kind={},
    )


# ----------------------------- tests -------------------------------------------


def test_forty_percent_supply_drop_yields_plus_0_4() -> None:
    """SPEC: 40 % supply drop over 30 days → +0.4."""
    daily_counts: dict[date, list[int]] = {}
    for offset in range(32):
        day = AS_OF - timedelta(days=offset)
        if day == AS_OF:
            daily_counts[day] = [60, 60, 60]
        else:
            daily_counts[day] = [100, 100, 100]

    signal = supply_velocity.compute(_ITEM_ID, AS_OF, _lookup(_listings_from(daily_counts)))
    assert signal.value is not None
    assert abs(signal.value - 0.4) < 1e-9


def test_flat_supply_returns_zero() -> None:
    """Constant listing count → relative change 0 → output 0."""
    daily_counts = {AS_OF - timedelta(days=offset): [100, 100, 100] for offset in range(32)}

    signal = supply_velocity.compute(_ITEM_ID, AS_OF, _lookup(_listings_from(daily_counts)))
    assert signal.value == 0.0


def test_under_14_days_history_returns_null() -> None:
    daily_counts = {AS_OF - timedelta(days=offset): [100, 100, 100] for offset in range(10)}

    signal = supply_velocity.compute(_ITEM_ID, AS_OF, _lookup(_listings_from(daily_counts)))
    assert signal.value is None
    assert signal.metadata["reason"] == "insufficient_history"


def test_zero_reference_count_returns_null() -> None:
    """SPEC: ``listings_30d_ago == 0`` → null (can't divide)."""
    daily_counts: dict[date, list[int]] = {}
    reference_day = AS_OF - timedelta(days=30)
    for offset in range(35):
        day = AS_OF - timedelta(days=offset)
        if day == AS_OF:
            daily_counts[day] = [60, 60, 60]
        elif day == reference_day:
            daily_counts[day] = [0, 0, 0]
        else:
            daily_counts[day] = [100, 100, 100]

    signal = supply_velocity.compute(_ITEM_ID, AS_OF, _lookup(_listings_from(daily_counts)))
    assert signal.value is None
    assert signal.metadata["reason"] == "reference_count_zero"


def test_single_observation_outlier_does_not_dominate() -> None:
    """SPEC: a single bad-scrape spike must be absorbed by median-of-3.

    All 32 days at 100 except today's middle slot at 99999. Without the
    median, "today's count" would be 99999 and the signal would saturate to
    -1. With the median, today = median([100, 99999, 100]) = 100 and the
    signal is flat 0.
    """
    daily_counts: dict[date, list[int]] = {}
    for offset in range(32):
        day = AS_OF - timedelta(days=offset)
        if day == AS_OF:
            daily_counts[day] = [100, 99999, 100]
        else:
            daily_counts[day] = [100, 100, 100]

    signal = supply_velocity.compute(_ITEM_ID, AS_OF, _lookup(_listings_from(daily_counts)))
    assert signal.value == 0.0


def test_returned_signal_carries_correct_metadata() -> None:
    daily_counts = {AS_OF - timedelta(days=offset): [100, 100, 100] for offset in range(32)}

    signal = supply_velocity.compute(_ITEM_ID, AS_OF, _lookup(_listings_from(daily_counts)))
    assert isinstance(signal, Signal)
    assert signal.signal_name == "supply_velocity"
    assert signal.computed_for == AS_OF
    assert signal.item_id == _ITEM_ID
