"""Ingestion orchestrator.

Fans out across the supplied item list using a single :class:`SteamMarketClient`,
validates each response, persists valid records to ``price_history`` /
``listing_history`` / ``latest_observation``, routes validation failures to
``quarantine``, and writes a single row to ``runs`` summarizing the outcome.

Phase 9c-i note: storage was moved from local SQLite to Cloudflare D1
over HTTP. The runner now opens a :class:`D1Connection` via
:func:`dota_deals.storage.db_async.connect` and dispatches to
:mod:`dota_deals.storage.repositories_async`. The Steam-side concurrency
model and slot-truncation semantics are unchanged from Phase 3.

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
from dota_deals.storage.db_async import D1Backend, D1Connection, connect
from dota_deals.storage.repositories_async import (
    get_item_by_hash,
    increment_ingest_strikes,
    insert_listing_point,
    insert_price_point,
    insert_run,
    quarantine_record,
    reset_ingest_strikes,
    set_item_active,
    update_run,
    upsert_latest_observation,
)

_ItemOutcome = Literal["ok", "quarantined", "failed"]

# 4xx responses for a single item accumulate as "strikes". Once an item has
# this many consecutive 4xx outcomes, ingest flips ``items.active = 0`` until
# the next universe refresh reactivates it.
_INGEST_DEACTIVATION_THRESHOLD = 3


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
    backend: D1Backend | None = None,
) -> RunSummary:
    """Ingest current price and listing data for every item in ``items``.

    Each item is fetched with both ``fetch_price_overview`` and
    ``fetch_listings``. Valid responses are persisted; validation failures are
    quarantined; transport/HTTP failures are counted but do not abort the run.

    :param items: list of Steam ``market_hash_name`` values to fetch. Items
        absent from the ``items`` table count as failures (the universe stage
        is responsible for populating that table).
    :param settings: process settings (concurrency, timeouts, cool-down, D1
        credentials, cadence).
    :param run_id: UUID4 identifying this ingestion run.
    :param parent_run_id: optional UUID4 grouping this run with sibling stage
        runs from the same CLI invocation.
    :param now: optional override for the run's wall-clock time; tests use
        this to control polling-slot truncation. Defaults to
        ``datetime.now(UTC)``.
    :param backend: test seam. When ``None`` (CLI path), the runner opens a
        real :class:`D1Client` from ``settings``. Tests pass a
        :class:`D1FakeClient` instance to keep the storage in-memory.
    """
    started_at = now if now is not None else datetime.now(UTC)
    observed_at = slot_for(started_at, settings.ingest_cadence_hours)

    log = get_logger("dota_deals.ingest.runner").bind(
        source="ingest",
        run_id=run_id,
        observed_at=observed_at.isoformat(),
    )

    async with connect(settings, backend=backend) as conn:
        await insert_run(
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
        await update_run(
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


async def _ingest_one(
    *,
    client: SteamMarketClient,
    conn: D1Connection,
    item_hash: str,
    observed_at: datetime,
    run_id: str,
    log: BoundLogger,
) -> _ItemResult:
    item_log = log.bind(item_hash=item_hash)

    item = await get_item_by_hash(conn, item_hash)
    if item is None:
        item_log.warning(
            "item not in items table, counting as failed",
            reason="universe stage has not seen this item",
        )
        return _ItemResult(outcome="failed", item_hash=item_hash)

    try:
        overview = await client.fetch_price_overview(item_hash)
    except IngestValidationError as ve:
        await quarantine_record(
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
        await _record_failure_strike(conn, item.item_id, item.active, ie.status_code, item_log)
        return _ItemResult(outcome="failed", item_hash=item_hash)

    try:
        listings = await client.fetch_listings(item_hash)
    except IngestValidationError as ve:
        await quarantine_record(
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
        await _record_failure_strike(conn, item.item_id, item.active, ie.status_code, item_log)
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

    await insert_price_point(conn, price_point)
    await insert_listing_point(conn, listing_point)
    await upsert_latest_observation(conn, price_point, listing_point.listings_count)

    # A successful poll clears any in-flight strikes. Reactivation is the
    # universe stage's job (per docs/ARCHITECTURE.md); ingest only clears the
    # counter so a future 4xx run restarts from zero.
    if item.consecutive_ingest_4xx > 0:
        await reset_ingest_strikes(conn, item.item_id)

    return _ItemResult(outcome="ok", item_hash=item_hash)


async def _record_failure_strike(
    conn: D1Connection,
    item_id: int,
    item_active: bool,
    status_code: int | None,
    item_log: BoundLogger,
) -> None:
    """Increment strikes for ``item_id`` if the failure was a true 4xx.

    Per the architecture: only client-side rejections (400-499, excluding 429)
    count as strikes — timeouts, 5xx, and rate-limits are infrastructure issues
    and shouldn't punish the item. Deactivation triggers exactly when the new
    strike count reaches :data:`_INGEST_DEACTIVATION_THRESHOLD` and the item is
    currently active (so re-counting doesn't re-fire on an already-deactivated
    item).
    """
    if status_code is None or not (400 <= status_code < 500) or status_code == 429:
        return
    new_count = await increment_ingest_strikes(conn, item_id)
    if new_count >= _INGEST_DEACTIVATION_THRESHOLD and item_active:
        await set_item_active(conn, item_id, active=False)
        item_log.warning(
            "item deactivated after consecutive 4xx",
            strike_count=new_count,
            threshold=_INGEST_DEACTIVATION_THRESHOLD,
        )
