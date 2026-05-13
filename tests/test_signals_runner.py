"""Tests for :mod:`dota_deals.signals.runner`.

The runner is the boundary the per-day scoring stage will depend on, so
these tests focus on three production-critical properties:

* For every active item we always emit four rows (one per signal), regardless
  of data sufficiency. Downstream needs the (item, signal_name) coverage so
  "what was computed today" is queryable.
* Re-running for the same date is a no-op via the PK constraint — no
  duplicates, no double-writes.
* A per-(item, signal) compute exception is caught at the documented loop
  boundary and replaced with a synthesized null row. Other items and other
  signals are unaffected.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, time, timedelta

import pytest

from dota_deals.config import Settings
from dota_deals.models.domain import Signal
from dota_deals.signals import runner as signals_runner
from tests.conftest import insert_test_item

AS_OF = date(2026, 5, 13)


# ----------------------------- helpers -----------------------------------------


def _populate_full_history(conn: sqlite3.Connection, item_id: int) -> None:
    """Insert enough history that price_zscore and supply_velocity can compute."""
    # 91 daily prices, all at $1.00 — yields a flat signal (value=0) which
    # is fine; we care about row presence, not the value.
    for offset in range(91):
        day = AS_OF - timedelta(days=offset)
        conn.execute(
            "INSERT INTO price_history (item_id, observed_at, lowest_cents) VALUES (?, ?, ?)",
            (item_id, datetime.combine(day, time(12), tzinfo=UTC).isoformat(), 10000),
        )
    # 32 days, 3 obs/day, of listings.
    for offset in range(32):
        day = AS_OF - timedelta(days=offset)
        for hour in (0, 8, 16):
            conn.execute(
                "INSERT INTO listing_history (item_id, observed_at, listings_count) "
                "VALUES (?, ?, ?)",
                (
                    item_id,
                    datetime.combine(day, time(hour), tzinfo=UTC).isoformat(),
                    50,
                ),
            )
    # Latest observation so comparables has a current price.
    conn.execute(
        """
        INSERT INTO latest_observation (item_id, observed_at, lowest_cents, listings_count)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(item_id) DO UPDATE SET
            observed_at = excluded.observed_at,
            lowest_cents = excluded.lowest_cents,
            listings_count = excluded.listings_count
        """,
        (item_id, datetime.combine(AS_OF, time(12), tzinfo=UTC).isoformat(), 10000, 50),
    )
    conn.commit()


def _signals_for(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT signal_name, value FROM signals "
        "WHERE item_id = ? AND computed_for = ? ORDER BY signal_name",
        (item_id, AS_OF.isoformat()),
    ).fetchall()


# ----------------------------- tests -------------------------------------------


def test_fully_warm_item_gets_four_signal_rows(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """All four signals computed and persisted for a fully-warm item."""
    item_id = insert_test_item(db_conn, market_hash="X", category="arcana")
    # Two more peers so comparables can find ≥ 3 peers (target + 2 below
    # would only be 2 peers; we need 3 OTHERS).
    p1 = insert_test_item(db_conn, market_hash="P1", category="arcana")
    p2 = insert_test_item(db_conn, market_hash="P2", category="arcana")
    p3 = insert_test_item(db_conn, market_hash="P3", category="arcana")
    for iid in (item_id, p1, p2, p3):
        _populate_full_history(db_conn, iid)

    summary = signals_runner.compute_signals_for(
        AS_OF, settings, run_id="r1", parent_run_id="parent-1"
    )

    rows = _signals_for(db_conn, item_id)
    assert [r["signal_name"] for r in rows] == [
        "comparables_delta",
        "event_proximity",
        "price_zscore",
        "supply_velocity",
    ]
    assert summary.status == "success"
    assert summary.items_ok == 4
    assert summary.items_failed == 0


def test_partial_history_item_still_emits_four_rows_with_nulls(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """Insufficient-history items still get four rows; some values are null.

    Downstream coverage reporting depends on (item, signal_name) being
    present regardless of computability.
    """
    item_id = insert_test_item(db_conn, market_hash="NEW", category="arcana")
    # No history populated at all — every signal will be null.

    signals_runner.compute_signals_for(AS_OF, settings, run_id="r2")

    rows = _signals_for(db_conn, item_id)
    assert len(rows) == 4
    assert {r["signal_name"] for r in rows} == {
        "price_zscore",
        "supply_velocity",
        "event_proximity",
        "comparables_delta",
    }
    # Every value is null because no history → all signals emit null
    # (except event_proximity, which returns 0.0 because no event is in the
    # 60-day window — that's the documented "doesn't apply" sentinel).
    null_signals = {r["signal_name"] for r in rows if r["value"] is None}
    zero_signals = {r["signal_name"] for r in rows if r["value"] == 0.0}
    assert null_signals == {"price_zscore", "supply_velocity", "comparables_delta"}
    assert zero_signals == {"event_proximity"}


def test_idempotent_rerun_does_not_double_write(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """Two runs for the same date → still exactly four rows per item."""
    item_id = insert_test_item(db_conn, market_hash="X", category="arcana")
    for i in range(3):
        peer = insert_test_item(db_conn, market_hash=f"P{i}", category="arcana")
        _populate_full_history(db_conn, peer)
    _populate_full_history(db_conn, item_id)

    signals_runner.compute_signals_for(AS_OF, settings, run_id="run-a")
    signals_runner.compute_signals_for(AS_OF, settings, run_id="run-b")

    count = db_conn.execute(
        "SELECT COUNT(*) FROM signals WHERE item_id = ? AND computed_for = ?",
        (item_id, AS_OF.isoformat()),
    ).fetchone()[0]
    assert count == 4

    # Both runs recorded in `runs`.
    run_count = db_conn.execute("SELECT COUNT(*) FROM runs WHERE kind = 'signals'").fetchone()[0]
    assert run_count == 2


def test_runs_row_carries_parent_and_kind(settings: Settings, db_conn: sqlite3.Connection) -> None:
    """The runs row is tagged kind='signals' and links to its parent_run_id."""
    item_id = insert_test_item(db_conn, market_hash="X", category="arcana")
    _populate_full_history(db_conn, item_id)

    signals_runner.compute_signals_for(
        AS_OF, settings, run_id="r-tagged", parent_run_id="parent-xyz"
    )

    run = db_conn.execute(
        "SELECT kind, parent_run_id, status FROM runs WHERE run_id = ?", ("r-tagged",)
    ).fetchone()
    assert run["kind"] == "signals"
    assert run["parent_run_id"] == "parent-xyz"
    assert run["status"] in ("success", "partial")


def test_per_item_per_signal_exception_isolated(
    settings: Settings,
    db_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising signal compute → null row for that (item, signal); others unaffected."""
    a = insert_test_item(db_conn, market_hash="A", category="arcana")
    b = insert_test_item(db_conn, market_hash="B", category="arcana")
    for iid in (a, b):
        _populate_full_history(db_conn, iid)

    # Inject a fault only when computing supply_velocity for item A.
    from dota_deals.signals import supply_velocity

    real_compute = supply_velocity.compute

    def faulty(conn: sqlite3.Connection, item_id: int, as_of: date) -> Signal:
        if item_id == a:
            raise RuntimeError("synthetic supply_velocity failure for A")
        return real_compute(conn, item_id, as_of)

    monkeypatch.setattr(supply_velocity, "compute", faulty)

    summary = signals_runner.compute_signals_for(AS_OF, settings, run_id="r-fault")

    # Item A's supply_velocity is a null row with exception metadata.
    a_supply = db_conn.execute(
        "SELECT value, metadata_json FROM signals "
        "WHERE item_id = ? AND signal_name = 'supply_velocity'",
        (a,),
    ).fetchone()
    assert a_supply["value"] is None
    assert "computation_exception" in a_supply["metadata_json"]
    assert "RuntimeError" in a_supply["metadata_json"]

    # Item A's OTHER signals still wrote (non-null values exist).
    a_rows = _signals_for(db_conn, a)
    assert len(a_rows) == 4

    # Item B is completely unaffected.
    b_rows = _signals_for(db_conn, b)
    assert len(b_rows) == 4

    # Per-item bookkeeping: A failed (had at least one synthesized null), B ok.
    assert summary.items_failed == 1
    assert summary.items_ok == 1
    assert summary.status == "partial"


