"""Tests for :mod:`dota_deals.signals.event_proximity`.

The signal's correctness depends on the relationship between three dates:
the as-of date, the upcoming event's start date, and the past event's start
date. The fixtures here pin all three explicitly so the comparable-window
arithmetic is auditable from the test body.

Phase 9c-ii: pure function over a :class:`DataLookup`. Tests construct
events and per-item daily prices directly, including dates outside the
production 95-day prefetch window — the production runner's window limit
is documented in :mod:`dota_deals.signals.dataset`, and a dedicated test
below pins the consequence (signal falls to null when past-event dates
fall outside what the runner pre-fetched).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from dota_deals.models.domain import Item, ItemCategory, Signal
from dota_deals.models.events import EventConfidence, EventKind, EventRecord
from dota_deals.signals import event_proximity
from dota_deals.signals.dataset import DataLookup

AS_OF = date(2026, 5, 13)


# ----------------------------- helpers -----------------------------------------


def _item(item_id: int, *, category: ItemCategory = "arcana") -> Item:
    return Item(
        item_id=item_id,
        market_hash=f"item-{item_id}",
        name=f"item-{item_id}",
        category=category,
        hero=None,
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=None,
        active=True,
    )


def _event(
    event_id: int,
    *,
    kind: EventKind = "ti",
    name: str = "TI",
    start_date: date,
    confidence: EventConfidence = "confirmed",
) -> EventRecord:
    return EventRecord(
        event_id=event_id,
        kind=kind,
        name=name,
        start_date=start_date,
        end_date=None,
        confidence=confidence,
        notes=None,
    )


def _lookup(
    *,
    items: list[Item],
    daily_prices: dict[int, list[tuple[date, int]]] | None = None,
    next_event: EventRecord | None = None,
    past_events_by_kind: dict[str, list[EventRecord]] | None = None,
) -> DataLookup:
    items_by_category: dict[ItemCategory, list[Item]] = {}
    for it in items:
        items_by_category.setdefault(it.category, []).append(it)
    return DataLookup(
        as_of=AS_OF,
        items_by_id={it.item_id: it for it in items},
        items_by_category=items_by_category,
        daily_prices=daily_prices or {},
        listings={},
        latest_observations={},
        next_event=next_event,
        past_events_by_kind=past_events_by_kind or {},
    )


# ----------------------------- tests -------------------------------------------


def test_no_event_within_60_days_returns_null() -> None:
    """No event in the 60-day lookahead → null (signal does not apply).

    The convention changed in Phase 5 from ``0.0`` to ``None`` so the
    scoring layer's renormalization picks up the slack (otherwise the 20%
    weight reserved for event_proximity would silently cap most-of-the-year
    scores at 0.80). See SPEC.md and docs/SCORING.md.
    """
    target = _item(1)
    # DataLookup's next_event is None (the build_data_lookup function fetched
    # nothing within 60 days). That's exactly the no-event case.
    signal = event_proximity.compute(target.item_id, AS_OF, _lookup(items=[target]))
    assert signal.value is None
    assert signal.metadata["reason"] == "no_event_within_60d"


def test_item_level_history_yields_expected_value() -> None:
    """+20% in the prior-year equivalent window → output +0.4.

    Setup:
    - Next event: TI 2026 on AS_OF + 30 days.
    - Past event: TI 2025 on AS_OF - 335 days (≈ a year before next event).
    - Item daily price at (TI 2025 start - 30 days) = $1.00.
    - Item daily price at TI 2025 start = $1.20 → fractional change +0.20.
    - Clipped to [-0.5, 0.5] (unchanged), scaled by 2 → +0.4.
    """
    target = _item(1)
    next_event_start = AS_OF + timedelta(days=30)
    past_event_start = AS_OF - timedelta(days=335)
    next_event = _event(1, name="TI 2026", start_date=next_event_start)
    past_event = _event(2, name="TI 2025", start_date=past_event_start)

    daily = {
        target.item_id: [
            (past_event_start - timedelta(days=30), 10000),
            (past_event_start, 12000),
        ],
    }

    signal = event_proximity.compute(
        target.item_id,
        AS_OF,
        _lookup(
            items=[target],
            daily_prices=daily,
            next_event=next_event,
            past_events_by_kind={"ti": [past_event]},
        ),
    )
    assert signal.value is not None
    assert abs(signal.value - 0.4) < 1e-9
    assert signal.metadata["windows_used"] == 1
    assert signal.metadata.get("fallback") is None


def test_category_fallback_when_item_has_no_history() -> None:
    """Item has no past-window data; 3 peers do → category-based signal + flag."""
    target = _item(1)
    peers = [_item(i) for i in (2, 3, 4)]
    next_event_start = AS_OF + timedelta(days=30)
    past_event_start = AS_OF - timedelta(days=335)
    next_event = _event(1, name="TI 2026", start_date=next_event_start)
    past_event = _event(2, name="TI 2025", start_date=past_event_start)

    # Target has no daily prices in the window; each peer shows +20%.
    daily: dict[int, list[tuple[date, int]]] = {target.item_id: []}
    for peer in peers:
        daily[peer.item_id] = [
            (past_event_start - timedelta(days=30), 10000),
            (past_event_start, 12000),
        ]

    signal = event_proximity.compute(
        target.item_id,
        AS_OF,
        _lookup(
            items=[target, *peers],
            daily_prices=daily,
            next_event=next_event,
            past_events_by_kind={"ti": [past_event]},
        ),
    )
    assert signal.value is not None
    assert abs(signal.value - 0.4) < 1e-9
    assert signal.metadata["fallback"] == "category-based"
    assert signal.metadata["peers_with_data"] == 3


def test_insufficient_peers_for_fallback_returns_null() -> None:
    """<3 peers with past-window data → null (not category-fallback either)."""
    target = _item(1)
    peers = [_item(i) for i in (2, 3)]
    next_event_start = AS_OF + timedelta(days=30)
    past_event_start = AS_OF - timedelta(days=335)
    next_event = _event(1, name="TI 2026", start_date=next_event_start)
    past_event = _event(2, name="TI 2025", start_date=past_event_start)

    daily: dict[int, list[tuple[date, int]]] = {target.item_id: []}
    for peer in peers:
        daily[peer.item_id] = [
            (past_event_start - timedelta(days=30), 10000),
            (past_event_start, 12000),
        ]

    signal = event_proximity.compute(
        target.item_id,
        AS_OF,
        _lookup(
            items=[target, *peers],
            daily_prices=daily,
            next_event=next_event,
            past_events_by_kind={"ti": [past_event]},
        ),
    )
    assert signal.value is None
    assert signal.metadata["reason"] == "insufficient_peers_with_history"
    assert signal.metadata["peers_with_data"] == 2


def test_tentative_event_confidence_propagates() -> None:
    """SPEC: a tentative event-date confidence must surface in metadata."""
    target = _item(1)
    next_event = _event(
        1,
        name="TI 2026 (rumored)",
        start_date=AS_OF + timedelta(days=30),
        confidence="tentative",
    )
    # No past events → signal will be null, but metadata still propagates
    # event_confidence so the display layer can soften the row.
    signal = event_proximity.compute(
        target.item_id,
        AS_OF,
        _lookup(items=[target], next_event=next_event, past_events_by_kind={"ti": []}),
    )
    assert signal.metadata["event_confidence"] == "tentative"


def test_no_past_events_of_kind_returns_null() -> None:
    """An upcoming event with no prior instance of the same kind → null."""
    target = _item(1)
    next_event = _event(
        1,
        kind="crownfall",
        name="Crownfall Part 1",
        start_date=AS_OF + timedelta(days=14),
    )
    signal = event_proximity.compute(
        target.item_id,
        AS_OF,
        _lookup(items=[target], next_event=next_event, past_events_by_kind={"crownfall": []}),
    )
    assert signal.value is None
    assert signal.metadata["reason"] == "no_past_events_of_kind"
    assert signal.metadata["event_kind"] == "crownfall"


def test_past_window_outside_prefetch_falls_to_null() -> None:
    """Phase 9c-ii: the production runner pre-fetches a bounded daily-prices
    window (95 days at v1). When a past event's comparable dates fall outside
    that window, the per-item lookup returns ``None`` and the signal can't
    use the event. With one past event and no peer data, the result is the
    same insufficient-peers null SPEC.md warns about for v1.

    This pins the documented v1 reality: event_proximity is essentially
    always null until a second TI cycle's worth of data has accumulated.
    """
    target = _item(1)
    next_event_start = AS_OF + timedelta(days=30)
    past_event_start = AS_OF - timedelta(days=335)
    next_event = _event(1, name="TI 2026", start_date=next_event_start)
    past_event = _event(2, name="TI 2025", start_date=past_event_start)

    # daily_prices contains ONLY recent dates — no entries for the past
    # window the signal will try to look up.
    daily = {
        target.item_id: [(AS_OF - timedelta(days=offset), 10000) for offset in range(95)],
    }

    signal = event_proximity.compute(
        target.item_id,
        AS_OF,
        _lookup(
            items=[target],
            daily_prices=daily,
            next_event=next_event,
            past_events_by_kind={"ti": [past_event]},
        ),
    )
    assert signal.value is None
    assert signal.metadata["reason"] == "insufficient_peers_with_history"


def test_returned_signal_carries_correct_metadata() -> None:
    """The Signal dataclass fields are populated correctly on the no-event null
    path."""
    target = _item(1)
    signal = event_proximity.compute(target.item_id, AS_OF, _lookup(items=[target]))
    assert isinstance(signal, Signal)
    assert signal.signal_name == "event_proximity"
    assert signal.computed_for == AS_OF
    assert signal.item_id == target.item_id
    assert signal.value is None  # no event within 60 days
