"""Universe-discovery end-to-end tests.

Covers the cases enumerated in the Phase 4a brief:

1. Happy path — multi-page response per rarity, all items upserted.
2. Pagination termination — start >= total_count ends the loop cleanly.
3. 4xx mid-scrape — category counted as failed, the *other* rarity still runs.
4. Malformed JSON — page body lands in quarantine.
5. (See ``test_ingest.py``) deactivation rule fires at exactly 3 strikes.
6. Reactivation — previously deactivated item reappears, ``active`` flips to 1
   and the strike counter resets.

Search-endpoint mocking matches by the ``category_570_Rarity[]`` query param
so a single respx route can serve both rarities with different bodies.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

import httpx
import pytest
import respx

from dota_deals.config import Settings
from dota_deals.ingest.universe import refresh_universe

SEARCH_URL = "https://steamcommunity.com/market/search/render"


# ----------------------------- helpers -----------------------------------------


def _result(market_hash: str, name: str | None = None, type_: str = "Arcana") -> dict[str, object]:
    """Build a single Steam-shaped search result entry."""
    display = name or market_hash
    return {
        "name": display,
        "hash_name": market_hash,
        "asset_description": {
            "market_hash_name": market_hash,
            "name": display,
            "type": type_,
        },
    }


def _page(*, total_count: int, start: int, results: list[dict[str, object]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "success": True,
            "start": start,
            "pagesize": len(results),
            "total_count": total_count,
            "results": results,
        },
    )


def _router_by_tag(
    arcana: Callable[[httpx.Request], httpx.Response] | httpx.Response,
    immortal: Callable[[httpx.Request], httpx.Response] | httpx.Response,
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a respx side-effect that dispatches on the rarity tag param."""

    def dispatch(request: httpx.Request) -> httpx.Response:
        tag = request.url.params.get("category_570_Rarity[]", "")
        target = arcana if tag == "tag_Rarity_Arcana" else immortal
        if callable(target):
            return target(request)
        return target

    return dispatch


# ----------------------------- happy path & pagination -------------------------


