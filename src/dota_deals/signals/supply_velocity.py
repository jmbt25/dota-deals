"""Signal 2: supply dynamics.

Computes the 30-day relative change in listing count, sign-flipped so a
supply drop reads positive. Uses a 3-observation median at each endpoint to
suppress single-poll outliers, as the SPEC requires.

Implementation specifics:

* **Today's listings** is the median of the 3 most recent observations with
  ``observed_at`` on or before ``as_of`` (end of UTC day).
* **30d-ago listings** is the median of the 3 most recent observations with
  ``observed_at`` on or before ``as_of - 30 days`` (end of that UTC day).
* **14-day minimum history** — the gap between the earliest observation and
  ``as_of`` must be at least 14 days.
* **``listings_30d_ago == 0``** emits null (can't compute a relative change
  against a zero denominator).
* **Output** is ``clip(-(today - past) / past, -1, 1)``.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import UTC, date, datetime, time, timedelta

from dota_deals.models.domain import ListingPoint, Signal
from dota_deals.storage.repositories import recent_listings

_HISTORY_WINDOW_DAYS = 60
_MIN_HISTORY_DAYS = 14
_REFERENCE_OFFSET_DAYS = 30
_OBS_FOR_MEDIAN = 3


def compute(conn: sqlite3.Connection, item_id: int, as_of: date) -> Signal:
    """Compute the ``supply_velocity`` signal for ``item_id`` as of ``as_of``.

    Returns a :class:`Signal` whose ``value`` is the clipped relative drop,
    or ``None`` with a ``reason`` in ``metadata`` for any insufficient-data
    case (the runner persists the null row so coverage reporting works).
    """
    listings = recent_listings(conn, item_id, days=_HISTORY_WINDOW_DAYS, as_of=as_of)
    if not listings:
        return _null_signal(item_id, as_of, reason="no_listing_history")

    earliest = listings[0].observed_at.date()
    if (as_of - earliest).days < _MIN_HISTORY_DAYS:
        return _null_signal(
            item_id,
            as_of,
            reason="insufficient_history",
            days_available=(as_of - earliest).days,
        )

    today_end = _end_of_day(as_of)
    today_obs = _take_last_n_before(listings, today_end, _OBS_FOR_MEDIAN)
    if len(today_obs) < _OBS_FOR_MEDIAN:
        return _null_signal(item_id, as_of, reason="too_few_recent_observations")

    reference_date = as_of - timedelta(days=_REFERENCE_OFFSET_DAYS)
    reference_end = _end_of_day(reference_date)
    reference_obs = _take_last_n_before(listings, reference_end, _OBS_FOR_MEDIAN)
    if len(reference_obs) < _OBS_FOR_MEDIAN:
        return _null_signal(item_id, as_of, reason="too_few_reference_observations")

    listings_today = statistics.median(p.listings_count for p in today_obs)
    listings_30d_ago = statistics.median(p.listings_count for p in reference_obs)

    if listings_30d_ago == 0:
        return _null_signal(item_id, as_of, reason="reference_count_zero")

    relative_change = (listings_today - listings_30d_ago) / listings_30d_ago
    value = max(-1.0, min(1.0, -relative_change))
    return Signal(
        item_id=item_id,
        computed_for=as_of,
        signal_name="supply_velocity",
        value=value,
        metadata={},
    )


def _end_of_day(d: date) -> datetime:
    """Last microsecond of ``d`` in UTC — used for inclusive ``<=`` filters."""
    return datetime.combine(d, time(23, 59, 59, 999999), tzinfo=UTC)


def _take_last_n_before(points: list[ListingPoint], cutoff: datetime, n: int) -> list[ListingPoint]:
    """Return the ``n`` most recent points with ``observed_at <= cutoff``,
    oldest-first.
    """
    eligible = [p for p in points if p.observed_at <= cutoff]
    return eligible[-n:]


def _null_signal(item_id: int, as_of: date, **metadata: object) -> Signal:
    return Signal(
        item_id=item_id,
        computed_for=as_of,
        signal_name="supply_velocity",
        value=None,
        metadata=dict(metadata),
    )
