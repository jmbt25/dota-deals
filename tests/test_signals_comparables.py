"""Tests for :mod:`dota_deals.signals.comparables`.

Construction of peer sets is deliberately small (3-4 items) so the expected
median is computable by eye. Self-exclusion is tested with a fixture where
including-vs-excluding the target item shifts the median to different values
— if exclusion silently drops out the math will visibly fail.

Phase 9c-ii: pure function over a :class:`DataLookup`; tests construct the
peer set and per-item ``LatestObservation`` directly.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from dota_deals.models.domain import Item, ItemCategory, LatestObservation, Signal
from dota_deals.signals import comparables
from dota_deals.signals.dataset import DataLookup

AS_OF = date(2026, 5, 13)


# ----------------------------- helpers -----------------------------------------


def _item(item_id: int, *, category: ItemCategory = "arcana") -> Item:
    return Item(
        item_id=item_id,
        market_hash=f"item-{item_id}",
        name=f"item-{item_id}",
        category=category,
        hero=None,
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=None,
        active=True,
    )


def _obs(item_id: int, *, lowest_cents: int) -> LatestObservation:
    return LatestObservation(
        item_id=item_id,
        observed_at=datetime.combine(AS_OF, datetime.min.time(), tzinfo=UTC),
        lowest_cents=lowest_cents,
        listings_count=10,
    )


def _lookup(
    items: list[Item],
    observations: dict[int, LatestObservation],
) -> DataLookup:
    items_by_category: dict[ItemCategory, list[Item]] = {}
    for it in items:
        items_by_category.setdefault(it.category, []).append(it)
    return DataLookup(
        as_of=AS_OF,
        items_by_id={it.item_id: it for it in items},
        items_by_category=items_by_category,
        daily_prices={},
        listings={},
        latest_observations=observations,
        next_event=None,
        past_events_by_kind={},
    )


# ----------------------------- tests -------------------------------------------


def test_priced_30_percent_below_three_peers_yields_plus_0_3() -> None:
    """SPEC: priced 30% below peer median → output +0.3."""
    target_id = 1
    target = _item(target_id)
    peers = [_item(i) for i in (2, 3, 4)]
    obs = {target_id: _obs(target_id, lowest_cents=7000)} | {
        p.item_id: _obs(p.item_id, lowest_cents=10000) for p in peers
    }

    signal = comparables.compute(target_id, AS_OF, _lookup([target, *peers], obs))
    assert signal.value is not None
    assert abs(signal.value - 0.3) < 1e-9


def test_fewer_than_three_peers_returns_null() -> None:
    """SPEC: <3 peers with a current price → null."""
    target = _item(1)
    p1 = _item(2)
    p2 = _item(3)
    obs = {
        target.item_id: _obs(target.item_id, lowest_cents=7000),
        p1.item_id: _obs(p1.item_id, lowest_cents=10000),
        p2.item_id: _obs(p2.item_id, lowest_cents=10000),
    }

    signal = comparables.compute(target.item_id, AS_OF, _lookup([target, p1, p2], obs))
    assert signal.value is None
    assert signal.metadata["reason"] == "insufficient_peers"
    assert signal.metadata["peers_with_price"] == 2


def test_peer_without_latest_observation_does_not_count() -> None:
    """Peers must have an actual observation; an items entry alone isn't enough."""
    target = _item(1)
    p1 = _item(2)
    p2 = _item(3)
    p3 = _item(4)  # no observation row
    obs = {
        target.item_id: _obs(target.item_id, lowest_cents=7000),
        p1.item_id: _obs(p1.item_id, lowest_cents=10000),
        p2.item_id: _obs(p2.item_id, lowest_cents=10000),
    }

    signal = comparables.compute(target.item_id, AS_OF, _lookup([target, p1, p2, p3], obs))
    assert signal.value is None
    assert signal.metadata["reason"] == "insufficient_peers"


def test_self_is_excluded_from_peer_set() -> None:
    """If self leaked into peers, the median would shift and output would change.

    Setup: target at 5000, peers at 10000, 30000, 50000.
    - With self in peer set: sorted = [5000, 10000, 30000, 50000], median = 20000.
    - With self excluded:    sorted = [10000, 30000, 50000],        median = 30000.

    For target=5000:
    - Wrong (self-included):  delta = (5000-20000)/20000 = -0.75, output +0.75.
    - Correct (self-excluded): delta = (5000-30000)/30000 = -0.833…, output
      clipped at +0.833…. Test asserts the latter.
    """
    target = _item(1)
    p1 = _item(2)
    p2 = _item(3)
    p3 = _item(4)
    obs = {
        target.item_id: _obs(target.item_id, lowest_cents=5000),
        p1.item_id: _obs(p1.item_id, lowest_cents=10000),
        p2.item_id: _obs(p2.item_id, lowest_cents=30000),
        p3.item_id: _obs(p3.item_id, lowest_cents=50000),
    }

    signal = comparables.compute(target.item_id, AS_OF, _lookup([target, p1, p2, p3], obs))
    assert signal.value is not None
    expected = -((5000 - 30000) / 30000)  # peer median EXCLUDING self = 30000
    assert abs(signal.value - expected) < 1e-9


def test_peers_from_other_category_are_ignored() -> None:
    """Comparables is category-scoped: an immortal can't be peer to an arcana."""
    target = _item(1, category="arcana")
    same_category = [_item(i, category="arcana") for i in (2, 3)]
    other_category = [_item(i, category="immortal") for i in (4, 5, 6)]
    obs = {target.item_id: _obs(target.item_id, lowest_cents=7000)} | {
        p.item_id: _obs(p.item_id, lowest_cents=10000) for p in same_category + other_category
    }

    # Only 2 same-category peers → insufficient.
    signal = comparables.compute(
        target.item_id, AS_OF, _lookup([target, *same_category, *other_category], obs)
    )
    assert signal.value is None
    assert signal.metadata["reason"] == "insufficient_peers"
    assert signal.metadata["peers_with_price"] == 2


def test_returned_signal_carries_correct_metadata() -> None:
    target = _item(1)
    peers = [_item(i) for i in (2, 3, 4)]
    obs = {target.item_id: _obs(target.item_id, lowest_cents=7000)} | {
        p.item_id: _obs(p.item_id, lowest_cents=10000) for p in peers
    }

    signal = comparables.compute(target.item_id, AS_OF, _lookup([target, *peers], obs))
    assert isinstance(signal, Signal)
    assert signal.signal_name == "comparables_delta"
    assert signal.computed_for == AS_OF
    assert signal.item_id == target.item_id
    assert signal.metadata["peers_with_price"] == 3
