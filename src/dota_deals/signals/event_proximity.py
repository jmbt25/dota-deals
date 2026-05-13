"""Signal 3: event proximity.

Looks up the item's (or its category's) price behavior in equivalent
past-event windows, returning the median percentage change scaled into
[-1, 1]. In v1 the item-level lookup almost always falls back to
category-level because we don't yet have multi-year history.
"""

from __future__ import annotations

import sqlite3
from datetime import date

from dota_deals.models.domain import Signal


def compute(conn: sqlite3.Connection, item_id: int, as_of: date) -> Signal:
    """Compute the ``event_proximity`` signal for ``item_id`` as of ``as_of``.

    Returns a :class:`Signal` whose ``value`` is the clipped, scaled past-window
    median, or ``None`` if no usable past data exists for the item *or* its
    category. When category-fallback is used, ``Signal.metadata`` carries
    ``{"fallback": "category-based"}``.

    :raises ValueError: if no event is within 60 days *and* the formula does
        not also emit 0.0 in that case (see SPEC.md for the exact rule).
    """
    raise NotImplementedError
