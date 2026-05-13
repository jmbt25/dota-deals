"""Tests for :mod:`dota_deals.signals.supply_velocity`.

The signal is sensitive to two distinct windows (now and 30d ago), each
collapsed by ``median(last_3)``. The fixtures here exercise each window
boundary explicitly: missing observations at one endpoint, zero counts,
single-point outliers, exact 40 % drop.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time, timedelta

from dota_deals.models.domain import Signal
from dota_deals.signals import supply_velocity
from tests.conftest import insert_test_item

AS_OF = date(2026, 5, 13)


# ----------------------------- helpers -----------------------------------------


def _insert_listing(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    when: datetime,
    count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO listing_history (item_id, observed_at, listings_count)
        VALUES (?, ?, ?)
        """,
        (item_id, when.isoformat(), count),
    )


def _three_slots(day: date) -> list[datetime]:
    """The three 8-hourly observation slots for ``day`` UTC."""
    return [
        datetime.combine(day, time(0), tzinfo=UTC),
        datetime.combine(day, time(8), tzinfo=UTC),
        datetime.combine(day, time(16), tzinfo=UTC),
    ]


def _insert_listing_history(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    daily_counts: dict[date, list[int]],
) -> None:
    """Insert listing observations from a ``{date: [counts at 00, 08, 16]}`` map."""
    for day, counts in daily_counts.items():
        for slot, count in zip(_three_slots(day), counts, strict=True):
            _insert_listing(conn, item_id, when=slot, count=count)
    conn.commit()


# ----------------------------- tests -------------------------------------------


def test_forty_percent_supply_drop_yields_plus_0_4(db_conn: sqlite3.Connection) -> None:
    """SPEC: 40 % supply drop over 30 days → +0.4."""
    item_id = insert_test_item(db_conn, market_hash="DROP")

    # 32 days of history. The 30d-ago reference (AS_OF - 30) gets three obs
    # at 100; today gets three obs at 60.
    daily_counts: dict[date, list[int]] = {}
    for offset in range(32):
        day = AS_OF - timedelta(days=offset)
        if day == AS_OF:
            daily_counts[day] = [60, 60, 60]
        else:
            daily_counts[day] = [100, 100, 100]
    _insert_listing_history(db_conn, item_id, daily_counts=daily_counts)

    signal = supply_velocity.compute(db_conn, item_id, AS_OF)
    assert signal.value is not None
    assert abs(signal.value - 0.4) < 1e-9


def test_flat_supply_returns_zero(db_conn: sqlite3.Connection) -> None:
    """Constant listing count → relative change 0 → output 0."""
    item_id = insert_test_item(db_conn, market_hash="FLAT")

    daily_counts = {AS_OF - timedelta(days=offset): [100, 100, 100] for offset in range(32)}
    _insert_listing_history(db_conn, item_id, daily_counts=daily_counts)

    signal = supply_velocity.compute(db_conn, item_id, AS_OF)
    assert signal.value == 0.0


def test_under_14_days_history_returns_null(db_conn: sqlite3.Connection) -> None:
    item_id = insert_test_item(db_conn, market_hash="SHORT")

    daily_counts = {AS_OF - timedelta(days=offset): [100, 100, 100] for offset in range(10)}
    _insert_listing_history(db_conn, item_id, daily_counts=daily_counts)

    signal = supply_velocity.compute(db_conn, item_id, AS_OF)
    assert signal.value is None
    assert signal.metadata["reason"] == "insufficient_history"


def test_zero_reference_count_returns_null(db_conn: sqlite3.Connection) -> None:
    """SPEC: ``listings_30d_ago == 0`` → null (can't divide)."""
    item_id = insert_test_item(db_conn, market_hash="ZERO_REF")

    # 35 days history, but the three obs at AS_OF - 30 are all zero.
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
    _insert_listing_history(db_conn, item_id, daily_counts=daily_counts)

    signal = supply_velocity.compute(db_conn, item_id, AS_OF)
    assert signal.value is None
    assert signal.metadata["reason"] == "reference_count_zero"


def test_single_observation_outlier_does_not_dominate(
    db_conn: sqlite3.Connection,
) -> None:
    """SPEC: a single bad-scrape spike must be absorbed by median-of-3.

    All 32 days at 100 except today's middle slot at 99999. Without the
    median, "today's count" would be 99999 and the signal would saturate to
    -1. With the median, today = median([100, 99999, 100]) = 100 and the
    signal is flat 0.
    """
    item_id = insert_test_item(db_conn, market_hash="OUTLIER")

    daily_counts: dict[date, list[int]] = {}
    for offset in range(32):
        day = AS_OF - timedelta(days=offset)
        if day == AS_OF:
            daily_counts[day] = [100, 99999, 100]
        else:
            daily_counts[day] = [100, 100, 100]
    _insert_listing_history(db_conn, item_id, daily_counts=daily_counts)

    signal = supply_velocity.compute(db_conn, item_id, AS_OF)
    assert signal.value == 0.0


def test_returned_signal_carries_correct_metadata(db_conn: sqlite3.Connection) -> None:
    item_id = insert_test_item(db_conn, market_hash="META")
    daily_counts = {AS_OF - timedelta(days=offset): [100, 100, 100] for offset in range(32)}
    _insert_listing_history(db_conn, item_id, daily_counts=daily_counts)

    signal = supply_velocity.compute(db_conn, item_id, AS_OF)
    assert isinstance(signal, Signal)
    assert signal.signal_name == "supply_velocity"
    assert signal.computed_for == AS_OF
    assert signal.item_id == item_id
