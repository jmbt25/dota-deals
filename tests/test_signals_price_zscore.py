"""Tests for :mod:`dota_deals.signals.price_zscore`.

Hand-constructed price histories chosen so the expected output is exact
arithmetic, not "approximately whatever statistics gives back". The flat /
1.5-sigma / outlier-robust / insufficient-history / stddev-zero shapes
from SPEC.md are each their own fixture.

Phase 9c-ii: the signal is a pure function of
``(item_id, as_of, DataLookup)``; tests construct a :class:`DataLookup`
with the per-day series the signal expects. No DB is involved.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from dota_deals.models.domain import Item, Signal
from dota_deals.signals import price_zscore
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


def _series_ending_at(daily_cents: list[int], ending_at: date) -> list[tuple[date, int]]:
    """Construct a sorted ``[(date, cents)]`` series ending at ``ending_at``.

    ``daily_cents[-1]`` is the value on ``ending_at``; preceding entries
    are consecutive prior days.
    """
    n = len(daily_cents)
    return [(ending_at - timedelta(days=n - 1 - i), cents) for i, cents in enumerate(daily_cents)]


def _lookup(daily_prices: list[tuple[date, int]], *, as_of: date = AS_OF) -> DataLookup:
    """Minimal DataLookup carrying just what price_zscore reads."""
    return DataLookup(
        as_of=as_of,
        items_by_id={_ITEM_ID: _item()},
        items_by_category={"arcana": [_item()]},
        daily_prices={_ITEM_ID: daily_prices},
        listings={},
        latest_observations={},
        next_event=None,
        past_events_by_kind={},
    )


# ----------------------------- tests -------------------------------------------


def test_flat_price_returns_zero() -> None:
    """SPEC: stddev=0 (totally flat price) → 0.0, not infinity."""
    # 90 days of $1.00 history ending the day before AS_OF, then $1.00 on AS_OF.
    history = _series_ending_at([100] * 90, AS_OF - timedelta(days=1))
    history.append((AS_OF, 100))

    signal = price_zscore.compute(_ITEM_ID, AS_OF, _lookup(history))
    assert signal.value == 0.0
    assert signal.metadata.get("reason") == "flat_window_stddev_zero"


def test_exact_1_5_sigma_below_median_yields_plus_0_5() -> None:
    """SPEC: priced at -1.5 sigma relative to 90d median → output +0.5.

    Window construction: 45 days at $0.90 and 45 days at $1.10 (population
    stddev exactly 10 cents, median exactly 100 cents). Current = $0.85
    → z = -1.5 → output = +0.5.
    """
    window = [9000] * 45 + [11000] * 45
    history = _series_ending_at(window, AS_OF - timedelta(days=1))
    history.append((AS_OF, 8500))

    signal = price_zscore.compute(_ITEM_ID, AS_OF, _lookup(history))
    assert signal.value == pytest.approx(0.5, abs=1e-9)


def test_insufficient_history_under_30_days_returns_null() -> None:
    history = _series_ending_at([100] * 25, AS_OF - timedelta(days=1))
    history.append((AS_OF, 80))

    signal = price_zscore.compute(_ITEM_ID, AS_OF, _lookup(history))
    assert signal.value is None
    assert signal.metadata["reason"] == "insufficient_history"
    assert signal.metadata["days_available"] == 25


def test_no_current_day_observation_returns_null() -> None:
    """History present but nothing for as_of itself → null."""
    history = _series_ending_at([100] * 60, AS_OF - timedelta(days=1))

    signal = price_zscore.compute(_ITEM_ID, AS_OF, _lookup(history))
    assert signal.value is None
    assert signal.metadata["reason"] == "no_daily_price_for_as_of"


def test_extreme_outlier_does_not_dominate_signal() -> None:
    """SPEC: outlier-driven median is bounded; signal stays sensible.

    With 89 days at $1.00 and 1 outlier at $999.99, the trimmed median is
    still $1.00 (the outlier gets trimmed; so do four of the $1.00 values,
    leaving 85 of them — median = 100 cents). The population stddev rises
    a lot, which suppresses the z-score. A "real" 30% discount today
    (current = 70 cents) yields a tiny output — far from the ±1 saturation —
    confirming the signal isn't being shoved around by the outlier.
    """
    window = [100] * 89 + [99999]  # one wild outlier on the most recent history day
    history = _series_ending_at(window, AS_OF - timedelta(days=1))
    history.append((AS_OF, 70))

    signal = price_zscore.compute(_ITEM_ID, AS_OF, _lookup(history))
    assert signal.value is not None
    assert abs(signal.value) < 0.05, f"outlier-dominated signal: {signal.value}"


def test_returned_signal_carries_correct_metadata() -> None:
    history = _series_ending_at([100] * 90, AS_OF - timedelta(days=1))
    history.append((AS_OF, 100))

    signal = price_zscore.compute(_ITEM_ID, AS_OF, _lookup(history))
    assert isinstance(signal, Signal)
    assert signal.signal_name == "price_zscore"
    assert signal.computed_for == AS_OF
    assert signal.item_id == _ITEM_ID
