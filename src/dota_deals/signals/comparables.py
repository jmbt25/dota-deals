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

Phase 9c-ii: pure function over a pre-fetched :class:`DataLookup`. The
peers list comes from the lookup's items-by-category index; peer prices
come from the latest_observations dict. No DB calls in this module.
"""

from __future__ import annotations

import statistics
from datetime import date

from dota_deals.models.domain import Signal
from dota_deals.signals.dataset import DataLookup

_MIN_PEERS = 3


def compute(item_id: int, as_of: date, data: DataLookup) -> Signal:
    """Compute the ``comparables_delta`` signal for ``item_id`` as of ``as_of``."""
    item = data.item(item_id)
    if item is None:
        return _null(item_id, as_of, reason="item_not_found")

    item_price = data.latest_lowest_cents_for(item_id)
    if item_price is None:
        return _null(item_id, as_of, reason="no_current_price")

    peers = data.peers(item.category, exclude_item_id=item_id)
    peer_prices: list[int] = []
    for peer in peers:
        price = data.latest_lowest_cents_for(peer.item_id)
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


def _null(item_id: int, as_of: date, **metadata: object) -> Signal:
    return Signal(
        item_id=item_id,
        computed_for=as_of,
        signal_name="comparables_delta",
        value=None,
        metadata=dict(metadata),
    )
