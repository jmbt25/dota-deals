"""Tests for :mod:`dota_deals.scoring.runner`.

These exercise the scoring stage's three production-critical properties:

* For every active item with ≥ 2 non-null signals, a score row exists.
* Re-running the same date is idempotent — no duplicate rows, no churn.
* The ``data_quality_json`` carried into each score row faithfully reflects
  the ingest run's state (success / partial / failed / missing) and whether
  this particular item was in the ingest's coverage gap.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, time

from dota_deals.config import Settings
from dota_deals.models.domain import RunSummary
from dota_deals.scoring.runner import compute_scores_for
from tests.conftest import insert_test_item

AS_OF = date(2026, 5, 12)


def _insert_signal(
    conn: sqlite3.Connection,
    *,
    item_id: int,
    signal_name: str,
    value: float | None,
    metadata: dict[str, object] | None = None,
) -> None:
    conn.execute(
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


def _seed_full_signals(conn: sqlite3.Connection, item_id: int) -> None:
    """Insert four non-null signal rows; values chosen so the score is +0.395."""
    _insert_signal(conn, item_id=item_id, signal_name="price_zscore", value=0.5)
    _insert_signal(conn, item_id=item_id, signal_name="supply_velocity", value=0.4)
    _insert_signal(conn, item_id=item_id, signal_name="event_proximity", value=0.3)
    _insert_signal(conn, item_id=item_id, signal_name="comparables_delta", value=0.2)
    conn.commit()


def _seed_ingest_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    status: str,
    started_at: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO runs (run_id, kind, started_at, status, items_ok, items_quarantined, items_failed)
        VALUES (?, 'ingest', ?, ?, 0, 0, 0)
        """,
        (run_id, started_at.isoformat(), status),
    )
    conn.commit()


def _seed_price_observation_on(conn: sqlite3.Connection, item_id: int, *, on: date) -> None:
    conn.execute(
        "INSERT INTO price_history (item_id, observed_at, lowest_cents) VALUES (?, ?, ?)",
        (item_id, datetime.combine(on, time(12), tzinfo=UTC).isoformat(), 10000),
    )
    conn.commit()


# ----------------------------- happy path --------------------------------------


