"""Ingestion orchestrator.

Fans out across the supplied item list using a single :class:`SteamMarketClient`,
validates each response, persists valid records to ``price_history`` /
``listing_history`` / ``latest_observation``, routes validation failures to
``quarantine``, and writes a single row to ``runs`` summarizing the outcome.

Observed-at semantics
---------------------
Every successful poll within a single CLI invocation writes its observations
with the same ``observed_at`` value — the **polling slot** corresponding to
``now`` under the configured cadence. The slot is computed once at the top of
the run so all items share it, and primary-key idempotency guarantees that a
re-run within the same slot is a no-op rather than a duplicate write.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from structlog.stdlib import BoundLogger

from dota_deals.config import Settings
from dota_deals.ingest.steam import (
    IngestError,
    IngestValidationError,
    SteamMarketClient,
)
from dota_deals.logging import get_logger
from dota_deals.models.domain import ListingPoint, PricePoint, RunStatus, RunSummary
from dota_deals.storage.db import bootstrap_schema, connect
from dota_deals.storage.repositories import (
    get_item_by_hash,
    insert_listing_point,
    insert_price_point,
    insert_run,
    quarantine_record,
    update_run,
    upsert_latest_observation,
)

_ItemOutcome = Literal["ok", "quarantined", "failed"]


@dataclass(frozen=True)
class _ItemResult:
    outcome: _ItemOutcome
    item_hash: str


def slot_for(at: datetime, cadence_hours: int) -> datetime:
    """Truncate ``at`` to the polling slot for the given cadence.

    The cadence is expected to divide 24 evenly (enforced in :class:`Settings`);
    the slot is `floor(at.hour / cadence_hours) * cadence_hours` with minute /
    second / microsecond zeroed and timezone preserved.

    :raises ValueError: if ``at`` is naive (no timezone) or not UTC.
    """
    if at.tzinfo is None or at.utcoffset() != timedelta(0):
        raise ValueError(f"slot_for requires a UTC-aware datetime, got {at!r}")
    slot_hour = (at.hour // cadence_hours) * cadence_hours
    return at.replace(hour=slot_hour, minute=0, second=0, microsecond=0)


async def run_ingestion(
    items: list[str],
    settings: Settings,
    *,
    run_id: str,
    parent_run_id: str | None = None,
    now: datetime | None = None,
) -> RunSummary:
    """Ingest current price and listing data for every item in ``items``.

    Each item is fetched with both ``fetch_price_overview`` and
    ``fetch_listings``. Valid responses are persisted; validation failures are
    quarantined; transport/HTTP failures are counted but do not abort the run.

    :param items: list of Steam ``market_hash_name`` values to fetch. Items
        absent from the ``items`` table count as failures (the universe stage
        is responsible for populating that table).
    :param settings: process settings (concurrency, timeouts, cool-down, DB
        path, cadence).
    :param run_id: UUID4 identifying this ingestion run.
    :param parent_run_id: optional UUID4 grouping this run with sibling stage
        runs from the same CLI invocation.
    :param now: optional override for the run's wall-clock time; tests use
        this to control polling-slot truncation. Defaults to
        ``datetime.now(UTC)``.
    """
    started_at = now if now is not None else datetime.now(UTC)
    observed_at = slot_for(started_at, settings.ingest_cadence_hours)

    log = get_logger("dota_deals.ingest.runner").bind(
        source="ingest",
        run_id=run_id,
        observed_at=observed_at.isoformat(),
    )

    conn = connect(settings.db_path)
    try:
        bootstrap_schema(conn)
        insert_run(
            conn,
            RunSummary(
                run_id=run_id,
                parent_run_id=parent_run_id,
                kind="ingest",
                started_at=started_at,
                finished_at=None,
                status="running",
                items_ok=0,
                items_quarantined=0,
                items_failed=0,
                notes=None,
            ),
        )

        async with SteamMarketClient(settings) as client:
            tasks = [
                _ingest_one(
                    client=client,
                    conn=conn,
                    item_hash=item_hash,
                    observed_at=observed_at,
                    run_id=run_id,
                    log=log,
                )
                for item_hash in items
            ]
            results = await asyncio.gather(*tasks)

        items_ok = sum(1 for r in results if r.outcome == "ok")
        items_quarantined = sum(1 for r in results if r.outcome == "quarantined")
        items_failed = sum(1 for r in results if r.outcome == "failed")

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
            "ingest run finished",
            status=final_status,
            items_ok=items_ok,
            items_quarantined=items_quarantined,
            items_failed=items_failed,
        )

        return RunSummary(
            run_id=run_id,
            parent_run_id=parent_run_id,
            kind="ingest",
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


async def _ingest_one(
    *,
    client: SteamMarketClient,
    conn: sqlite3.Connection,
    item_hash: str,
    observed_at: datetime,
    run_id: str,
    log: BoundLogger,
) -> _ItemResult:
    item_log = log.bind(item_hash=item_hash)

    item = get_item_by_hash(conn, item_hash)
    if item is None:
        item_log.warning(
            "item not in items table, counting as failed",
            reason="universe stage has not seen this item",
        )
        return _ItemResult(outcome="failed", item_hash=item_hash)

    try:
        overview = await client.fetch_price_overview(item_hash)
    except IngestValidationError as ve:
        quarantine_record(
            conn,
            run_id=run_id,
            source=ve.source,
            item_hash=item_hash,
            raw_payload=ve.raw_payload,
            error_type=ve.error_type,
            error_message=ve.error_message,
        )
        item_log.warning("price overview quarantined", error_type=ve.error_type)
        return _ItemResult(outcome="quarantined", item_hash=item_hash)
    except IngestError as ie:
        item_log.warning(
            "price overview failed",
            status_code=ie.status_code,
            error=str(ie),
        )
        return _ItemResult(outcome="failed", item_hash=item_hash)

    try:
        listings = await client.fetch_listings(item_hash)
    except IngestValidationError as ve:
        quarantine_record(
            conn,
            run_id=run_id,
            source=ve.source,
            item_hash=item_hash,
            raw_payload=ve.raw_payload,
            error_type=ve.error_type,
            error_message=ve.error_message,
        )
        item_log.warning("listings quarantined", error_type=ve.error_type)
        return _ItemResult(outcome="quarantined", item_hash=item_hash)
    except IngestError as ie:
        item_log.warning(
            "listings failed",
            status_code=ie.status_code,
            error=str(ie),
        )
        return _ItemResult(outcome="failed", item_hash=item_hash)

    # Steam returned 200s but item may not have prices yet (newly listed).
    # That's not an error — but we can't write a row without a price. Count as
    # failed (the item still shows up in the run summary; nothing in
    # price_history yet).
    if not overview.success or overview.lowest_cents is None:
        item_log.info(
            "no price data yet",
            success=overview.success,
            has_lowest=overview.lowest_cents is not None,
        )
        return _ItemResult(outcome="failed", item_hash=item_hash)

    price_point = PricePoint(
        item_id=item.item_id,
        observed_at=observed_at,
        lowest_cents=overview.lowest_cents,
        median_cents=overview.median_cents,
        volume_24h=overview.volume_24h,
    )
    listing_point = ListingPoint(
        item_id=item.item_id,
        observed_at=observed_at,
        listings_count=listings.listings_count,
    )

    insert_price_point(conn, price_point)
    insert_listing_point(conn, listing_point)
    upsert_latest_observation(conn, price_point, listing_point.listings_count)

    return _ItemResult(outcome="ok", item_hash=item_hash)
