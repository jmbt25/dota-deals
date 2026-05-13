"""Ingestion end-to-end tests.

Covers the seven cases enumerated in the Phase 3 brief:

1. Happy path with respx-mocked Steam response.
2. Timeout retried, succeeds on 3rd attempt.
3. 429 triggers extended backoff (verified by call count + sleep values).
4. 4xx not retried, logged, run continues.
5. Validation failure routes to quarantine, not price_history.
6. Idempotency: running ingest twice for same (item, observed_at) doesn't double-insert.
7. Run summary in runs table reflects ok / quarantined / failed counts.

Steam-client-level retry behaviors (cases 2, 3, 4 in part) are tested against
the client directly with a recording sleep so retries don't burn wall-clock
time. Runner-level concerns (cases 1, 4 partial, 5, 6, 7) drive the full
:func:`run_ingestion` orchestration with respx-mocked HTTP and the real
SQLite repositories.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import httpx
import pytest
import respx

from dota_deals.config import Settings
from dota_deals.ingest.runner import run_ingestion, slot_for
from dota_deals.ingest.steam import (
    IngestError,
    IngestValidationError,
    SteamMarketClient,
)
from tests.conftest import insert_test_item

PRICE_OVERVIEW = "https://steamcommunity.com/market/priceoverview/"
LISTINGS_PATTERN = re.compile(r"https://steamcommunity\.com/market/listings/570/[^/]+/render")

# Fixed wall-clock for tests. slot_for(FIXED_NOW, 8) → 2026-01-15 08:00 UTC.
FIXED_NOW = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)
EXPECTED_OBSERVED_AT = datetime(2026, 1, 15, 8, 0, tzinfo=UTC)


# ----------------------------- helpers -----------------------------------------


def _ok_priceoverview(
    lowest: str = "$3.45", median: str | None = "$3.49", volume: str | None = "12"
) -> httpx.Response:
    body: dict[str, object] = {"success": True, "lowest_price": lowest}
    if median is not None:
        body["median_price"] = median
    if volume is not None:
        body["volume"] = volume
    return httpx.Response(200, json=body)


def _ok_listings(count: int = 27) -> httpx.Response:
    return httpx.Response(
        200,
        json={"success": True, "start": 0, "pagesize": 1, "total_count": count},
    )


def _make_recorded_sleep() -> tuple[Callable[[float], Awaitable[None]], list[float]]:
    """Return ``(fake_sleep_fn, calls_list)``.

    ``fake_sleep_fn`` records the requested duration and then yields once via
    ``asyncio.sleep(0)`` so other coroutines (notably the global-cooldown task)
    get a chance to run, but no real waiting happens.
    """
    calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)
        await asyncio.sleep(0)

    return fake_sleep, calls


# ----------------------------- slot truncation ---------------------------------


def test_slot_for_truncates_to_polling_slot() -> None:
    assert slot_for(FIXED_NOW, 8) == EXPECTED_OBSERVED_AT
    assert slot_for(datetime(2026, 1, 15, 0, 0, tzinfo=UTC), 8) == datetime(
        2026, 1, 15, 0, 0, tzinfo=UTC
    )
    assert slot_for(datetime(2026, 1, 15, 23, 59, tzinfo=UTC), 8) == datetime(
        2026, 1, 15, 16, 0, tzinfo=UTC
    )


def test_slot_for_rejects_naive_datetime() -> None:
    naive = datetime(2026, 1, 15, 10, 30)
    with pytest.raises(ValueError):
        slot_for(naive, 8)


# ----------------------------- steam client unit tests -------------------------


@pytest.mark.asyncio
async def test_client_timeout_retried_then_succeeds(settings: Settings) -> None:
    """Case 2: timeout retried, succeeds on 3rd attempt."""
    sleep, sleep_calls = _make_recorded_sleep()

    with respx.mock(assert_all_called=False) as router:
        route = router.get(PRICE_OVERVIEW).mock(
            side_effect=[
                httpx.TimeoutException("first timeout"),
                httpx.TimeoutException("second timeout"),
                _ok_priceoverview(),
            ]
        )
        async with SteamMarketClient(settings, sleep=sleep) as client:
            overview = await client.fetch_price_overview("Item")

    assert overview.lowest_cents == 345
    assert route.call_count == 3
    assert len(sleep_calls) == 2  # two retries between three attempts


@pytest.mark.asyncio
async def test_client_429_extended_backoff(settings: Settings) -> None:
    """Case 3: 429 triggers extended backoff. Verify via call count + sleep values.

    The first 429 backoff is 30s; the second 60s; the third 120s. Four attempts
    total before giving up (``_MAX_429_ATTEMPTS``).
    """
    sleep, sleep_calls = _make_recorded_sleep()

    with respx.mock(assert_all_called=False) as router:
        route = router.get(PRICE_OVERVIEW).mock(return_value=httpx.Response(429))
        async with SteamMarketClient(settings, sleep=sleep) as client:
            with pytest.raises(IngestError) as exc_info:
                await client.fetch_price_overview("Item")

    assert exc_info.value.status_code == 429
    assert route.call_count == 4  # 4 attempts before giving up
    assert 30.0 in sleep_calls
    assert 60.0 in sleep_calls
    assert 120.0 in sleep_calls


@pytest.mark.asyncio
async def test_client_4xx_not_retried(settings: Settings) -> None:
    """Case 4 (client level): 4xx is a one-shot failure."""
    sleep, sleep_calls = _make_recorded_sleep()

    with respx.mock(assert_all_called=False) as router:
        route = router.get(PRICE_OVERVIEW).mock(return_value=httpx.Response(404))
        async with SteamMarketClient(settings, sleep=sleep) as client:
            with pytest.raises(IngestError) as exc_info:
                await client.fetch_price_overview("Item")

    assert exc_info.value.status_code == 404
    assert route.call_count == 1
    assert sleep_calls == []  # no retry backoff happened


@pytest.mark.asyncio
async def test_client_validation_carries_raw_payload(settings: Settings) -> None:
    """Validation errors surface as IngestValidationError with the raw body."""
    sleep, _ = _make_recorded_sleep()
    bad = {
        "success": True,
        "lowest_price": "totally_not_a_price",
        "median_price": "$3.49",
        "volume": "12",
    }

    with respx.mock(assert_all_called=False) as router:
        router.get(PRICE_OVERVIEW).mock(return_value=httpx.Response(200, json=bad))
        async with SteamMarketClient(settings, sleep=sleep) as client:
            with pytest.raises(IngestValidationError) as exc_info:
                await client.fetch_price_overview("Item")

    assert "totally_not_a_price" in exc_info.value.raw_payload
    assert exc_info.value.source == "steam_price_overview"


@pytest.mark.asyncio
async def test_client_5xx_retried(settings: Settings) -> None:
    """5xx is retried with the same policy as transport errors."""
    sleep, sleep_calls = _make_recorded_sleep()

    with respx.mock(assert_all_called=False) as router:
        route = router.get(PRICE_OVERVIEW).mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(502),
                _ok_priceoverview(),
            ]
        )
        async with SteamMarketClient(settings, sleep=sleep) as client:
            overview = await client.fetch_price_overview("Item")

    assert overview.lowest_cents == 345
    assert route.call_count == 3
    assert len(sleep_calls) == 2


# ----------------------------- runner integration tests ------------------------


@pytest.mark.asyncio
async def test_runner_happy_path(settings: Settings, db_conn: sqlite3.Connection) -> None:
    """Case 1: happy path persists rows in price_history + listing_history."""
    item_id = insert_test_item(
        db_conn, market_hash="Inscribed Manifold Paradox", hero="Phantom Assassin"
    )

    with respx.mock(assert_all_called=False) as router:
        router.get(PRICE_OVERVIEW).mock(return_value=_ok_priceoverview())
        router.get(LISTINGS_PATTERN).mock(return_value=_ok_listings(27))

        summary = await run_ingestion(
            items=["Inscribed Manifold Paradox"],
            settings=settings,
            run_id="run-1",
            parent_run_id="parent-1",
            now=FIXED_NOW,
        )

    assert summary.status == "success"
    assert summary.items_ok == 1
    assert summary.items_quarantined == 0
    assert summary.items_failed == 0

    price_rows = db_conn.execute(
        "SELECT * FROM price_history ORDER BY item_id, observed_at"
    ).fetchall()
    assert len(price_rows) == 1
    assert price_rows[0]["item_id"] == item_id
    assert price_rows[0]["observed_at"] == EXPECTED_OBSERVED_AT.isoformat()
    assert price_rows[0]["lowest_cents"] == 345
    assert price_rows[0]["median_cents"] == 349
    assert price_rows[0]["volume_24h"] == 12

    listing_rows = db_conn.execute("SELECT * FROM listing_history").fetchall()
    assert len(listing_rows) == 1
    assert listing_rows[0]["listings_count"] == 27

    latest = db_conn.execute("SELECT * FROM latest_observation").fetchone()
    assert latest["lowest_cents"] == 345
    assert latest["listings_count"] == 27

    run = db_conn.execute("SELECT * FROM runs WHERE run_id = ?", ("run-1",)).fetchone()
    assert run["status"] == "success"
    assert run["parent_run_id"] == "parent-1"
    assert run["items_ok"] == 1


@pytest.mark.asyncio
async def test_runner_4xx_run_continues(settings: Settings, db_conn: sqlite3.Connection) -> None:
    """Case 4 (runner level): a 4xx on one item doesn't abort the run."""
    insert_test_item(db_conn, market_hash="GOOD")
    insert_test_item(db_conn, market_hash="BAD")

    def route_overview(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("market_hash_name", "")
        if name == "BAD":
            return httpx.Response(404)
        return _ok_priceoverview()

    with respx.mock(assert_all_called=False) as router:
        router.get(PRICE_OVERVIEW).mock(side_effect=route_overview)
        router.get(LISTINGS_PATTERN).mock(return_value=_ok_listings(27))

        summary = await run_ingestion(
            items=["GOOD", "BAD"],
            settings=settings,
            run_id="run-2",
            now=FIXED_NOW,
        )

    assert summary.items_ok == 1
    assert summary.items_failed == 1
    assert summary.status == "partial"
    # The good item still persisted.
    assert db_conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1


@pytest.mark.asyncio
async def test_runner_validation_routes_to_quarantine(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """Case 5: bad payload lands in quarantine, not price_history."""
    insert_test_item(db_conn, market_hash="X")

    bad = {
        "success": True,
        "lowest_price": "totally_not_a_price",
        "median_price": "$3.49",
        "volume": "12",
    }

    with respx.mock(assert_all_called=False) as router:
        router.get(PRICE_OVERVIEW).mock(return_value=httpx.Response(200, json=bad))
        router.get(LISTINGS_PATTERN).mock(return_value=_ok_listings(27))

        summary = await run_ingestion(items=["X"], settings=settings, run_id="run-3", now=FIXED_NOW)

    assert summary.items_quarantined == 1
    assert summary.items_ok == 0

    assert db_conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 0
    q_rows = db_conn.execute("SELECT * FROM quarantine").fetchall()
    assert len(q_rows) == 1
    assert "totally_not_a_price" in q_rows[0]["raw_payload"]
    assert q_rows[0]["source"] == "steam_price_overview"
    assert q_rows[0]["item_hash"] == "X"
    assert q_rows[0]["run_id"] == "run-3"


@pytest.mark.asyncio
async def test_runner_idempotent_double_run(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """Case 6: two runs at the same polling slot produce one row each."""
    insert_test_item(db_conn, market_hash="X")

    with respx.mock(assert_all_called=False) as router:
        router.get(PRICE_OVERVIEW).mock(return_value=_ok_priceoverview())
        router.get(LISTINGS_PATTERN).mock(return_value=_ok_listings(27))

        s1 = await run_ingestion(items=["X"], settings=settings, run_id="r-a", now=FIXED_NOW)
        # Different wall-clock minute, same polling slot.
        same_slot_later = FIXED_NOW.replace(minute=59)
        s2 = await run_ingestion(items=["X"], settings=settings, run_id="r-b", now=same_slot_later)

    assert s1.items_ok == 1
    assert s2.items_ok == 1

    assert db_conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM listing_history").fetchone()[0] == 1
    assert db_conn.execute("SELECT COUNT(*) FROM latest_observation").fetchone()[0] == 1
    # Both runs recorded.
    assert db_conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 2


@pytest.mark.asyncio
async def test_runner_summary_reflects_mixed_outcomes(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """Case 7: runs table records ok / quarantined / failed counts faithfully."""
    insert_test_item(db_conn, market_hash="OK_ITEM")
    insert_test_item(db_conn, market_hash="BAD_ITEM")
    # UNKNOWN_ITEM intentionally not in the items table.

    def route_overview(request: httpx.Request) -> httpx.Response:
        name = request.url.params.get("market_hash_name", "")
        if name == "OK_ITEM":
            return _ok_priceoverview()
        if name == "BAD_ITEM":
            return httpx.Response(200, json={"success": True, "lowest_price": "garbage"})
        return httpx.Response(404)

    with respx.mock(assert_all_called=False) as router:
        router.get(PRICE_OVERVIEW).mock(side_effect=route_overview)
        router.get(LISTINGS_PATTERN).mock(return_value=_ok_listings(27))

        summary = await run_ingestion(
            items=["OK_ITEM", "BAD_ITEM", "UNKNOWN_ITEM"],
            settings=settings,
            run_id="run-mixed",
            now=FIXED_NOW,
        )

    assert summary.items_ok == 1
    assert summary.items_quarantined == 1
    assert summary.items_failed == 1
    assert summary.status == "partial"

    run = db_conn.execute("SELECT * FROM runs WHERE run_id = ?", ("run-mixed",)).fetchone()
    assert run["status"] == "partial"
    assert run["items_ok"] == 1
    assert run["items_quarantined"] == 1
    assert run["items_failed"] == 1
    assert run["finished_at"] is not None


# ----------------------------- 3-strike deactivation ---------------------------


@pytest.mark.asyncio
async def test_deactivation_fires_at_exactly_three_strikes(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """Phase 4a: 3 consecutive 4xx flips items.active = 0. Not at 2, not at 4."""
    insert_test_item(db_conn, market_hash="X")

    def _state() -> tuple[int, int]:
        row = db_conn.execute(
            "SELECT active, consecutive_ingest_4xx FROM items WHERE market_hash = ?",
            ("X",),
        ).fetchone()
        return int(row["active"]), int(row["consecutive_ingest_4xx"])

    with respx.mock(assert_all_called=False) as router:
        router.get(PRICE_OVERVIEW).mock(return_value=httpx.Response(404))

        # Strike 1
        await run_ingestion(items=["X"], settings=settings, run_id="r1", now=FIXED_NOW)
        assert _state() == (1, 1), "after strike 1: still active, count=1"

        # Strike 2
        await run_ingestion(items=["X"], settings=settings, run_id="r2", now=FIXED_NOW)
        assert _state() == (1, 2), "after strike 2: still active, count=2"

        # Strike 3 — deactivation triggers here, not earlier.
        await run_ingestion(items=["X"], settings=settings, run_id="r3", now=FIXED_NOW)
        assert _state() == (0, 3), "after strike 3: deactivated, count=3"

        # Strike 4 — counter keeps climbing but active stays 0 (no re-deactivation).
        await run_ingestion(items=["X"], settings=settings, run_id="r4", now=FIXED_NOW)
        assert _state() == (0, 4), "after strike 4: still deactivated, count=4"


@pytest.mark.asyncio
async def test_strike_counter_reset_on_success(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """A successful ingest run clears any accumulated strikes."""
    insert_test_item(db_conn, market_hash="X")
    # Pre-seed two strikes so a third would deactivate.
    db_conn.execute("UPDATE items SET consecutive_ingest_4xx = 2 WHERE market_hash = ?", ("X",))
    db_conn.commit()

    with respx.mock(assert_all_called=False) as router:
        router.get(PRICE_OVERVIEW).mock(return_value=_ok_priceoverview())
        router.get(LISTINGS_PATTERN).mock(return_value=_ok_listings(27))

        await run_ingestion(items=["X"], settings=settings, run_id="r-ok", now=FIXED_NOW)

    row = db_conn.execute(
        "SELECT active, consecutive_ingest_4xx FROM items WHERE market_hash = ?", ("X",)
    ).fetchone()
    assert row["active"] == 1
    assert row["consecutive_ingest_4xx"] == 0


def test_non_4xx_failures_do_not_count_as_strikes(
    db_conn: sqlite3.Connection,
) -> None:
    """Only true 4xx (not 429, not 5xx, not timeouts) increment the strike counter.

    Exercises :func:`_record_failure_strike` directly because the alternative
    paths through ``run_ingestion`` (timeout, 5xx, 429) all involve real
    retry waits at runner level. The strike-policy contract is a small,
    standalone branch worth testing in isolation.
    """
    from dota_deals.ingest.runner import _record_failure_strike
    from dota_deals.logging import get_logger

    insert_test_item(db_conn, market_hash="X")
    row = db_conn.execute("SELECT item_id FROM items WHERE market_hash = ?", ("X",)).fetchone()
    item_id = int(row["item_id"])
    log = get_logger("test").bind()

    for status_code in (429, None, 503, 500, 502):
        _record_failure_strike(db_conn, item_id, True, status_code, log)

    assert (
        db_conn.execute(
            "SELECT consecutive_ingest_4xx FROM items WHERE market_hash = ?", ("X",)
        ).fetchone()["consecutive_ingest_4xx"]
        == 0
    ), "429/timeout/5xx must not increment strikes"

    # Sanity: a true 4xx does increment.
    _record_failure_strike(db_conn, item_id, True, 404, log)
    assert (
        db_conn.execute(
            "SELECT consecutive_ingest_4xx FROM items WHERE market_hash = ?", ("X",)
        ).fetchone()["consecutive_ingest_4xx"]
        == 1
    )
