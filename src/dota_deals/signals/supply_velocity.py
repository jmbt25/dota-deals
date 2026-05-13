"""Signal 2: supply dynamics.

Computes the 30-day relative change in listing count, sign-flipped so that a
supply drop reads positive. Uses a 3-observation median at each endpoint to
suppress single-poll outliers.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from dota_deals.models.domain import Signal


def compute(conn: sqlite3.Connection, item_id: int, as_of: date) -> Signal:
    """Compute the ``supply_velocity`` signal for ``item_id`` as of ``as_of``.

    Returns a :class:`Signal` whose ``value`` is the clipped relative change,
    or ``None`` if the item has fewer than 14 days of listings history or
    ``listings_30d_ago`` is zero.

    :raises ValueError: if the item has insufficient history.
    """
    raise NotImplementedError
