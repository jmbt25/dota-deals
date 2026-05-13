"""Tests for :mod:`dota_deals.signals.event_proximity`.

The signal's correctness depends on the relationship between three dates:
the as-of date, the upcoming event's start date, and the past event's start
date. The fixtures here pin all three explicitly so the comparable-window
arithmetic is auditable from the test body.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time, timedelta

from dota_deals.models.domain import Signal
from dota_deals.models.events import EventConfidence, EventKind, EventRecord
from dota_deals.signals import event_proximity
from dota_deals.storage.repositories import insert_event
from tests.conftest import insert_test_item

AS_OF = date(2026, 5, 13)


# ----------------------------- helpers -----------------------------------------


def _insert_price(conn: sqlite3.Connection, item_id: int, *, on: date, cents: int) -> None:
    """Insert a single price_history observation at noon UTC for ``on``."""
    conn.execute(
        "INSERT INTO price_history (item_id, observed_at, lowest_cents) VALUES (?, ?, ?)",
        (item_id, datetime.combine(on, time(12), tzinfo=UTC).isoformat(), cents),
    )


def _insert_event(
    conn: sqlite3.Connection,
    *,
    kind: EventKind = "ti",
    name: str = "TI",
    start_date: date,
    confidence: EventConfidence = "confirmed",
) -> int:
    return insert_event(
        conn,
        EventRecord(
            event_id=None,
            kind=kind,
            name=name,
            start_date=start_date,
            end_date=None,
            confidence=confidence,
            notes=None,
        ),
    )


# ----------------------------- tests -------------------------------------------


def test_no_event_within_60_days_returns_null(db_conn: sqlite3.Connection) -> None:
    """No event in the 60-day lookahead → null (signal does not apply).

    The convention changed in Phase 5 from ``0.0`` to ``None`` so the
    scoring layer's renormalization picks up the slack (otherwise the 20%
    weight reserved for event_proximity would silently cap most-of-the-year
    scores at 0.80). See SPEC.md and docs/SCORING.md.
    """
    item_id = insert_test_item(db_conn, market_hash="X", category="arcana")
    _insert_event(db_conn, start_date=AS_OF + timedelta(days=120))  # far future
    db_conn.commit()

    signal = event_proximity.compute(db_conn, item_id, AS_OF)
    assert signal.value is None
    assert signal.metadata["reason"] == "no_event_within_60d"


def test_item_level_history_yields_expected_value(db_conn: sqlite3.Connection) -> None:
    """+20% in the prior-year equivalent window → output +0.4.

    Setup:
    - Next event: TI 2026 on AS_OF + 30 days.
    - Past event: TI 2025 on AS_OF - 335 days (≈ a year before next event).
    - Item daily price at (TI 2025 start - 30 days) = $1.00.
    - Item daily price at TI 2025 start = $1.20 → fractional change +0.20.
    - Clipped to [-0.5, 0.5] (unchanged), scaled by 2 → +0.4.
    """
    item_id = insert_test_item(db_conn, market_hash="X", category="arcana")
    next_event_start = AS_OF + timedelta(days=30)
    past_event_start = AS_OF - timedelta(days=335)
    _insert_event(db_conn, name="TI 2026", start_date=next_event_start)
    _insert_event(db_conn, name="TI 2025", start_date=past_event_start)
    # 30 days before TI 2025: $1.00. At TI 2025: $1.20.
    _insert_price(db_conn, item_id, on=past_event_start - timedelta(days=30), cents=10000)
    _insert_price(db_conn, item_id, on=past_event_start, cents=12000)
    db_conn.commit()

    signal = event_proximity.compute(db_conn, item_id, AS_OF)
    assert signal.value is not None
    assert abs(signal.value - 0.4) < 1e-9
    assert signal.metadata["windows_used"] == 1
    assert signal.metadata.get("fallback") is None


def test_category_fallback_when_item_has_no_history(db_conn: sqlite3.Connection) -> None:
    """Item has no past-window data; 3 peers do → category-based signal + flag."""
    target = insert_test_item(db_conn, market_hash="X", category="arcana")
    peers = [insert_test_item(db_conn, market_hash=f"P{i}", category="arcana") for i in range(3)]

    next_event_start = AS_OF + timedelta(days=30)
    past_event_start = AS_OF - timedelta(days=335)
    _insert_event(db_conn, name="TI 2026", start_date=next_event_start)
    _insert_event(db_conn, name="TI 2025", start_date=past_event_start)

    # Each peer: +20% in past window. Target has no history → falls back.
    for peer in peers:
        _insert_price(db_conn, peer, on=past_event_start - timedelta(days=30), cents=10000)
        _insert_price(db_conn, peer, on=past_event_start, cents=12000)
    db_conn.commit()

    signal = event_proximity.compute(db_conn, target, AS_OF)
    assert signal.value is not None
    assert abs(signal.value - 0.4) < 1e-9
    assert signal.metadata["fallback"] == "category-based"
    assert signal.metadata["peers_with_data"] == 3


def test_insufficient_peers_for_fallback_returns_null(
    db_conn: sqlite3.Connection,
) -> None:
    """<3 peers with past-window data → null (not category-fallback either)."""
    target = insert_test_item(db_conn, market_hash="X", category="arcana")
    peers = [insert_test_item(db_conn, market_hash=f"P{i}", category="arcana") for i in range(2)]

    next_event_start = AS_OF + timedelta(days=30)
    past_event_start = AS_OF - timedelta(days=335)
    _insert_event(db_conn, name="TI 2026", start_date=next_event_start)
    _insert_event(db_conn, name="TI 2025", start_date=past_event_start)

    for peer in peers:
        _insert_price(db_conn, peer, on=past_event_start - timedelta(days=30), cents=10000)
        _insert_price(db_conn, peer, on=past_event_start, cents=12000)
    db_conn.commit()

    signal = event_proximity.compute(db_conn, target, AS_OF)
    assert signal.value is None
    assert signal.metadata["reason"] == "insufficient_peers_with_history"
    assert signal.metadata["peers_with_data"] == 2


def test_tentative_event_confidence_propagates(db_conn: sqlite3.Connection) -> None:
    """SPEC: a tentative event-date confidence must surface in metadata."""
    item_id = insert_test_item(db_conn, market_hash="X", category="arcana")
    next_event_start = AS_OF + timedelta(days=30)
    _insert_event(
        db_conn, name="TI 2026 (rumored)", start_date=next_event_start, confidence="tentative"
    )
    # No past events → signal will be null, but the metadata still
    # propagates event_confidence so the display layer can soften the row.
    db_conn.commit()

    signal = event_proximity.compute(db_conn, item_id, AS_OF)
    assert signal.metadata["event_confidence"] == "tentative"


def test_no_past_events_of_kind_returns_null(db_conn: sqlite3.Connection) -> None:
    """An upcoming event with no prior instance of the same kind → null."""
    item_id = insert_test_item(db_conn, market_hash="X", category="arcana")
    _insert_event(
        db_conn,
        kind="crownfall",
        name="Crownfall Part 1",
        start_date=AS_OF + timedelta(days=14),
    )
    db_conn.commit()

    signal = event_proximity.compute(db_conn, item_id, AS_OF)
    assert signal.value is None
    assert signal.metadata["reason"] == "no_past_events_of_kind"
    assert signal.metadata["event_kind"] == "crownfall"


def test_returned_signal_carries_correct_metadata(db_conn: sqlite3.Connection) -> None:
    """The Signal dataclass fields are populated correctly even on the
    no-event null path."""
    item_id = insert_test_item(db_conn, market_hash="X", category="arcana")
    _insert_event(db_conn, start_date=AS_OF + timedelta(days=120))
    db_conn.commit()

    signal = event_proximity.compute(db_conn, item_id, AS_OF)
    assert isinstance(signal, Signal)
    assert signal.signal_name == "event_proximity"
    assert signal.computed_for == AS_OF
    assert signal.item_id == item_id
    assert signal.value is None  # no event within 60 days
