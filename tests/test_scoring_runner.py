"""Tests for :mod:`dota_deals.scoring.runner`.

These exercise the scoring stage's three production-critical properties:

* For every active item with ≥ 2 non-null signals, a score row exists.
* Re-running the same date is idempotent — no duplicate rows, no churn.
* The ``data_quality_json`` carried into each score row faithfully reflects
  the ingest run's state (success / partial / failed / missing) and whether
  this particular item was in the ingest's coverage gap.

Phase 9c-iii: storage moves to async D1. Tests use ``db_conn_async`` and
pass ``backend=fake`` to :func:`compute_scores_for`.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time
from typing import Any

import pytest

from dota_deals.config import Settings
from dota_deals.models.domain import RunSummary
from dota_deals.scoring.runner import compute_scores_for
from dota_deals.storage.db_async import D1Connection
from tests._d1_fake import D1FakeClient
from tests.conftest import insert_test_item_async

AS_OF = date(2026, 5, 12)


async def _insert_signal(
    conn: D1Connection,
    *,
    item_id: int,
    signal_name: str,
    value: float | None,
    metadata: dict[str, object] | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO signals (item_id, computed_for, signal_name, value, metadata_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            item_id,
            AS_OF.isoformat(),
            signal_name,
            value,
            json.dumps(metadata) if metadata else None,
        ),
    )


async def _seed_full_signals(conn: D1Connection, item_id: int) -> None:
    """Insert four non-null signal rows; values chosen so the score is +0.395."""
    await _insert_signal(conn, item_id=item_id, signal_name="price_zscore", value=0.5)
    await _insert_signal(conn, item_id=item_id, signal_name="supply_velocity", value=0.4)
    await _insert_signal(conn, item_id=item_id, signal_name="event_proximity", value=0.3)
    await _insert_signal(conn, item_id=item_id, signal_name="comparables_delta", value=0.2)


async def _seed_ingest_run(
    conn: D1Connection,
    *,
    run_id: str,
    status: str,
    started_at: datetime,
) -> None:
    await conn.execute(
        """
        INSERT INTO runs (run_id, kind, started_at, status,
                          items_ok, items_quarantined, items_failed)
        VALUES (?, 'ingest', ?, ?, 0, 0, 0)
        """,
        (run_id, started_at.isoformat(), status),
    )


async def _seed_price_observation_on(conn: D1Connection, item_id: int, *, on: date) -> None:
    await conn.execute(
        "INSERT INTO price_history (item_id, observed_at, lowest_cents) VALUES (?, ?, ?)",
        (item_id, datetime.combine(on, time(12), tzinfo=UTC).isoformat(), 10000),
    )


async def _select(
    conn: D1Connection, sql: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    result = await conn.query(sql, params)
    return result.results


async def _count(conn: D1Connection, sql: str, params: tuple[Any, ...] = ()) -> int:
    rows = await _select(conn, sql, params)
    return int(rows[0]["n"])


# ----------------------------- happy path --------------------------------------


@pytest.mark.asyncio
async def test_score_row_written_for_fully_warm_item(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="X")
    await _seed_full_signals(conn, item_id)
    await _seed_price_observation_on(conn, item_id, on=AS_OF)
    await _seed_ingest_run(
        conn,
        run_id="ingest-1",
        status="success",
        started_at=datetime.combine(AS_OF, time(8), tzinfo=UTC),
    )

    summary: RunSummary = await compute_scores_for(
        AS_OF, settings, run_id="score-1", parent_run_id="parent-1", backend=fake
    )

    assert summary.status == "success"
    assert summary.items_ok == 1
    assert summary.items_failed == 0

    rows = await _select(
        conn,
        "SELECT buy_score, components_json, explanation, data_quality_json "
        "FROM scores WHERE item_id = ? AND computed_for = ?",
        (item_id, AS_OF.isoformat()),
    )
    assert rows
    row = rows[0]
    assert abs(float(row["buy_score"]) - 0.395) < 1e-9

    components = json.loads(row["components_json"])
    assert components == {
        "price_zscore": 0.5,
        "supply_velocity": 0.4,
        "event_proximity": 0.3,
        "comparables_delta": 0.2,
    }
    assert row["explanation"]  # non-empty

    dq = json.loads(row["data_quality_json"])
    assert dq["ingest_status"] == "success"
    assert dq["item_missing_from_ingest"] is False
    assert dq["null_signals"] == []


@pytest.mark.asyncio
async def test_runs_row_carries_parent_and_kind(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="X")
    await _seed_full_signals(conn, item_id)

    await compute_scores_for(
        AS_OF, settings, run_id="score-tag", parent_run_id="parent-xyz", backend=fake
    )

    rows = await _select(
        conn,
        "SELECT kind, parent_run_id, status FROM runs WHERE run_id = ?",
        ("score-tag",),
    )
    assert rows[0]["kind"] == "scoring"
    assert rows[0]["parent_run_id"] == "parent-xyz"
    assert rows[0]["status"] in ("success", "partial")


# ----------------------------- null/edge cases --------------------------------


@pytest.mark.asyncio
async def test_item_with_three_null_signals_gets_no_score_row(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="LIGHT")
    await _insert_signal(conn, item_id=item_id, signal_name="price_zscore", value=0.5)
    await _insert_signal(conn, item_id=item_id, signal_name="supply_velocity", value=None)
    await _insert_signal(conn, item_id=item_id, signal_name="event_proximity", value=None)
    await _insert_signal(conn, item_id=item_id, signal_name="comparables_delta", value=None)

    summary = await compute_scores_for(AS_OF, settings, run_id="score-3null", backend=fake)
    assert summary.items_ok == 0
    assert summary.items_failed == 1

    n = await _count(conn, "SELECT COUNT(*) AS n FROM scores WHERE item_id = ?", (item_id,))
    assert n == 0


@pytest.mark.asyncio
async def test_item_with_no_signals_at_all_gets_no_score(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    """If signals.runner never wrote a row for this item, scorer skips it.

    Matches the "3+ nulls → None" contract: the item didn't have enough
    inputs to produce a score, full stop.
    """
    conn, fake = db_conn_async
    await insert_test_item_async(conn, market_hash="GHOST")

    summary = await compute_scores_for(AS_OF, settings, run_id="score-ghost", backend=fake)
    assert summary.items_failed == 1
    assert await _count(conn, "SELECT COUNT(*) AS n FROM scores") == 0


# ----------------------------- idempotency ------------------------------------


@pytest.mark.asyncio
async def test_idempotent_rerun_does_not_double_write(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="X")
    await _seed_full_signals(conn, item_id)

    await compute_scores_for(AS_OF, settings, run_id="score-a", backend=fake)
    await compute_scores_for(AS_OF, settings, run_id="score-b", backend=fake)

    n = await _count(
        conn,
        "SELECT COUNT(*) AS n FROM scores WHERE item_id = ? AND computed_for = ?",
        (item_id, AS_OF.isoformat()),
    )
    assert n == 1

    run_count = await _count(conn, "SELECT COUNT(*) AS n FROM runs WHERE kind = 'scoring'")
    assert run_count == 2


# ----------------------------- data_quality propagation -----------------------


@pytest.mark.asyncio
async def test_partial_ingest_propagates_to_score_data_quality(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="X")
    await _seed_full_signals(conn, item_id)
    await _seed_price_observation_on(conn, item_id, on=AS_OF)
    await _seed_ingest_run(
        conn,
        run_id="ingest-partial",
        status="partial",
        started_at=datetime.combine(AS_OF, time(8), tzinfo=UTC),
    )

    await compute_scores_for(AS_OF, settings, run_id="score-after-partial", backend=fake)

    rows = await _select(conn, "SELECT data_quality_json FROM scores WHERE item_id = ?", (item_id,))
    dq = json.loads(rows[0]["data_quality_json"])
    assert dq["ingest_status"] == "partial"
    assert dq["item_missing_from_ingest"] is False


@pytest.mark.asyncio
async def test_item_missing_from_ingest_flagged_in_data_quality(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    """Item has signals (maybe from cached/derived data) but no price_history
    on the date — data_quality_json must surface that."""
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="STALE")
    await _seed_full_signals(conn, item_id)
    # Note: no _seed_price_observation_on for AS_OF.
    await _seed_ingest_run(
        conn,
        run_id="ingest-partial-2",
        status="partial",
        started_at=datetime.combine(AS_OF, time(8), tzinfo=UTC),
    )

    await compute_scores_for(AS_OF, settings, run_id="score-stale", backend=fake)

    rows = await _select(conn, "SELECT data_quality_json FROM scores WHERE item_id = ?", (item_id,))
    dq = json.loads(rows[0]["data_quality_json"])
    assert dq["item_missing_from_ingest"] is True


@pytest.mark.asyncio
async def test_no_ingest_run_for_date_yields_missing_status(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="X")
    await _seed_full_signals(conn, item_id)
    # No ingest run inserted for AS_OF — but signals exist (e.g., from a
    # historical recompute).

    await compute_scores_for(AS_OF, settings, run_id="score-noingest", backend=fake)

    rows = await _select(conn, "SELECT data_quality_json FROM scores WHERE item_id = ?", (item_id,))
    dq = json.loads(rows[0]["data_quality_json"])
    assert dq["ingest_status"] == "missing"


@pytest.mark.asyncio
async def test_null_signals_listed_in_data_quality(
    settings: Settings,
    db_conn_async: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="X")
    await _insert_signal(conn, item_id=item_id, signal_name="price_zscore", value=0.5)
    await _insert_signal(conn, item_id=item_id, signal_name="supply_velocity", value=0.4)
    await _insert_signal(conn, item_id=item_id, signal_name="event_proximity", value=None)
    await _insert_signal(conn, item_id=item_id, signal_name="comparables_delta", value=0.2)

    await compute_scores_for(AS_OF, settings, run_id="score-onenull", backend=fake)

    rows = await _select(conn, "SELECT data_quality_json FROM scores WHERE item_id = ?", (item_id,))
    dq = json.loads(rows[0]["data_quality_json"])
    assert dq["null_signals"] == ["event_proximity"]
