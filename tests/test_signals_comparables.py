"""Tests for :mod:`dota_deals.signals.comparables`.

Construction of peer sets is deliberately small (3-4 items) so the expected
median is computable by eye. Self-exclusion is tested with a fixture where
including-vs-excluding the target item shifts the median to different values
— if exclusion silently drops out the math will visibly fail.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from dota_deals.models.domain import Signal
from dota_deals.signals import comparables
from tests.conftest import insert_test_item

AS_OF = date(2026, 5, 13)


# ----------------------------- helpers -----------------------------------------


def _set_latest_observation(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    lowest_cents: int,
    listings_count: int | None = 10,
) -> None:
    """Stamp the ``latest_observation`` cache directly for test setup."""
    conn.execute(
        """
        INSERT INTO latest_observation (item_id, observed_at, lowest_cents, listings_count)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            observed_at = excluded.observed_at,
            lowest_cents = excluded.lowest_cents,
            listings_count = excluded.listings_count
        """,
        (
            item_id,
            datetime.combine(AS_OF, datetime.min.time(), tzinfo=UTC).isoformat(),
            lowest_cents,
            listings_count,
        ),
    )
    conn.commit()


# ----------------------------- tests -------------------------------------------


def test_priced_30_percent_below_three_peers_yields_plus_0_3(
    db_conn: sqlite3.Connection,
) -> None:
    """SPEC: priced 30% below peer median → output +0.3."""
    target = insert_test_item(db_conn, market_hash="X", category="arcana")
    p1 = insert_test_item(db_conn, market_hash="P1", category="arcana")
    p2 = insert_test_item(db_conn, market_hash="P2", category="arcana")
    p3 = insert_test_item(db_conn, market_hash="P3", category="arcana")

    _set_latest_observation(db_conn, target, lowest_cents=7000)  # $70
    for peer in (p1, p2, p3):
        _set_latest_observation(db_conn, peer, lowest_cents=10000)  # $100

    signal = comparables.compute(db_conn, target, AS_OF)
    assert signal.value is not None
    assert abs(signal.value - 0.3) < 1e-9


def test_fewer_than_three_peers_returns_null(db_conn: sqlite3.Connection) -> None:
    """SPEC: <3 peers with a current price → null."""
    target = insert_test_item(db_conn, market_hash="X", category="arcana")
    p1 = insert_test_item(db_conn, market_hash="P1", category="arcana")
    p2 = insert_test_item(db_conn, market_hash="P2", category="arcana")

    _set_latest_observation(db_conn, target, lowest_cents=7000)
    _set_latest_observation(db_conn, p1, lowest_cents=10000)
    _set_latest_observation(db_conn, p2, lowest_cents=10000)

    signal = comparables.compute(db_conn, target, AS_OF)
    assert signal.value is None
    assert signal.metadata["reason"] == "insufficient_peers"
    assert signal.metadata["peers_with_price"] == 2


def test_peer_without_latest_observation_does_not_count(
    db_conn: sqlite3.Connection,
) -> None:
    """Peers must have an actual observation; the items row alone isn't enough."""
    target = insert_test_item(db_conn, market_hash="X", category="arcana")
    p1 = insert_test_item(db_conn, market_hash="P1", category="arcana")
    p2 = insert_test_item(db_conn, market_hash="P2", category="arcana")
    insert_test_item(db_conn, market_hash="P3", category="arcana")  # no observation row

    _set_latest_observation(db_conn, target, lowest_cents=7000)
    _set_latest_observation(db_conn, p1, lowest_cents=10000)
    _set_latest_observation(db_conn, p2, lowest_cents=10000)

    signal = comparables.compute(db_conn, target, AS_OF)
    assert signal.value is None
    assert signal.metadata["reason"] == "insufficient_peers"


def test_self_is_excluded_from_peer_set(db_conn: sqlite3.Connection) -> None:
    """If self leaked into peers, the median would shift and output would change.

    Setup: target at 5000, peers at 10000, 30000, 50000.
    - With self in peer set: sorted = [5000, 10000, 30000, 50000], median = 20000.
    - With self excluded:    sorted = [10000, 30000, 50000],        median = 30000.

    For target=5000:
    - Wrong (self-included):  delta = (5000-20000)/20000 = -0.75, output +0.75.
    - Correct (self-excluded): delta = (5000-30000)/30000 = -0.833…, output
      clipped at +0.833…. Test asserts the latter.
    """
    target = insert_test_item(db_conn, market_hash="X", category="arcana")
    p1 = insert_test_item(db_conn, market_hash="P1", category="arcana")
    p2 = insert_test_item(db_conn, market_hash="P2", category="arcana")
    p3 = insert_test_item(db_conn, market_hash="P3", category="arcana")

    _set_latest_observation(db_conn, target, lowest_cents=5000)
    _set_latest_observation(db_conn, p1, lowest_cents=10000)
    _set_latest_observation(db_conn, p2, lowest_cents=30000)
    _set_latest_observation(db_conn, p3, lowest_cents=50000)

    signal = comparables.compute(db_conn, target, AS_OF)
    assert signal.value is not None
    expected = -((5000 - 30000) / 30000)  # peer median EXCLUDING self = 30000
    assert abs(signal.value - expected) < 1e-9


def test_peers_from_other_category_are_ignored(db_conn: sqlite3.Connection) -> None:
    """Comparables is category-scoped: an immortal can't be peer to an arcana."""
    target = insert_test_item(db_conn, market_hash="X", category="arcana")
    same_category = [
        insert_test_item(db_conn, market_hash=f"A{i}", category="arcana") for i in range(2)
    ]
    other_category = [
        insert_test_item(db_conn, market_hash=f"I{i}", category="immortal") for i in range(3)
    ]

    _set_latest_observation(db_conn, target, lowest_cents=7000)
    for peer in same_category + other_category:
        _set_latest_observation(db_conn, peer, lowest_cents=10000)

    # Only 2 same-category peers → insufficient.
    signal = comparables.compute(db_conn, target, AS_OF)
    assert signal.value is None
    assert signal.metadata["reason"] == "insufficient_peers"
    assert signal.metadata["peers_with_price"] == 2


def test_returned_signal_carries_correct_metadata(db_conn: sqlite3.Connection) -> None:
    target = insert_test_item(db_conn, market_hash="X", category="arcana")
    peers = [insert_test_item(db_conn, market_hash=f"P{i}", category="arcana") for i in range(3)]
    _set_latest_observation(db_conn, target, lowest_cents=7000)
    for peer in peers:
        _set_latest_observation(db_conn, peer, lowest_cents=10000)

    signal = comparables.compute(db_conn, target, AS_OF)
    assert isinstance(signal, Signal)
    assert signal.signal_name == "comparables_delta"
    assert signal.computed_for == AS_OF
    assert signal.item_id == target
    assert signal.metadata["peers_with_price"] == 3
