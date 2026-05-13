"""Signal 4: comparables delta.

Compares the item's current price to the median current price of its peer set
(same hero if hero-bound arcana, else same category). Sign-flipped so that
"cheaper than peers" reads positive.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from dota_deals.models.domain import Signal


def compute(conn: sqlite3.Connection, item_id: int, as_of: date) -> Signal:
    """Compute the ``comparables_delta`` signal for ``item_id`` as of ``as_of``.

    Returns a :class:`Signal` whose ``value`` is the clipped relative delta vs.
    the peer median, or ``None`` if the peer set has fewer than three items
    with a current price.

    :raises ValueError: on internal contract violations (e.g. unknown item).
    """
    raise NotImplementedError
