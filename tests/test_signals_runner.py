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

Phase 9c-ii: the runner is async and writes to D1. Tests use the
``db_conn_async`` fixture and seed the in-memory fake before invocation;
assertions read back via ``conn.query``.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from typing import Any

import pytest

from dota_deals.config import Settings
from dota_deals.models.domain import Signal
from dota_deals.signals import runner as signals_runner
from dota_deals.signals.dataset import DataLookup
from dota_deals.storage.db_async import D1Connection
from tests._d1_fake import D1FakeClient
from tests.conftest import insert_test_item_async

AS_OF = date(2026, 5, 13)


# ----------------------------- helpers -----------------------------------------


async def _populate_full_history(conn: D1Connection, item_id: int) -> None:
    """Insert enough history that price_zscore and supply_velocity can compute."""
    # 91 daily prices at $1.00 (flat → signal == 0 but valid; we only assert
    # row presence here).
    price_rows = [
        (
            item_id,
            datetime.combine(AS_OF - timedelta(days=offset), time(12), tzinfo=UTC).isoformat(),
            10000,
        )
        for offset in range(91)
    ]
    for row in price_rows:
        await conn.execute(
            "INSERT INTO price_history (item_id, observed_at, lowest_cents) VALUES (?, ?, ?)",
            row,
        )
    # 32 days, 3 obs/day, of listings.
    for offset in range(32):
        day = AS_OF - timedelta(days=offset)
        for hour in (0, 8, 16):
            await conn.execute(
                "INSERT INTO listing_history (item_id, observed_at, listings_count) "
                "VALUES (?, ?, ?)",
                (
                    item_id,
                    datetime.combine(day, time(hour), tzinfo=UTC).isoformat(),
                    50,
                ),
            )
    # Latest observation so comparables has a current price.
    await conn.execute(
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


async def _signals_for(conn: D1Connection, item_id: int) -> list[dict[str, Any]]:
    result = await conn.query(
        "SELECT signal_name, value, metadata_json FROM signals "
        "WHERE item_id = ? AND computed_for = ? ORDER BY signal_name",
        (item_id, AS_OF.isoformat()),
    )
    return result.results


async def _count(conn: D1Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    result = await conn.query(sql, params)
    return int(result.results[0]["n"])


# ----------------------------- tests -------------------------------------------


@pytest.mark.asyncio
async def test_fully_warm_item_gets_four_signal_rows(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    """All four signals computed and persisted for a fully-warm item."""
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="X", category="arcana")
    # Three peers so comparables clears the ≥3-peer threshold.
    p1 = await insert_test_item_async(conn, market_hash="P1", category="arcana")
    p2 = await insert_test_item_async(conn, market_hash="P2", category="arcana")
    p3 = await insert_test_item_async(conn, market_hash="P3", category="arcana")
    for iid in (item_id, p1, p2, p3):
        await _populate_full_history(conn, iid)

    summary = await signals_runner.compute_signals_for(
        AS_OF, settings, run_id="r1", parent_run_id="parent-1", backend=fake
    )

    rows = await _signals_for(conn, item_id)
    assert [r["signal_name"] for r in rows] == [
        "comparables_delta",
        "event_proximity",
        "price_zscore",
        "supply_velocity",
    ]
    assert summary.status == "success"
    assert summary.items_ok == 4
    assert summary.items_failed == 0


@pytest.mark.asyncio
async def test_partial_history_item_still_emits_four_rows_with_nulls(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    """Insufficient-history items still get four rows; some values are null.

    Downstream coverage reporting depends on (item, signal_name) being
    present regardless of computability.
    """
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="NEW", category="arcana")
    # No history populated — every signal will be null.

    await signals_runner.compute_signals_for(AS_OF, settings, run_id="r2", backend=fake)

    rows = await _signals_for(conn, item_id)
    assert len(rows) == 4
    assert {r["signal_name"] for r in rows} == {
        "price_zscore",
        "supply_velocity",
        "event_proximity",
        "comparables_delta",
    }
    # Every signal emits null on a cold-start item: three because of
    # insufficient data, event_proximity because no event is in the 60-day
    # window.
    null_signals = {r["signal_name"] for r in rows if r["value"] is None}
    assert null_signals == {
        "price_zscore",
        "supply_velocity",
        "event_proximity",
        "comparables_delta",
    }


@pytest.mark.asyncio
async def test_idempotent_rerun_does_not_double_write(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    """Two runs for the same date → still exactly four rows per item."""
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="X", category="arcana")
    for i in range(3):
        peer = await insert_test_item_async(conn, market_hash=f"P{i}", category="arcana")
        await _populate_full_history(conn, peer)
    await _populate_full_history(conn, item_id)

    await signals_runner.compute_signals_for(AS_OF, settings, run_id="run-a", backend=fake)
    await signals_runner.compute_signals_for(AS_OF, settings, run_id="run-b", backend=fake)

    count = await _count(
        conn,
        "SELECT COUNT(*) AS n FROM signals WHERE item_id = ? AND computed_for = ?",
        (item_id, AS_OF.isoformat()),
    )
    assert count == 4

    # Both runs recorded in `runs`.
    run_count = await _count(conn, "SELECT COUNT(*) AS n FROM runs WHERE kind = 'signals'")
    assert run_count == 2


@pytest.mark.asyncio
async def test_runs_row_carries_parent_and_kind(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    """The runs row is tagged kind='signals' and links to its parent_run_id."""
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="X", category="arcana")
    await _populate_full_history(conn, item_id)

    await signals_runner.compute_signals_for(
        AS_OF, settings, run_id="r-tagged", parent_run_id="parent-xyz", backend=fake
    )

    result = await conn.query(
        "SELECT kind, parent_run_id, status FROM runs WHERE run_id = ?", ("r-tagged",)
    )
    row = result.results[0]
    assert row["kind"] == "signals"
    assert row["parent_run_id"] == "parent-xyz"
    assert row["status"] in ("success", "partial")


@pytest.mark.asyncio
async def test_per_item_per_signal_exception_isolated(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising signal compute → null row for that (item, signal); others unaffected."""
    conn, fake = db_conn_async
    a = await insert_test_item_async(conn, market_hash="A", category="arcana")
    b = await insert_test_item_async(conn, market_hash="B", category="arcana")
    for iid in (a, b):
        await _populate_full_history(conn, iid)

    # Inject a fault only when computing supply_velocity for item A.
    from dota_deals.signals import supply_velocity

    real_compute = supply_velocity.compute

    def faulty(item_id: int, as_of: date, data: DataLookup) -> Signal:
        if item_id == a:
            raise RuntimeError("synthetic supply_velocity failure for A")
        return real_compute(item_id, as_of, data)

    monkeypatch.setattr(supply_velocity, "compute", faulty)

    summary = await signals_runner.compute_signals_for(
        AS_OF, settings, run_id="r-fault", backend=fake
    )

    # Item A's supply_velocity is a null row with exception metadata.
    result = await conn.query(
        "SELECT value, metadata_json FROM signals "
        "WHERE item_id = ? AND signal_name = 'supply_velocity'",
        (a,),
    )
    row = result.results[0]
    assert row["value"] is None
    assert "computation_exception" in row["metadata_json"]
    assert "RuntimeError" in row["metadata_json"]

    # Item A's OTHER signals still wrote.
    a_rows = await _signals_for(conn, a)
    assert len(a_rows) == 4

    # Item B is completely unaffected.
    b_rows = await _signals_for(conn, b)
    assert len(b_rows) == 4

    # Per-item bookkeeping: A failed (had at least one synthesized null), B ok.
    assert summary.items_failed == 1
    assert summary.items_ok == 1
    assert summary.status == "partial"


@pytest.mark.asyncio
async def test_resumable_after_partial_prior_run(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    """A prior partial run left two signals in the table; the rerun fills the
    remaining two without duplicating the existing rows.

    Production scenario: previous batch crashed (KeyboardInterrupt, OOM,
    DB lock) after writing only part of an item's signals. Re-running the
    same date must converge to four rows per item with no churn.
    """
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="X", category="arcana")
    for i in range(3):
        peer = await insert_test_item_async(conn, market_hash=f"P{i}", category="arcana")
        await _populate_full_history(conn, peer)
    await _populate_full_history(conn, item_id)

    # Simulate a prior partial run by pre-inserting two signals for item_id
    # with sentinel values we can recognize later.
    sentinel_iso = AS_OF.isoformat()
    await conn.execute(
        "INSERT INTO signals (item_id, computed_for, signal_name, value, metadata_json) "
        "VALUES (?, ?, 'price_zscore', 0.99, '{\"sentinel\": true}')",
        (item_id, sentinel_iso),
    )
    await conn.execute(
        "INSERT INTO signals (item_id, computed_for, signal_name, value, metadata_json) "
        "VALUES (?, ?, 'supply_velocity', 0.88, '{\"sentinel\": true}')",
        (item_id, sentinel_iso),
    )

    await signals_runner.compute_signals_for(AS_OF, settings, run_id="r-resume", backend=fake)

    # Still exactly four rows.
    rows = await _signals_for(conn, item_id)
    assert len(rows) == 4

    # The pre-existing rows are UNTOUCHED (INSERT OR IGNORE preserves them).
    pre_existing = {r["signal_name"]: r["value"] for r in rows}
    assert pre_existing["price_zscore"] == 0.99
    assert pre_existing["supply_velocity"] == 0.88

    # The two missing signals were written by the rerun.
    assert "event_proximity" in pre_existing
    assert "comparables_delta" in pre_existing
