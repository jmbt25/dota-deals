"""Signal 3: event proximity.

Looks at the item's (or its category's) price behavior in prior equivalent
event windows and outputs the median fractional change, scaled into
``[-1, 1]``. In v1 (forward-fill only, ≤ 365 days history) the item-level
lookup is essentially always empty and the category-level fallback carries
the signal; the implementation handles both correctly.

Algorithm
---------
1. Find the **next event** within 60 days. If none, output ``None`` — the
   signal "does not apply right now", and the scoring layer renormalizes
   the remaining signal weights accordingly. (SPEC.md originally said
   ``0.0``; that biased scores toward zero most of the year because events
   are sparse. The current convention keeps the score's interpretation
   stable across event-rich and event-poor windows.)
2. Compute ``days_until = next_event.start_date - as_of``.
3. For each **past event of the same kind**, look up the item's daily price at
   ``past_event.start_date - days_until`` (the "start of the comparable
   window") and at ``past_event.start_date`` (the "end"). Compute
   ``(end - start) / start``.
4. If the item has at least one such pair, output the median of those
   fractional changes (clipped to ``[-0.5, 0.5]``, scaled to ``[-1, 1]``).
5. Otherwise fall back to category peers (active, same category, self
   excluded). Each peer contributes its own median fractional change; we
   need **at least 3 peers with usable past-window data** before emitting
   a value. Below that threshold the signal is null.

Metadata
--------
On success, ``metadata`` includes the event id/kind/confidence, the
``days_until_event``, and either ``windows_used`` (item-level) or
``fallback="category-based"`` plus ``peers_with_data`` (fallback path).
A tentative event date is surfaced as ``event_confidence="tentative"`` so
downstream display can soften the recommendation.
"""

from __future__ import annotations

import sqlite3
import statistics
from datetime import date, timedelta

from dota_deals.models.domain import Signal
from dota_deals.models.events import EventRecord
from dota_deals.storage.repositories import (
    active_items_in_category,
    daily_prices,
    get_item_by_id,
    next_event_within,
    past_events_of_kind,
)

_LOOKAHEAD_DAYS = 60
_MIN_PEERS_WITH_DATA = 3
_CLIP_LIMIT = 0.5  # fractional change clipped here, then scaled to [-1, 1]


def compute(conn: sqlite3.Connection, item_id: int, as_of: date) -> Signal:
    """Compute the ``event_proximity`` signal for ``item_id`` as of ``as_of``."""
    next_event = next_event_within(conn, as_of, days_window=_LOOKAHEAD_DAYS)
    if next_event is None:
        # Convention: no upcoming event → null, not zero. Scoring renormalizes.
        return _signal_with(item_id, as_of, value=None, metadata={"reason": "no_event_within_60d"})

    days_until = (next_event.start_date - as_of).days
    base_metadata: dict[str, object] = {
        "event_id": next_event.event_id,
        "event_kind": next_event.kind,
        "event_confidence": next_event.confidence,
        "days_until_event": days_until,
    }

    past = past_events_of_kind(conn, next_event.kind, before=as_of)
    if not past:
        return _signal_with(
            item_id,
            as_of,
            value=None,
            metadata=base_metadata | {"reason": "no_past_events_of_kind"},
        )

    item = get_item_by_id(conn, item_id)
    if item is None:
        return _signal_with(
            item_id, as_of, value=None, metadata=base_metadata | {"reason": "item_not_found"}
        )

    # Item-level first.
    item_changes = _changes_in_past_windows(conn, item_id, past, days_until)
    if item_changes:
        median_change = statistics.median(item_changes)
        return _signal_with(
            item_id,
            as_of,
            value=_clip_and_scale(median_change),
            metadata=base_metadata | {"windows_used": len(item_changes)},
        )

    # Category-level fallback. Need ≥ _MIN_PEERS_WITH_DATA peers with usable
    # past-window data.
    peers = active_items_in_category(conn, item.category, exclude_item_id=item_id)
    peer_medians: list[float] = []
    for peer in peers:
        peer_changes = _changes_in_past_windows(conn, peer.item_id, past, days_until)
        if peer_changes:
            peer_medians.append(statistics.median(peer_changes))

    if len(peer_medians) < _MIN_PEERS_WITH_DATA:
        return _signal_with(
            item_id,
            as_of,
            value=None,
            metadata=base_metadata
            | {
                "reason": "insufficient_peers_with_history",
                "peers_with_data": len(peer_medians),
            },
        )

    median_change = statistics.median(peer_medians)
    return _signal_with(
        item_id,
        as_of,
        value=_clip_and_scale(median_change),
        metadata=base_metadata
        | {"fallback": "category-based", "peers_with_data": len(peer_medians)},
    )


def _changes_in_past_windows(
    conn: sqlite3.Connection,
    item_id: int,
    past_events: list[EventRecord],
    days_until: int,
) -> list[float]:
    """Fractional price changes across each ``(past_event.start - days_until,
    past_event.start)`` window. Skips events where either endpoint has no
    daily price for ``item_id``.
    """
    changes: list[float] = []
    for event in past_events:
        window_start = event.start_date - timedelta(days=days_until)
        start_rows = daily_prices(conn, item_id, days=1, as_of=window_start)
        end_rows = daily_prices(conn, item_id, days=1, as_of=event.start_date)
        if not start_rows or not end_rows:
            continue
        start_price = start_rows[-1][1]
        end_price = end_rows[-1][1]
        if start_price <= 0:
            continue
        changes.append((end_price - start_price) / start_price)
    return changes


def _clip_and_scale(fractional_change: float) -> float:
    """Clip to ``[-0.5, 0.5]`` and scale to ``[-1, 1]``."""
    clipped = max(-_CLIP_LIMIT, min(_CLIP_LIMIT, fractional_change))
    return clipped * 2.0


def _signal_with(
    item_id: int,
    as_of: date,
    *,
    value: float | None,
    metadata: dict[str, object],
) -> Signal:
    return Signal(
        item_id=item_id,
        computed_for=as_of,
        signal_name="event_proximity",
        value=value,
        metadata=metadata,
    )
