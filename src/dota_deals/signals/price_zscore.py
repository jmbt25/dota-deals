"""Signal 1: price vs. own history.

Computes a normalized z-score of the item's current price against its 90-day
trimmed-median baseline, sign-flipped so that "below baseline" is positive.

See ``docs/SPEC.md`` for the full formula and failure modes.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from dota_deals.models.domain import Signal


def compute(conn: sqlite3.Connection, item_id: int, as_of: date) -> Signal:
    """Compute the ``price_zscore`` signal for ``item_id`` as of ``as_of``.

    Returns a :class:`Signal` whose ``value`` is the normalized z-score, or
    ``None`` if the item has fewer than 30 days of history.

    :raises ValueError: if the item has insufficient history. (Caught by the
        signals runner and recorded as ``value=None``.)
    """
    raise NotImplementedError