def test_score_row_written_for_fully_warm_item(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    item_id = insert_test_item(db_conn, market_hash="X")
    _seed_full_signals(db_conn, item_id)
    _seed_price_observation_on(db_conn, item_id, on=AS_OF)
    _seed_ingest_run(
        db_conn,
        run_id="ingest-1",
        status="success",
        started_at=datetime.combine(AS_OF, time(8), tzinfo=UTC),
    )

    summary: RunSummary = compute_scores_for(
        AS_OF, settings, run_id="score-1", parent_run_id="parent-1"
    )

    assert summary.status == "success"
    assert summary.items_ok == 1
    assert summary.items_failed == 0

    row = db_conn.execute(
        "SELECT buy_score, components_json, explanation, data_quality_json "
        "FROM scores WHERE item_id = ? AND computed_for = ?",
        (item_id, AS_OF.isoformat()),
    ).fetchone()
    assert row is not None
    assert abs(row["buy_score"] - 0.395) < 1e-9

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


def test_runs_row_carries_parent_and_kind(settings: Settings, db_conn: sqlite3.Connection) -> None:
    item_id = insert_test_item(db_conn, market_hash="X")
    _seed_full_signals(db_conn, item_id)

    compute_scores_for(AS_OF, settings, run_id="score-tag", parent_run_id="parent-xyz")

    run = db_conn.execute(
        "SELECT kind, parent_run_id, status FROM runs WHERE run_id = ?",
        ("score-tag",),
    ).fetchone()
    assert run["kind"] == "scoring"
    assert run["parent_run_id"] == "parent-xyz"
    assert run["status"] in ("success", "partial")


# ----------------------------- null/edge cases --------------------------------


def test_item_with_three_null_signals_gets_no_score_row(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    item_id = insert_test_item(db_conn, market_hash="LIGHT")
    _insert_signal(db_conn, item_id=item_id, signal_name="price_zscore", value=0.5)
    _insert_signal(db_conn, item_id=item_id, signal_name="supply_velocity", value=None)
    _insert_signal(db_conn, item_id=item_id, signal_name="event_proximity", value=None)
    _insert_signal(db_conn, item_id=item_id, signal_name="comparables_delta", value=None)
    db_conn.commit()

    summary = compute_scores_for(AS_OF, settings, run_id="score-3null")
    assert summary.items_ok == 0
    assert summary.items_failed == 1

    row_count = db_conn.execute(
        "SELECT COUNT(*) FROM scores WHERE item_id = ?", (item_id,)
    ).fetchone()[0]
    assert row_count == 0


def test_item_with_no_signals_at_all_gets_no_score(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """If signals.runner never wrote a row for this item, scorer skips it.

    Matches the "3+ nulls → None" contract: the item didn't have enough
    inputs to produce a score, full stop.
    """
    insert_test_item(db_conn, market_hash="GHOST")

    summary = compute_scores_for(AS_OF, settings, run_id="score-ghost")
    assert summary.items_failed == 1
    assert db_conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0] == 0


# ----------------------------- idempotency ------------------------------------


def test_idempotent_rerun_does_not_double_write(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    item_id = insert_test_item(db_conn, market_hash="X")
    _seed_full_signals(db_conn, item_id)

    compute_scores_for(AS_OF, settings, run_id="score-a")
    compute_scores_for(AS_OF, settings, run_id="score-b")

    count = db_conn.execute(
        "SELECT COUNT(*) FROM scores WHERE item_id = ? AND computed_for = ?",
        (item_id, AS_OF.isoformat()),
    ).fetchone()[0]
    assert count == 1

    run_count = db_conn.execute("SELECT COUNT(*) FROM runs WHERE kind = 'scoring'").fetchone()[0]
    assert run_count == 2


# ----------------------------- data_quality propagation -----------------------


def test_partial_ingest_propagates_to_score_data_quality(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    item_id = insert_test_item(db_conn, market_hash="X")
    _seed_full_signals(db_conn, item_id)
    _seed_price_observation_on(db_conn, item_id, on=AS_OF)
    _seed_ingest_run(
        db_conn,
        run_id="ingest-partial",
        status="partial",
        started_at=datetime.combine(AS_OF, time(8), tzinfo=UTC),
    )

    compute_scores_for(AS_OF, settings, run_id="score-after-partial")

    row = db_conn.execute(
        "SELECT data_quality_json FROM scores WHERE item_id = ?", (item_id,)
    ).fetchone()
    dq = json.loads(row["data_quality_json"])
    assert dq["ingest_status"] == "partial"
    assert dq["item_missing_from_ingest"] is False


def test_item_missing_from_ingest_flagged_in_data_quality(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """Item has signals (maybe from cached/derived data) but no price_history
    on the date — data_quality_json must surface that."""
    item_id = insert_test_item(db_conn, market_hash="STALE")
    _seed_full_signals(db_conn, item_id)
    # Note: no _seed_price_observation_on() for AS_OF.
    _seed_ingest_run(
        db_conn,
        run_id="ingest-partial-2",
        status="partial",
        started_at=datetime.combine(AS_OF, time(8), tzinfo=UTC),
    )

    compute_scores_for(AS_OF, settings, run_id="score-stale")

    row = db_conn.execute(
        "SELECT data_quality_json FROM scores WHERE item_id = ?", (item_id,)
    ).fetchone()
    dq = json.loads(row["data_quality_json"])
    assert dq["item_missing_from_ingest"] is True


def test_no_ingest_run_for_date_yields_missing_status(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    item_id = insert_test_item(db_conn, market_hash="X")
    _seed_full_signals(db_conn, item_id)
    # No ingest run inserted for AS_OF — but signals exist (e.g., from a
    # historical recompute).

    compute_scores_for(AS_OF, settings, run_id="score-noingest")

    row = db_conn.execute(
        "SELECT data_quality_json FROM scores WHERE item_id = ?", (item_id,)
    ).fetchone()
    dq = json.loads(row["data_quality_json"])
    assert dq["ingest_status"] == "missing"


def test_null_signals_listed_in_data_quality(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    item_id = insert_test_item(db_conn, market_hash="X")
    _insert_signal(db_conn, item_id=item_id, signal_name="price_zscore", value=0.5)
    _insert_signal(db_conn, item_id=item_id, signal_name="supply_velocity", value=0.4)
    _insert_signal(db_conn, item_id=item_id, signal_name="event_proximity", value=None)
    _insert_signal(db_conn, item_id=item_id, signal_name="comparables_delta", value=0.2)
    db_conn.commit()

    compute_scores_for(AS_OF, settings, run_id="score-onenull")

    row = db_conn.execute(
        "SELECT data_quality_json FROM scores WHERE item_id = ?", (item_id,)
    ).fetchone()
    dq = json.loads(row["data_quality_json"])
    assert dq["null_signals"] == ["event_proximity"]
