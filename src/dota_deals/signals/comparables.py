"""Signal 4: comparables delta.

Compares the item's current price (latest known) to the median current
price of its category peers, sign-flipped so "cheaper than peers" reads
positive. Self is excluded from the peer set; the v1 peer scope is
**category-level only** because ``items.hero`` is NULL until the hero-
parsing followup (see docs/FUTURE.md) lands.

Failure modes (return null with ``reason`` in metadata):

* The item itself isn't in ``items``.
* No row in ``latest_observation`` for the item (never been ingested).
* Fewer than 3 peers with a current price (median wouldn't be meaningful).
* Peer median == 0 (defensive — schema's CHECK on ``lowest_cents`` prevents
  this in practice, but the formula is undefined either way).
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date

from dota_deals.models.domain import Signal
from dota_deals.storage.db import StorageError
from dota_deals.storage.repositories import active_items_in_category, get_item_by_id

_MIN_PEERS = 3


def compute(conn: sqlite3.Connection, item_id: int, as_of: date) -> Signal:
    """Compute the ``comparables_delta`` signal for ``item_id`` as of ``as_of``."""
    item = get_item_by_id(conn, item_id)
    if item is None:
        return _null(item_id, as_of, reason="item_not_found")

    item_price = _latest_lowest_cents(conn, item_id)
    if item_price is None:
        return _null(item_id, as_of, reason="no_current_price")

    peers = active_items_in_category(conn, item.category, exclude_item_id=item_id)
    peer_prices: list[int] = []
    for peer in peers:
        price = _latest_lowest_cents(conn, peer.item_id)
        if price is not None:
            peer_prices.append(price)

    if len(peer_prices) < _MIN_PEERS:
        return _null(
            item_id,
            as_of,
            reason="insufficient_peers",
            peers_with_price=len(peer_prices),
        )

    peer_median = statistics.median(peer_prices)
    if peer_median == 0:
        return _null(item_id, as_of, reason="peer_median_zero")

    relative = (item_price - peer_median) / peer_median
    value = max(-1.0, min(1.0, -relative))
    return Signal(
        item_id=item_id,
        computed_for=as_of,
        signal_name="comparables_delta",
        value=value,
        metadata={"peers_with_price": len(peer_prices)},
    )


def _latest_lowest_cents(conn: sqlite3.Connection, item_id: int) -> int | None:
    """Return the ``lowest_cents`` from ``latest_observation`` for ``item_id``."""
    try:
        row = conn.execute(
            "SELECT lowest_cents FROM latest_observation WHERE item_id = ?",
            (item_id,),
        ).fetchone()
    except sqlite3.Error as e:
        raise StorageError(f"latest_observation lookup failed for item_id={item_id}: {e}") from e
    return int(row["lowest_cents"]) if row is not None else None


def _null(item_id: int, as_of: date, **metadata: object) -> Signal:
    return Signal(
        item_id=item_id,
        computed_for=as_of,
        signal_name="comparables_delta",
        value=None,
        metadata=dict(metadata),
    )