@pytest.mark.asyncio
async def test_happy_path_single_page_per_rarity(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """Case 1: each rarity returns one page; every result upserts into items."""
    arcana_page = _page(
        total_count=2,
        start=0,
        results=[
            _result("Manifold Paradox", type_="Arcana"),
            _result("Demon Eater", type_="Arcana"),
        ],
    )
    immortal_page = _page(
        total_count=1,
        start=0,
        results=[_result("Riptide Raider", type_="Immortal Item")],
    )

    with respx.mock(assert_all_called=False) as router:
        router.get(SEARCH_URL).mock(side_effect=_router_by_tag(arcana_page, immortal_page))

        summary = await refresh_universe(settings=settings, run_id="u-1", parent_run_id="parent-1")

    assert summary.status == "success"
    assert summary.items_ok == 3
    assert summary.items_failed == 0
    assert summary.items_quarantined == 0

    rows = db_conn.execute(
        "SELECT market_hash, category, active, consecutive_ingest_4xx FROM items "
        "ORDER BY market_hash"
    ).fetchall()
    assert [(r["market_hash"], r["category"]) for r in rows] == [
        ("Demon Eater", "arcana"),
        ("Manifold Paradox", "arcana"),
        ("Riptide Raider", "immortal"),
    ]
    assert all(row["active"] == 1 for row in rows)
    assert all(row["consecutive_ingest_4xx"] == 0 for row in rows)


@pytest.mark.asyncio
async def test_pagination_terminates_when_start_reaches_total_count(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """Case 2: three pages of two arcanas each (total_count=6), one page of immortals."""
    arcana_pages = [
        _page(
            total_count=6,
            start=0,
            results=[_result(f"Arc{i}") for i in range(2)],
        ),
        _page(
            total_count=6,
            start=2,
            results=[_result(f"Arc{i}") for i in range(2, 4)],
        ),
        _page(
            total_count=6,
            start=4,
            results=[_result(f"Arc{i}") for i in range(4, 6)],
        ),
    ]
    immortal_page = _page(total_count=0, start=0, results=[])  # zero immortals — still a valid page

    def arcana_dispatch(_request: httpx.Request) -> httpx.Response:
        return arcana_pages.pop(0)

    with respx.mock(assert_all_called=False) as router:
        router.get(SEARCH_URL).mock(side_effect=_router_by_tag(arcana_dispatch, immortal_page))

        summary = await refresh_universe(settings=settings, run_id="u-2", page_size=2)

    assert summary.items_ok == 6
    assert summary.status == "success"
    assert db_conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 6
    # Pagination consumed exactly the three arcana pages, no fourth fetch.
    assert arcana_pages == []


# ----------------------------- failure modes -----------------------------------


@pytest.mark.asyncio
async def test_4xx_on_one_rarity_partials_the_run(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """Case 3: 404 on arcanas → that category fails; immortals still upsert."""
    immortal_page = _page(
        total_count=1, start=0, results=[_result("Riptide Raider", type_="Immortal Item")]
    )

    with respx.mock(assert_all_called=False) as router:
        router.get(SEARCH_URL).mock(side_effect=_router_by_tag(httpx.Response(404), immortal_page))

        summary = await refresh_universe(settings=settings, run_id="u-3")

    assert summary.status == "partial"
    assert summary.items_ok == 1
    assert summary.items_failed == 1
    assert summary.items_quarantined == 0

    # The good rarity still landed in items.
    items = db_conn.execute("SELECT market_hash, category FROM items").fetchall()
    assert [(r["market_hash"], r["category"]) for r in items] == [("Riptide Raider", "immortal")]


@pytest.mark.asyncio
async def test_malformed_response_routes_to_quarantine(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """Case 4: a JSON body that doesn't match the search schema is quarantined."""
    bad_page = httpx.Response(
        200,
        json={
            "success": True,
            "start": 0,
            "pagesize": 1,
            "total_count": 1,
            "results": [{"asset_description": {}}],  # no market_hash_name
        },
    )
    immortal_page = _page(total_count=0, start=0, results=[])

    with respx.mock(assert_all_called=False) as router:
        router.get(SEARCH_URL).mock(side_effect=_router_by_tag(bad_page, immortal_page))

        summary = await refresh_universe(settings=settings, run_id="u-4")

    assert summary.status == "partial"
    assert summary.items_quarantined == 1
    assert summary.items_ok == 0

    q_rows = db_conn.execute(
        "SELECT source, item_hash, error_type FROM quarantine WHERE run_id = ?",
        ("u-4",),
    ).fetchall()
    assert len(q_rows) == 1
    assert q_rows[0]["source"] == "steam_market_search"
    assert q_rows[0]["item_hash"] is None  # category-level failure, not item-level


# ----------------------------- reactivation ------------------------------------


@pytest.mark.asyncio
async def test_reactivation_when_item_reappears(
    settings: Settings, db_conn: sqlite3.Connection
) -> None:
    """Case 6: deactivated item with strikes is reactivated and counter resets."""
    db_conn.execute(
        """
        INSERT INTO items (
            market_hash, name, category, hero, first_seen_at,
            active, consecutive_ingest_4xx
        )
        VALUES (?, ?, ?, ?, ?, 0, 5)
        """,
        ("Manifold Paradox", "Manifold Paradox", "arcana", None, "2025-01-01T00:00:00+00:00"),
    )
    db_conn.commit()

    arcana_page = _page(
        total_count=1, start=0, results=[_result("Manifold Paradox", type_="Arcana")]
    )
    immortal_page = _page(total_count=0, start=0, results=[])

    with respx.mock(assert_all_called=False) as router:
        router.get(SEARCH_URL).mock(side_effect=_router_by_tag(arcana_page, immortal_page))

        await refresh_universe(settings=settings, run_id="u-5")

    row = db_conn.execute(
        "SELECT active, consecutive_ingest_4xx FROM items WHERE market_hash = ?",
        ("Manifold Paradox",),
    ).fetchone()
    assert row["active"] == 1
    assert row["consecutive_ingest_4xx"] == 0
