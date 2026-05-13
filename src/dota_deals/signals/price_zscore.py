"""Signal 1: price vs. own history.

Computes a normalized z-score of the item's current price against its 90-day
trimmed-median baseline, sign-flipped so that "below baseline" is positive.

See ``docs/SPEC.md`` for the full formula and failure modes. Implementation
specifics:

* **Window** is the 90 UTC days *before* ``as_of`` (exclusive). The current
  price (``as_of``'s daily price) is compared against window statistics; it
  is not itself in the window.
* **Trimmed median** drops the bottom and top ``floor(N * 0.05)`` values
  from a sorted copy of the window, then takes the median. For ``N=90`` this
  drops 4 from each end. SPEC.md treats trim as outlier protection for the
  *median*, not the stddev.
* **Standard deviation** is population stddev over the full (un-trimmed)
  window — that's the variability the formula compares against.
* **stddev = 0** short-circuits to ``value = 0.0`` (the architecture's
  documented "totally flat price" case).
* **Output** is ``-clip(z, -3, 3) / 3``, in ``[-1, 1]``.

Insufficient-history cases (``< 30`` unique window days, or no daily price
for ``as_of``) emit ``value = None`` with a ``reason`` annotation in
``metadata`` — the runner records the row so downstream "data quality"
reporting can see the gap.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta

from dota_deals.models.domain import Signal
from dota_deals.storage.repositories import daily_prices

_MIN_HISTORY_DAYS = 30
_WINDOW_DAYS = 90
_TRIM_FRACTION = 0.05
_CLIP_LIMIT = 3.0


def compute(conn: sqlite3.Connection, item_id: int, as_of: date) -> Signal:
    """Compute the ``price_zscore`` signal for ``item_id`` as of ``as_of``.

    Never raises for data-quality reasons — those return a ``value=None``
    Signal with a descriptive ``reason`` in ``metadata``. Storage errors and
    other unexpected exceptions are allowed to propagate to the runner.
    """
    history = daily_prices(conn, item_id, days=_WINDOW_DAYS, as_of=as_of - timedelta(days=1))
    if len(history) < _MIN_HISTORY_DAYS:
        return _null_signal(
            item_id, as_of, reason="insufficient_history", days_available=len(history)
        )

    current_row = daily_prices(conn, item_id, days=1, as_of=as_of)
    if not current_row:
        return _null_signal(item_id, as_of, reason="no_daily_price_for_as_of")
    current_cents = current_row[-1][1]

    window_prices = [cents for (_, cents) in history]

    trim = int(len(window_prices) * _TRIM_FRACTION)
    sorted_window = sorted(window_prices)
    trimmed = sorted_window[trim : len(sorted_window) - trim] if trim else sorted_window
    trimmed_median = statistics.median(trimmed)

    stddev = statistics.pstdev(window_prices)
    if stddev == 0:
        return Signal(
            item_id=item_id,
            computed_for=as_of,
            signal_name="price_zscore",
            value=0.0,
            metadata={"reason": "flat_window_stddev_zero"},
        )

    z = (current_cents - trimmed_median) / stddev
    clipped = max(-_CLIP_LIMIT, min(_CLIP_LIMIT, z))
    value = -clipped / _CLIP_LIMIT
    return Signal(
        item_id=item_id,
        computed_for=as_of,
        signal_name="price_zscore",
        value=value,
        metadata={},
    )


def _null_signal(item_id: int, as_of: date, **metadata: object) -> Signal:
    return Signal(
        item_id=item_id,
        computed_for=as_of,
        signal_name="price_zscore",
        value=None,
        metadata=dict(metadata),
    )
