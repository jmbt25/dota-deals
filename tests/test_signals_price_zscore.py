"""Tests for :mod:`dota_deals.signals.price_zscore`.

Hand-constructed price histories chosen so the expected output is exact
arithmetic, not "approximately whatever statistics gives back". The flat /
1.5-sigma / outlier-robust / insufficient-history / stddev-zero shapes
from SPEC.md are each their own fixture.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time, timedelta

import pytest

from dota_deals.models.domain import Signal
from dota_deals.signals import price_zscore
from tests.conftest import insert_test_item

AS_OF = date(2026, 5, 13)


# ----------------------------- helpers -----------------------------------------


def _insert_daily_price(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    on: date,
    cents: int,
    hour: int = 8,
) -> None:
    """Insert a single price_history row at the given UTC date and hour.

    One observation per day is enough to drive the daily-price view (the
    MEDIAN of a single value is that value).
    """
    observed_at = datetime.combine(on, time(hour), tzinfo=UTC)
    conn.execute(
        """
        INSERT INTO price_history (item_id, observed_at, lowest_cents)
        VALUES (?, ?, ?)
        """,
        (item_id, observed_at.isoformat(), cents),
    )


def _insert_history(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    daily_cents: list[int],
    ending_at: date,
) -> None:
    """Insert one observation per day for ``len(daily_cents)`` days ending at
    ``ending_at`` (inclusive). ``daily_cents[-1]`` is on ``ending_at``.
    """
    n = len(daily_cents)
    for i, cents in enumerate(daily_cents):
        day = ending_at - timedelta(days=n - 1 - i)
        _insert_daily_price(conn, item_id, on=day, cents=cents)
    conn.commit()


# ----------------------------- tests -------------------------------------------


def test_flat_price_returns_zero(db_conn: sqlite3.Connection) -> None:
    """SPEC: stddev=0 (totally flat price) → 0.0, not infinity."""
    item_id = insert_test_item(db_conn, market_hash="FLAT")

    # 90 days of history at $1.00, plus today also at $1.00.
    _insert_history(db_conn, item_id, daily_cents=[100] * 90, ending_at=AS_OF - timedelta(days=1))
    _insert_daily_price(db_conn, item_id, on=AS_OF, cents=100)
    db_conn.commit()

    signal = price_zscore.compute(db_conn, item_id, AS_OF)
    assert signal.value == 0.0
    assert signal.metadata.get("reason") == "flat_window_stddev_zero"


def test_exact_1_5_sigma_below_median_yields_plus_0_5(
    db_conn: sqlite3.Connection,
) -> None:
    """SPEC: priced at -1.5 sigma relative to 90d median → output +0.5.

    Window construction: 45 days at $0.90 and 45 days at $1.10 (population
    stddev exactly 10 cents, median exactly 100 cents). Current = $0.85
    → z = -1.5 → output = +0.5.
    """
    item_id = insert_test_item(db_conn, market_hash="ZS")

    history = [9000] * 45 + [11000] * 45  # cents
    _insert_history(db_conn, item_id, daily_cents=history, ending_at=AS_OF - timedelta(days=1))
    _insert_daily_price(db_conn, item_id, on=AS_OF, cents=8500)
    db_conn.commit()

    signal = price_zscore.compute(db_conn, item_id, AS_OF)
    assert signal.value == pytest.approx(0.5, abs=1e-9)


def test_insufficient_history_under_30_days_returns_null(
    db_conn: sqlite3.Connection,
) -> None:
    item_id = insert_test_item(db_conn, market_hash="SHORT")

    _insert_history(db_conn, item_id, daily_cents=[100] * 25, ending_at=AS_OF - timedelta(days=1))
    _insert_daily_price(db_conn, item_id, on=AS_OF, cents=80)
    db_conn.commit()

    signal = price_zscore.compute(db_conn, item_id, AS_OF)
    assert signal.value is None
    assert signal.metadata["reason"] == "insufficient_history"
    assert signal.metadata["days_available"] == 25


def test_no_current_day_observation_returns_null(db_conn: sqlite3.Connection) -> None:
    """If we have history but no observation for ``as_of`` itself, emit null."""
    item_id = insert_test_item(db_conn, market_hash="NOCURRENT")

    _insert_history(db_conn, item_id, daily_cents=[100] * 60, ending_at=AS_OF - timedelta(days=1))
    db_conn.commit()

    signal = price_zscore.compute(db_conn, item_id, AS_OF)
    assert signal.value is None
    assert signal.metadata["reason"] == "no_daily_price_for_as_of"


def test_extreme_outlier_does_not_dominate_signal(db_conn: sqlite3.Connection) -> None:
    """SPEC: outlier-driven median is bounded; signal stays sensible.

    With 89 days at $1.00 and 1 outlier at $999.99, the trimmed median is
    still $1.00 (the outlier gets trimmed; so do four of the $1.00 values,
    leaving 85 of them — median = 100 cents). The population stddev rises
    a lot, which suppresses the z-score. A "real" 30% discount today
    (current = 70 cents) yields a tiny output — far from the ±1 saturation —
    confirming the signal isn't being shoved around by the outlier.
    """
    item_id = insert_test_item(db_conn, market_hash="OUT")

    history = [100] * 89 + [99999]  # one wild outlier on the most recent history day
    _insert_history(db_conn, item_id, daily_cents=history, ending_at=AS_OF - timedelta(days=1))
    _insert_daily_price(db_conn, item_id, on=AS_OF, cents=70)
    db_conn.commit()

    signal = price_zscore.compute(db_conn, item_id, AS_OF)
    assert signal.value is not None
    assert abs(signal.value) < 0.05, f"outlier-dominated signal: {signal.value}"


def test_returned_signal_carries_correct_metadata(db_conn: sqlite3.Connection) -> None:
    """Verify the Signal object's name and date fields are set correctly."""
    item_id = insert_test_item(db_conn, market_hash="META")

    _insert_history(db_conn, item_id, daily_cents=[100] * 90, ending_at=AS_OF - timedelta(days=1))
    _insert_daily_price(db_conn, item_id, on=AS_OF, cents=100)
    db_conn.commit()

    signal = price_zscore.compute(db_conn, item_id, AS_OF)
    assert isinstance(signal, Signal)
    assert signal.signal_name == "price_zscore"
    assert signal.computed_for == AS_OF
    assert signal.item_id == item_id