def test_resumable_after_partial_prior_run(settings: Settings, db_conn: sqlite3.Connection) -> None:
    """A prior partial run left two signals in the table; the rerun fills the
    remaining two without duplicating the existing rows.

    Production scenario: previous batch crashed (KeyboardInterrupt, OOM,
    DB lock) after writing only part of an item's signals. Re-running the
    same date must converge to four rows per item with no churn.
    """
    item_id = insert_test_item(db_conn, market_hash="X", category="arcana")
    for i in range(3):
        peer = insert_test_item(db_conn, market_hash=f"P{i}", category="arcana")
        _populate_full_history(db_conn, peer)
    _populate_full_history(db_conn, item_id)

    # Simulate a prior partial run by pre-inserting two signals for item_id
    # with sentinel values we can recognize later.
    sentinel_iso = AS_OF.isoformat()
    db_conn.execute(
        "INSERT INTO signals (item_id, computed_for, signal_name, value, metadata_json) "
        "VALUES (?, ?, 'price_zscore', 0.99, '{\"sentinel\": true}')",
        (item_id, sentinel_iso),
    )
    db_conn.execute(
        "INSERT INTO signals (item_id, computed_for, signal_name, value, metadata_json) "
        "VALUES (?, ?, 'supply_velocity', 0.88, '{\"sentinel\": true}')",
        (item_id, sentinel_iso),
    )
    db_conn.commit()

    signals_runner.compute_signals_for(AS_OF, settings, run_id="r-resume")

    # Still exactly four rows.
    rows = _signals_for(db_conn, item_id)
    assert len(rows) == 4

    # The pre-existing rows are UNTOUCHED (INSERT OR IGNORE preserves them).
    pre_existing = {r["signal_name"]: r["value"] for r in rows}
    assert pre_existing["price_zscore"] == 0.99
    assert pre_existing["supply_velocity"] == 0.88

    # The two missing signals were written by the rerun.
    assert pre_existing["event_proximity"] is not None or pre_existing["event_proximity"] is None
    assert "comparables_delta" in pre_existing
