"""Tests for :mod:`dota_deals.publish.builder`.

The warmup / empty-state contracts get explicit assertions — the frontend
depends on them for its empty-state UI. If those break silently the bug
shows up only when a user lands on the page during cold start.

Phase 9c-iii: builders are async over D1. Tests use ``db_conn``
and ``await`` every builder call.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, time, timedelta

import pytest

from dota_deals.publish.builder import (
    build_health,
    build_historical_report,
    build_item_detail,
    build_latest_report,
)
from dota_deals.publish.models import LatestReport
from dota_deals.storage.db import D1Connection
from tests._d1_fake import D1FakeClient
from tests.conftest import insert_test_item

AS_OF = date(2026, 5, 13)


# ----------------------------- helpers -----------------------------------------


async def _insert_score(
    conn: D1Connection,
    *,
    item_id: int,
    score: float,
    on: date = AS_OF,
    components: dict[str, float | None] | None = None,
    null_signals: list[str] | None = None,
) -> None:
    components = components or {
        "price_zscore": 0.5,
        "supply_velocity": 0.4,
        "event_proximity": 0.3,
        "comparables_delta": 0.2,
    }
    null_signals = null_signals or []
    await conn.execute(
        """
        INSERT INTO scores
            (item_id, computed_for, buy_score, components_json,
             explanation, data_quality_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            on.isoformat(),
            score,
            json.dumps(components, sort_keys=True),
            "Priced below recent baseline",
            json.dumps({"null_signals": null_signals}),
        ),
    )


async def _insert_latest_observation(
    conn: D1Connection, item_id: int, *, lowest_cents: int
) -> None:
    await conn.execute(
        """
        INSERT INTO latest_observation
            (item_id, observed_at, lowest_cents, listings_count)
        VALUES (?, ?, ?, ?)
        """,
        (
            item_id,
            datetime.combine(AS_OF, time(8), tzinfo=UTC).isoformat(),
            lowest_cents,
            42,
        ),
    )


async def _insert_ingest_run(
    conn: D1Connection,
    *,
    run_id: str,
    status: str,
    on: date = AS_OF,
) -> None:
    await conn.execute(
        """
        INSERT INTO runs (run_id, kind, started_at, finished_at, status,
                          items_ok, items_quarantined, items_failed)
        VALUES (?, 'ingest', ?, ?, ?, 0, 0, 0)
        """,
        (
            run_id,
            datetime.combine(on, time(8), tzinfo=UTC).isoformat(),
            datetime.combine(on, time(9), tzinfo=UTC).isoformat(),
            status,
        ),
    )


async def _insert_price_history(
    conn: D1Connection,
    item_id: int,
    *,
    on: date,
    cents: int = 10000,
) -> None:
    await conn.execute(
        "INSERT INTO price_history (item_id, observed_at, lowest_cents) VALUES (?, ?, ?)",
        (item_id, datetime.combine(on, time(12), tzinfo=UTC).isoformat(), cents),
    )


# ----------------------------- build_latest_report -----------------------------


