"""Builders for the wire-format JSON payloads.

Each function is read-only against D1 and returns a fully populated wire
model (or ``None`` for the historical/per-item lookups when the entity
doesn't exist). The "latest" and "health" builders never raise on empty
state — they describe the warmup case explicitly, since the frontend
depends on that contract for its empty-state UI.

These builders are the only place that translates between the internal
domain (integer cents, raw datetimes) and the wire (USD strings, ``Z``
suffixes, ``schema_version`` envelopes).

Phase 9c-iii: storage moves to async D1. The builders take a
:class:`D1Connection` and run reads via async repository calls plus a
small number of private helpers that issue raw ``conn.query`` (the
helpers are publish-specific concerns — "most recent score date", "row
count of items with the active flag set" — that don't belong in the
public repository surface). Item-detail per-call queries (~3 per item)
are accepted at the v1 scale; publish is once-daily.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, date, datetime
from typing import Any

from dota_deals.models.domain import Item, Signal, SignalName
from dota_deals.publish.models import (
    Health,
    HistoricalReport,
    ItemDetail,
    LatestReport,
    PipelineStatus,
    WireDataCoverage,
    WireDataQuality,
    WireListingPoint,
    WirePricePoint,
    WireRunRef,
    WireScore,
    WireScoreComponents,
    WireSignalPoint,
    WireSignalSeries,
    WireWarmupEstimate,
    cents_to_usd_string,
)
from dota_deals.storage.d1_client import D1QueryError
from dota_deals.storage.db import D1Connection, StorageError
from dota_deals.storage.repositories import (
    daily_prices,
    get_item_by_id,
    items_missing_observation_for_date,
    latest_ingest_run_for_date,
    latest_scores,
    recent_listings,
    recent_signals,
)

_WARMUP_THRESHOLD_DAYS = 30  # longest signal warmup window (price_zscore)
_DETAIL_HISTORY_DAYS = 30
_ALL_SIGNAL_NAMES: tuple[SignalName, ...] = (
    "price_zscore",
    "supply_velocity",
    "event_proximity",
    "comparables_delta",
)


# ----------------------------- public builders --------------------------------


async def build_latest_report(conn: D1Connection, *, top_n: int = 20) -> LatestReport:
    """Build the wire payload for ``public/data/latest.json``.

    The most recent scored date's top-``top_n`` rows, ranked. If no
    scored date exists yet, returns the warmup envelope (``status="warmup"``,
    ``scores=[]``) — never raises.
    """
    most_recent = await _most_recent_score_date(conn)
    now = _now_utc()

    if most_recent is None:
        return LatestReport(
            schema_version=1,
            generated_at=now,
            report_date=None,
            status="warmup",
            data_quality=WireDataQuality(
                ingest_status="missing", ingest_run_id=None, missing_items=[]
            ),
            scores=[],
        )

    scores = await _build_scores_for_date(conn, most_recent, top_n=top_n)
    data_quality = await _build_data_quality(conn, most_recent)
    status = _resolve_status(scores_exist=True, ingest_status=data_quality.ingest_status)

    return LatestReport(
        schema_version=1,
        generated_at=now,
        report_date=most_recent,
        status=status,
        data_quality=data_quality,
        scores=scores,
    )


async def build_historical_report(
    conn: D1Connection, on: date, *, top_n: int = 20
) -> HistoricalReport | None:
    """Build the wire payload for ``public/data/history/YYYY-MM-DD.json``.

    Returns ``None`` if no scores exist for ``on``; the caller decides
    what to do (the ``publish`` CLI skips writing the file in that case).
    """
    if not await _date_has_scores(conn, on):
        return None
    scores = await _build_scores_for_date(conn, on, top_n=top_n)
    data_quality = await _build_data_quality(conn, on)
    return HistoricalReport(
        schema_version=1,
        generated_at=_now_utc(),
        report_date=on,
        data_quality=data_quality,
        scores=scores,
    )


async def build_health(conn: D1Connection) -> Health:
    """Build the wire payload for ``public/data/health.json``.

    Status precedence:

    1. ``warmup`` — no scored date exists yet.
    2. ``degraded`` — most recent ingest run was ``partial``.
    3. ``operational`` — everything else.

    ``warmup_estimate.days_remaining`` is ``None`` once observations span
    ≥ 30 calendar days (the longest signal-warmup window).
    """
    now = _now_utc()
    coverage = await _build_data_coverage(conn, now)
    warmup = _build_warmup_estimate(coverage)

    most_recent_score_date = await _most_recent_score_date(conn)
    ingest_status_today = await _ingest_status_for(conn, now.date())
    status = _resolve_status(
        scores_exist=most_recent_score_date is not None,
        ingest_status=ingest_status_today,
    )

    last_run = await _latest_successful_run(conn)
    return Health(
        schema_version=1,
        generated_at=now,
        status=status,
        last_run=last_run,
        data_coverage=coverage,
        warmup_estimate=warmup,
    )


async def build_item_detail(
    conn: D1Connection, item_id: int, *, history_days: int = _DETAIL_HISTORY_DAYS
) -> ItemDetail | None:
    """Build the wire payload for ``public/data/items/<item_id>.json``.

    Returns ``None`` if ``item_id`` isn't in the ``items`` table.
    """
    item = await get_item_by_id(conn, item_id)
    if item is None:
        return None
    now = _now_utc()
    as_of = now.date()

    daily_rows = await daily_prices(conn, item_id, days=history_days, as_of=as_of)
    daily = [
        WirePricePoint(date=d, lowest_price=cents_to_usd_string(cents)) for d, cents in daily_rows
    ]
    listing_rows = await recent_listings(conn, item_id, days=history_days, as_of=as_of)
    listings = [
        WireListingPoint(observed_at=p.observed_at, listings_count=p.listings_count)
        for p in listing_rows
    ]
    signals = await recent_signals(conn, item_id, days=history_days, as_of=as_of)
    signal_series = _group_signals_into_series(signals)

    return ItemDetail(
        schema_version=1,
        generated_at=now,
        item_id=item.item_id,
        market_hash_name=item.market_hash,
        name=item.name,
        category=item.category,
        hero=item.hero,
        active=item.active,
        daily_prices=daily,
        listings=listings,
        signals=signal_series,
    )


# ----------------------------- internals --------------------------------------


def _now_utc() -> datetime:
    return datetime.now(UTC)


async def _most_recent_score_date(conn: D1Connection) -> date | None:
    try:
        result = await conn.query("SELECT MAX(computed_for) AS d FROM scores")
    except D1QueryError as e:
        raise StorageError(f"_most_recent_score_date failed: {e}") from e
    if not result.results or result.results[0]["d"] is None:
        return None
    return date.fromisoformat(str(result.results[0]["d"]))


async def _date_has_scores(conn: D1Connection, on: date) -> bool:
    try:
        result = await conn.query(
            "SELECT 1 AS hit FROM scores WHERE computed_for = ? LIMIT 1",
            (on.isoformat(),),
        )
    except D1QueryError as e:
        raise StorageError(f"_date_has_scores failed: {e}") from e
    return bool(result.results)


async def _build_scores_for_date(conn: D1Connection, on: date, *, top_n: int) -> list[WireScore]:
    domain_scores = await latest_scores(conn, on, top_n)
    if not domain_scores:
        return []
    # One round-trip to fetch all the items we need.
    item_ids = tuple(s.item_id for s in domain_scores)
    items_by_id = await _items_by_id(conn, item_ids)
    latest_prices = await _latest_prices_for(conn, item_ids)

    wire_scores: list[WireScore] = []
    for s in domain_scores:
        item = items_by_id.get(s.item_id)
        if item is None:
            # Shouldn't happen — scores FKs items — but be defensive.
            continue
        current_cents = latest_prices.get(s.item_id)
        current_price = cents_to_usd_string(current_cents) if current_cents is not None else None
        null_signals_raw = s.data_quality.get("null_signals", [])
        null_signals = list(null_signals_raw) if isinstance(null_signals_raw, list) else []
        wire_scores.append(
            WireScore(
                item_id=item.item_id,
                market_hash_name=item.market_hash,
                name=item.name,
                category=item.category,
                hero=item.hero,
                current_price=current_price,
                computed_for=s.computed_for,
                buy_score=s.score,
                components=WireScoreComponents(
                    price_zscore=s.components.get("price_zscore"),
                    supply_velocity=s.components.get("supply_velocity"),
                    event_proximity=s.components.get("event_proximity"),
                    comparables_delta=s.components.get("comparables_delta"),
                ),
                explanation=s.explanation,
                null_signals=null_signals,
            )
        )
    return wire_scores


async def _items_by_id(conn: D1Connection, item_ids: Sequence[int]) -> dict[int, Item]:
    """Bulk fetch of a small fixed-size set of items by id (top-N for publish)."""
    if not item_ids:
        return {}
    placeholders = ",".join("?" * len(item_ids))
    try:
        result = await conn.query(
            f"SELECT item_id, market_hash, name, category, hero, "
            f"first_seen_at, last_seen_at, active, consecutive_ingest_4xx "
            f"FROM items WHERE item_id IN ({placeholders})",
            tuple(item_ids),
        )
    except D1QueryError as e:
        raise StorageError(f"_items_by_id failed: {e}") from e
    return {int(row["item_id"]): _row_to_item(row) for row in result.results}


async def _latest_prices_for(conn: D1Connection, item_ids: Sequence[int]) -> dict[int, int]:
    if not item_ids:
        return {}
    placeholders = ",".join("?" * len(item_ids))
    try:
        result = await conn.query(
            f"SELECT item_id, lowest_cents FROM latest_observation "
            f"WHERE item_id IN ({placeholders})",
            tuple(item_ids),
        )
    except D1QueryError as e:
        raise StorageError(f"_latest_prices_for failed: {e}") from e
    return {int(r["item_id"]): int(r["lowest_cents"]) for r in result.results}


async def _build_data_quality(conn: D1Connection, on: date) -> WireDataQuality:
    info = await latest_ingest_run_for_date(conn, on)
    missing = await items_missing_observation_for_date(conn, on)
    if info is None:
        return WireDataQuality(ingest_status="missing", ingest_run_id=None, missing_items=missing)
    return WireDataQuality(ingest_status=info[1], ingest_run_id=info[0], missing_items=missing)


async def _ingest_status_for(conn: D1Connection, on: date) -> str:
    info = await latest_ingest_run_for_date(conn, on)
    return info[1] if info is not None else "missing"


def _resolve_status(*, scores_exist: bool, ingest_status: str) -> PipelineStatus:
    if not scores_exist:
        return "warmup"
    if ingest_status == "partial":
        return "degraded"
    return "operational"


async def _build_data_coverage(conn: D1Connection, now: datetime) -> WireDataCoverage:
    try:
        items_tracked_r = await conn.query("SELECT COUNT(*) AS n FROM items WHERE active = 1")
        items_tracked = int(items_tracked_r.results[0]["n"])
        items_with_signals_r = await conn.query("SELECT COUNT(DISTINCT item_id) AS n FROM signals")
        items_with_signals = int(items_with_signals_r.results[0]["n"])
        first_at_r = await conn.query("SELECT MIN(observed_at) AS first_at FROM price_history")
    except D1QueryError as e:
        raise StorageError(f"_build_data_coverage failed: {e}") from e

    first_at_raw = first_at_r.results[0]["first_at"] if first_at_r.results else None
    first_at = datetime.fromisoformat(str(first_at_raw)) if first_at_raw else None
    if first_at is not None:
        days_of_history = max(0, (now.date() - first_at.date()).days + 1)
    else:
        days_of_history = 0
    return WireDataCoverage(
        items_tracked=items_tracked,
        items_with_signals=items_with_signals,
        days_of_history=days_of_history,
        first_observation_at=first_at,
    )


def _build_warmup_estimate(coverage: WireDataCoverage) -> WireWarmupEstimate:
    if coverage.first_observation_at is None:
        return WireWarmupEstimate(days_remaining=_WARMUP_THRESHOLD_DAYS)
    remaining = _WARMUP_THRESHOLD_DAYS - coverage.days_of_history
    if remaining <= 0:
        return WireWarmupEstimate(days_remaining=None)
    return WireWarmupEstimate(days_remaining=remaining)


async def _latest_successful_run(conn: D1Connection) -> WireRunRef | None:
    try:
        result = await conn.query(
            """
            SELECT run_id, kind, finished_at, status
            FROM runs
            WHERE status = 'success'
            ORDER BY finished_at DESC
            LIMIT 1
            """,
        )
    except D1QueryError as e:
        raise StorageError(f"_latest_successful_run failed: {e}") from e
    if not result.results:
        return None
    row = result.results[0]
    finished_at_raw = row["finished_at"]
    finished_at = datetime.fromisoformat(str(finished_at_raw)) if finished_at_raw else None
    return WireRunRef(
        run_id=str(row["run_id"]),
        kind=str(row["kind"]),
        finished_at=finished_at,
        status=str(row["status"]),
    )


def _row_to_item(row: dict[str, Any]) -> Item:
    last_seen = row["last_seen_at"]
    return Item(
        item_id=int(row["item_id"]),
        market_hash=str(row["market_hash"]),
        name=str(row["name"]),
        category=row["category"],
        hero=row["hero"],
        first_seen_at=datetime.fromisoformat(str(row["first_seen_at"])),
        last_seen_at=datetime.fromisoformat(str(last_seen)) if last_seen else None,
        active=bool(row["active"]),
        consecutive_ingest_4xx=int(row["consecutive_ingest_4xx"]),
    )


def _group_signals_into_series(signals: Iterable[Signal]) -> list[WireSignalSeries]:
    """Group raw Signal rows by ``signal_name`` and emit a stable list."""
    by_name: dict[str, list[WireSignalPoint]] = defaultdict(list)
    for s in signals:
        by_name[s.signal_name].append(WireSignalPoint(date=s.computed_for, value=s.value))
    out: list[WireSignalSeries] = []
    for name in _ALL_SIGNAL_NAMES:
        out.append(WireSignalSeries(signal_name=name, points=by_name.get(name, [])))
    return out


__all__ = [
    "build_health",
    "build_historical_report",
    "build_item_detail",
    "build_latest_report",
]
