"""Universe discovery — build the list of items to track.

Pages ``https://steamcommunity.com/market/search/render`` (``?norender=1`` so
Steam returns JSON, not HTML), filtered to the rarity tags corresponding to
arcanas and immortals for Dota 2 (``appid=570``). Each discovered item is
upserted into the ``items`` table.

Reactivation
------------
A successful universe sighting unconditionally reactivates a previously
deactivated item (``active=1``) and resets its ingest-strike counter to
``0``. The premise is: if Steam still lists the item, the ingest stage
deserves another chance — its previous 4xx run may have been a transient
classid mismatch or a temporary delisting that the universe stage has now
re-confirmed away.

Tag values
----------
The Steam Market filter tags are undocumented Valve internals. The pair below
is what currently produces the right item universe for Dota 2; if Steam ever
renames them the universe stage stops producing results and an operator will
need to discover the new values via the Steam UI ("Filter results" panel)
and update the mapping here.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from structlog.stdlib import BoundLogger

from dota_deals.config import Settings
from dota_deals.ingest.steam import (
    IngestError,
    IngestValidationError,
    SteamMarketClient,
)
from dota_deals.logging import get_logger
from dota_deals.models.domain import Item, ItemCategory, RunStatus, RunSummary
from dota_deals.storage.db import bootstrap_schema, connect
from dota_deals.storage.repositories import (
    insert_run,
    quarantine_record,
    update_run,
    upsert_item,
)

# Undocumented Steam rarity tags. See the module docstring for the source of
# truth on these values.
_RARITY_TAGS: tuple[tuple[ItemCategory, str], ...] = (
    ("arcana", "tag_Rarity_Arcana"),
    ("immortal", "tag_Rarity_Immortal"),
)

# Default page size for /market/search/render. Steam quietly caps at 100;
# requesting more silently returns fewer.
_DEFAULT_PAGE_SIZE = 100

# Hard safety ceiling on total pages per category — protects against a buggy
# Steam response where total_count never decreases.
_MAX_PAGES_PER_CATEGORY = 200


async def refresh_universe(
    settings: Settings,
    *,
    run_id: str,
    parent_run_id: str | None = None,
    now: datetime | None = None,
    page_size: int = _DEFAULT_PAGE_SIZE,
) -> RunSummary:
    """Discover and upsert every arcana and immortal Steam currently lists.

    Iterates the two rarity tags, paginating each until ``start >= total_count``.
    Each result is upserted into ``items`` via :func:`upsert_item`, which
    resets the strike counter and forces ``active=1`` (so previously
    deactivated items get reactivated on sighting).

    Per-category failures (network exhausted, non-retriable HTTP) count as
    one ``items_failed`` each; validation failures route the offending page
    body to ``quarantine`` and count as one ``items_quarantined``. The run
    continues across categories regardless of failures.

    :param settings: process settings (concurrency, timeouts, DB path).
    :param run_id: UUID4 identifying this universe-refresh run.
    :param parent_run_id: optional UUID4 grouping this run with sibling
        stage runs from the same CLI invocation.
    :param now: optional override for the run's wall-clock time; tests use
        this for determinism. Defaults to :func:`datetime.now` (UTC).
    :param page_size: passed straight through to
        :meth:`SteamMarketClient.fetch_search_page`. Default 100.
    """
    started_at = now if now is not None else datetime.now(UTC)
    log = get_logger("dota_deals.ingest.universe").bind(
        source="universe",
        run_id=run_id,
    )

    conn = connect(settings.db_path)
    try:
        bootstrap_schema(conn)
        insert_run(
            conn,
            RunSummary(
                run_id=run_id,
                parent_run_id=parent_run_id,
                kind="universe",
                started_at=started_at,
                finished_at=None,
                status="running",
                items_ok=0,
                items_quarantined=0,
                items_failed=0,
                notes=None,
            ),
        )

        items_ok = 0
        items_quarantined = 0
        items_failed = 0

        async with SteamMarketClient(settings) as client:
            for category, rarity_tag in _RARITY_TAGS:
                cat_log = log.bind(category=category, rarity_tag=rarity_tag)
                try:
                    upserted = await _discover_category(
                        client=client,
                        conn=conn,
                        category=category,
                        rarity_tag=rarity_tag,
                        now=started_at,
                        page_size=page_size,
                        log=cat_log,
                    )
                    items_ok += upserted
                    cat_log.info("category complete", items_discovered=upserted)
                except IngestValidationError as ve:
                    quarantine_record(
                        conn,
                        run_id=run_id,
                        source=ve.source,
                        item_hash=None,
                        raw_payload=ve.raw_payload,
                        error_type=ve.error_type,
                        error_message=ve.error_message,
                    )
                    items_quarantined += 1
                    cat_log.warning("category quarantined", error_type=ve.error_type)
                except IngestError as ie:
                    items_failed += 1
                    cat_log.error("category failed", error=str(ie), status_code=ie.status_code)

        final_status: RunStatus = (
            "success" if items_quarantined == 0 and items_failed == 0 else "partial"
        )
        finished_at = datetime.now(UTC)
        update_run(
            conn,
            run_id,
            status=final_status,
            items_ok=items_ok,
            items_quarantined=items_quarantined,
            items_failed=items_failed,
        )

        log.info(
            "universe run finished",
            status=final_status,
            items_ok=items_ok,
            items_quarantined=items_quarantined,
            items_failed=items_failed,
        )

        return RunSummary(
            run_id=run_id,
            parent_run_id=parent_run_id,
            kind="universe",
            started_at=started_at,
            finished_at=finished_at,
            status=final_status,
            items_ok=items_ok,
            items_quarantined=items_quarantined,
            items_failed=items_failed,
            notes=None,
        )
    finally:
        conn.close()


async def _discover_category(
    *,
    client: SteamMarketClient,
    conn: sqlite3.Connection,
    category: ItemCategory,
    rarity_tag: str,
    now: datetime,
    page_size: int,
    log: BoundLogger,
) -> int:
    """Paginate one category, upserting each result. Returns count of upserts."""
    upserted = 0
    start = 0
    page_number = 0
    while True:
        page_number += 1
        if page_number > _MAX_PAGES_PER_CATEGORY:
            log.warning(
                "hit pagination safety ceiling, stopping early",
                page_number=page_number,
                start=start,
            )
            return upserted

        page = await client.fetch_search_page(rarity_tag=rarity_tag, start=start, count=page_size)

        if not page.success:
            log.warning("page returned success=false; stopping", start=start)
            return upserted

        if not page.results:
            return upserted

        for result in page.results:
            item = Item(
                item_id=0,  # ignored by upsert_item
                market_hash=result.market_hash_name,
                name=result.name,
                category=category,
                hero=None,  # hero parsing is deferred; comparables uses null fallback
                first_seen_at=now,
                last_seen_at=now,
                active=True,
                consecutive_ingest_4xx=0,
            )
            upsert_item(conn, item)
            upserted += 1

        start += len(page.results)
        if start >= page.total_count:
            return upserted