@pytest.mark.asyncio
async def test_latest_report_warmup_when_no_scores(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    """Empty DB → warmup envelope. The frontend depends on this contract.

    Status MUST be 'warmup', scores MUST be empty, report_date MUST be None.
    """
    conn, _ = db_conn
    report = await build_latest_report(conn)
    assert isinstance(report, LatestReport)
    assert report.status == "warmup"
    assert report.scores == []
    assert report.report_date is None
    assert report.data_quality.ingest_status == "missing"


@pytest.mark.asyncio
async def test_latest_report_happy_path(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, _ = db_conn
    item_id = await insert_test_item(conn, market_hash="X", category="arcana")
    await _insert_score(conn, item_id=item_id, score=0.62)
    await _insert_latest_observation(conn, item_id, lowest_cents=3450)
    await _insert_ingest_run(conn, run_id="ingest-1", status="success")

    report = await build_latest_report(conn)
    assert report.status == "operational"
    assert report.report_date == AS_OF
    assert len(report.scores) == 1
    s = report.scores[0]
    assert s.item_id == item_id
    assert s.market_hash_name == "X"
    assert s.current_price == "34.50"
    assert s.buy_score == pytest.approx(0.62)
    assert s.components.price_zscore == pytest.approx(0.5)
    assert s.components.event_proximity == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_latest_report_degraded_when_ingest_partial(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, _ = db_conn
    item_id = await insert_test_item(conn, market_hash="X", category="arcana")
    await _insert_score(conn, item_id=item_id, score=0.5)
    await _insert_ingest_run(conn, run_id="ingest-partial", status="partial")

    report = await build_latest_report(conn)
    assert report.status == "degraded"


@pytest.mark.asyncio
async def test_latest_report_top_n_truncates_and_orders(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    """Top-N: highest score first, item_id tie-break, count <= top_n."""
    conn, _ = db_conn
    for idx, value in enumerate([0.1, 0.5, 0.3, 0.4, 0.2], start=1):
        item_id = await insert_test_item(conn, market_hash=f"item-{idx}", category="arcana")
        await _insert_score(conn, item_id=item_id, score=value)
    await _insert_ingest_run(conn, run_id="ingest-1", status="success")

    report = await build_latest_report(conn, top_n=3)
    assert [s.buy_score for s in report.scores] == [0.5, 0.4, 0.3]


@pytest.mark.asyncio
async def test_latest_report_serializes_null_current_price(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    """Item without latest_observation → current_price stays None on the wire."""
    conn, _ = db_conn
    item_id = await insert_test_item(conn, market_hash="X", category="arcana")
    await _insert_score(conn, item_id=item_id, score=0.5)
    # No latest_observation insert.
    await _insert_ingest_run(conn, run_id="ingest-1", status="success")

    report = await build_latest_report(conn)
    assert report.scores[0].current_price is None


# ----------------------------- build_historical_report -------------------------


@pytest.mark.asyncio
async def test_historical_report_returns_none_when_no_scores(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, _ = db_conn
    assert await build_historical_report(conn, AS_OF) is None


@pytest.mark.asyncio
async def test_historical_report_happy_path(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, _ = db_conn
    item_id = await insert_test_item(conn, market_hash="X", category="arcana")
    await _insert_score(conn, item_id=item_id, score=0.5)

    report = await build_historical_report(conn, AS_OF)
    assert report is not None
    assert report.report_date == AS_OF
    assert len(report.scores) == 1


@pytest.mark.asyncio
async def test_historical_report_different_date_returns_none(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    """Scores for AS_OF don't satisfy a query for AS_OF - 1."""
    conn, _ = db_conn
    item_id = await insert_test_item(conn, market_hash="X", category="arcana")
    await _insert_score(conn, item_id=item_id, score=0.5, on=AS_OF)
    assert await build_historical_report(conn, AS_OF - timedelta(days=1)) is None


# ----------------------------- build_health -----------------------------------


@pytest.mark.asyncio
async def test_health_warmup_when_no_scores(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    """Empty DB → status warmup, days_remaining defaults to threshold."""
    conn, _ = db_conn
    health = await build_health(conn)
    assert health.status == "warmup"
    assert health.last_run is None
    assert health.data_coverage.items_tracked == 0
    assert health.data_coverage.items_with_signals == 0
    assert health.data_coverage.days_of_history == 0
    assert health.warmup_estimate.days_remaining == 30


@pytest.mark.asyncio
async def test_health_warmup_days_remaining_decreases_with_history(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    """With 10 days of history, days_remaining = 30 - 10 = 20."""
    conn, _ = db_conn
    today = datetime.now(UTC).date()
    item_id = await insert_test_item(conn, market_hash="X", category="arcana")
    # First observation 9 days ago → 10 days of history span.
    await _insert_price_history(conn, item_id, on=today - timedelta(days=9))

    health = await build_health(conn)
    assert health.data_coverage.days_of_history == 10
    assert health.warmup_estimate.days_remaining == 20


@pytest.mark.asyncio
async def test_health_past_warmup_yields_null_remaining(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, _ = db_conn
    today = datetime.now(UTC).date()
    item_id = await insert_test_item(conn, market_hash="X", category="arcana")
    # 60 days ago → > 30 days of history.
    await _insert_price_history(conn, item_id, on=today - timedelta(days=60))

    health = await build_health(conn)
    assert health.warmup_estimate.days_remaining is None


@pytest.mark.asyncio
async def test_health_degraded_when_today_ingest_partial(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, _ = db_conn
    today = datetime.now(UTC).date()
    item_id = await insert_test_item(conn, market_hash="X", category="arcana")
    await _insert_score(conn, item_id=item_id, score=0.5)
    await _insert_ingest_run(conn, run_id="ingest-partial", status="partial", on=today)

    health = await build_health(conn)
    assert health.status == "degraded"


@pytest.mark.asyncio
async def test_health_last_run_is_most_recent_successful(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, _ = db_conn
    today = datetime.now(UTC).date()
    item_id = await insert_test_item(conn, market_hash="X", category="arcana")
    await _insert_score(conn, item_id=item_id, score=0.5)
    await _insert_ingest_run(conn, run_id="ingest-success", status="success", on=today)

    health = await build_health(conn)
    assert health.last_run is not None
    assert health.last_run.run_id == "ingest-success"
    assert health.last_run.status == "success"


# ----------------------------- build_item_detail ------------------------------


@pytest.mark.asyncio
async def test_item_detail_returns_none_for_unknown(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, _ = db_conn
    assert await build_item_detail(conn, item_id=9999) is None


@pytest.mark.asyncio
async def test_item_detail_happy_path(
    db_conn: tuple[D1Connection, D1FakeClient],
) -> None:
    conn, _ = db_conn
    item_id = await insert_test_item(
        conn, market_hash="X", name="Inscribed Manifold Paradox", category="arcana"
    )
    today = datetime.now(UTC).date()
    for offset in range(5):
        await _insert_price_history(
            conn, item_id, on=today - timedelta(days=offset), cents=10000 + offset * 100
        )

    detail = await build_item_detail(conn, item_id)
    assert detail is not None
    assert detail.item_id == item_id
    assert detail.name == "Inscribed Manifold Paradox"
    assert detail.category == "arcana"
    assert len(detail.daily_prices) == 5
    # daily_prices is sorted oldest-first by daily_prices repo helper
    assert detail.daily_prices[0].lowest_price == "104.00"
    assert detail.daily_prices[-1].lowest_price == "100.00"
    # All four signal series present (with zero points each, since no
    # signals were inserted).
    series_names = [s.signal_name for s in detail.signals]
    assert series_names == [
        "price_zscore",
        "supply_velocity",
        "event_proximity",
        "comparables_delta",
    ]
