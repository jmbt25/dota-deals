"""Async repository layer for D1.

Function-for-function port of :mod:`dota_deals.storage.repositories`, with
three structural shifts:

1. **Async.** Every function is ``async def``; the first argument is a
   :class:`dota_deals.storage.db_async.D1Connection` instead of a
   ``sqlite3.Connection``.

2. **Batch variants** for high-frequency writes (price points, listing
   points, signals, scores, latest_observation upserts). The non-batch
   versions still exist for one-shot writes (universe upserts, run
   bookkeeping); only the points-and-signals path benefits from
   amortizing HTTP latency over a transaction.

3. **Bulk-read variants** for the data the future DataLookup will need:
   :func:`daily_prices_for_items`, :func:`recent_listings_for_items`,
   :func:`latest_observations_all`. Each chunks the IN clause at
   :data:`_BULK_QUERY_CHUNK_SIZE` so a 800-item universe doesn't blow
   past D1's per-statement bound-parameter limit (999 by default).

The :func:`daily_prices` family moves the per-day median into Python
because D1 has no ``create_aggregate`` hook (see ``docs/D1_MIGRATION.md``
for the rationale and cost estimate).

Exception model stays compatible with the sync layer: callers continue
catching :class:`StorageError` / :class:`IntegrityViolation`. D1's typed
exceptions are translated at the boundary in this module — repository
callers don't see :class:`D1QueryError` directly.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from dota_deals.models.domain import (
    BuyScore,
    Item,
    ItemCategory,
    LatestObservation,
    ListingPoint,
    PricePoint,
    RunStatus,
    RunSummary,
    Signal,
)
from dota_deals.models.events import EventRecord
from dota_deals.storage.d1_client import D1QueryError, D1Statement
from dota_deals.storage.db import IntegrityViolation, StorageError
from dota_deals.storage.db_async import D1Connection

# D1's per-statement bound-parameter limit is 100 (documented at
# https://developers.cloudflare.com/d1/platform/limits/, much tighter
# than the upstream SQLite default of 999). Phase 9c-iii's real-D1
# universe smoke test surfaced this — with 1,424 items in the table,
# the next bulk read of signals_for_items_on_date used
# 100 IN placeholders + 1 date param = 101 bound vars and got
# "too many SQL variables at offset 314: SQLITE_ERROR". 90 leaves
# headroom for up to 10 non-IN params per query without me having
# to reason about the budget per call site.
_BULK_QUERY_CHUNK_SIZE = 90

_ITEM_COLUMNS = (
    "item_id, market_hash, name, category, hero, "
    "first_seen_at, last_seen_at, active, consecutive_ingest_4xx"
)


# ----------------------------- helpers ----------------------------------------


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _median_cents(values: Sequence[int]) -> int | None:
    """Integer-floor median of a non-empty sequence; ``None`` for empty.

    Matches the behavior of the SQLite ``MEDIAN`` aggregate previously
    backing ``v_daily_price`` — for even-count inputs, returns
    ``(a + b) // 2`` rather than the floating-point average. Persisted
    prices are integer cents so the integer return keeps downstream
    comparisons exact.
    """
    if not values:
        return None
    sorted_values = sorted(values)
    n = len(sorted_values)
    mid = n // 2
    if n % 2 == 0:
        return (sorted_values[mid - 1] + sorted_values[mid]) // 2
    return sorted_values[mid]


def _chunked(seq: Sequence[Any], size: int) -> list[Sequence[Any]]:
    if size < 1:
        raise ValueError(f"chunk size must be >= 1, got {size}")
    return [seq[i : i + size] for i in range(0, len(seq), size)]


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


def _row_to_event(row: dict[str, Any]) -> EventRecord:
    end = row["end_date"]
    return EventRecord(
        event_id=int(row["event_id"]),
        kind=row["kind"],
        name=str(row["name"]),
        start_date=date.fromisoformat(str(row["start_date"])),
        end_date=date.fromisoformat(str(end)) if end else None,
        confidence=row["confidence"],
        notes=row["notes"],
    )


def _row_to_buy_score(row: dict[str, Any]) -> BuyScore:
    return BuyScore(
        item_id=int(row["item_id"]),
        computed_for=date.fromisoformat(str(row["computed_for"])),
        score=float(row["buy_score"]),
        components=json.loads(str(row["components_json"])),
        explanation=str(row["explanation"]),
        data_quality=(
            json.loads(str(row["data_quality_json"])) if row["data_quality_json"] else {}
        ),
    )


def _translate_integrity(exc: D1QueryError, *, context: str) -> StorageError:
    """Map a D1 integrity violation onto the sync-compatible exception
    hierarchy.

    UNIQUE / FK / CHECK violations all surface from D1 as the same
    :class:`D1QueryError` with a non-None code; the caller wants them
    surfaced as :class:`IntegrityViolation` so existing handlers
    written against the sync layer keep working.
    """
    return IntegrityViolation(f"{context}: {exc}")


def _translate_storage(exc: D1QueryError, *, context: str) -> StorageError:
    return StorageError(f"{context}: {exc}")


# ---- items ----


async def upsert_item(conn: D1Connection, item: Item) -> int:
    """Insert ``item`` or update its mutable fields if ``market_hash`` exists.

    Universe-refresh semantics identical to the sync path:
    ``name``/``category``/``hero``/``last_seen_at`` overwritten, ``active``
    forced to 1, strike counter reset to 0, ``first_seen_at`` preserved.
    """
    try:
        await conn.execute(
            """
            INSERT INTO items
                (market_hash, name, category, hero,
                 first_seen_at, last_seen_at, active, consecutive_ingest_4xx)
            VALUES (?, ?, ?, ?, ?, ?, 1, 0)
            ON CONFLICT(market_hash) DO UPDATE SET
                name = excluded.name,
                category = excluded.category,
                hero = excluded.hero,
                last_seen_at = excluded.last_seen_at,
                active = 1,
                consecutive_ingest_4xx = 0
            """,
            (
                item.market_hash,
                item.name,
                item.category,
                item.hero,
                item.first_seen_at.isoformat(),
                item.last_seen_at.isoformat() if item.last_seen_at else None,
            ),
        )
    except D1QueryError as exc:
        raise _translate_integrity(
            exc, context=f"items upsert failed for market_hash={item.market_hash!r}"
        ) from exc

    # `last_row_id` is unreliable for ON CONFLICT updates (D1 returns 0 on
    # the update path); always re-query for the resolved id.
    result = await conn.query(
        "SELECT item_id FROM items WHERE market_hash = ?", (item.market_hash,)
    )
    if not result.results:
        raise StorageError(f"upsert_item: item_id lookup failed for {item.market_hash!r}")
    return int(result.results[0]["item_id"])


async def upsert_items_batch(conn: D1Connection, items: Sequence[Item]) -> int:
    """Batch variant of :func:`upsert_item` for the universe-refresh hot path.

    Universe currently sights ~500-800 items per refresh; per-item HTTP
    round-trips would push a single refresh into 30+ seconds. Batched
    upserts amortize the HTTP cost over D1's transactional batch
    endpoint.

    The ON CONFLICT clause is identical to :func:`upsert_item`:
    overwrite name/category/hero/last_seen_at, force ``active=1``, reset
    the strike counter. Returns total rows changed (across both insert
    and update paths); the universe runner uses this for telemetry
    only, not for resolving ids — callers that need ids back follow up
    with :func:`get_item_by_hash`.
    """
    if not items:
        return 0
    statements = [
        D1Statement(
            sql=(
                "INSERT INTO items "
                "(market_hash, name, category, hero, "
                "first_seen_at, last_seen_at, active, consecutive_ingest_4xx) "
                "VALUES (?, ?, ?, ?, ?, ?, 1, 0) "
                "ON CONFLICT(market_hash) DO UPDATE SET "
                "name = excluded.name, "
                "category = excluded.category, "
                "hero = excluded.hero, "
                "last_seen_at = excluded.last_seen_at, "
                "active = 1, "
                "consecutive_ingest_4xx = 0"
            ),
            params=(
                i.market_hash,
                i.name,
                i.category,
                i.hero,
                i.first_seen_at.isoformat(),
                i.last_seen_at.isoformat() if i.last_seen_at else None,
            ),
        )
        for i in items
    ]
    try:
        results = await conn.batch(statements)
    except D1QueryError as exc:
        raise _translate_integrity(exc, context="upsert_items_batch failed") from exc
    return sum(r.meta.changes for r in results)


async def get_item_by_hash(conn: D1Connection, market_hash: str) -> Item | None:
    try:
        result = await conn.query(
            f"SELECT {_ITEM_COLUMNS} FROM items WHERE market_hash = ?",
            (market_hash,),
        )
    except D1QueryError as exc:
        raise _translate_storage(
            exc, context=f"lookup failed for market_hash={market_hash!r}"
        ) from exc
    return _row_to_item(result.results[0]) if result.results else None


async def get_item_by_id(conn: D1Connection, item_id: int) -> Item | None:
    try:
        result = await conn.query(
            f"SELECT {_ITEM_COLUMNS} FROM items WHERE item_id = ?",
            (item_id,),
        )
    except D1QueryError as exc:
        raise _translate_storage(exc, context=f"lookup failed for item_id={item_id}") from exc
    return _row_to_item(result.results[0]) if result.results else None


async def active_items(conn: D1Connection) -> list[Item]:
    try:
        result = await conn.query(
            f"SELECT {_ITEM_COLUMNS} FROM items WHERE active = 1 ORDER BY item_id"
        )
    except D1QueryError as exc:
        raise _translate_storage(exc, context="active_items query failed") from exc
    return [_row_to_item(row) for row in result.results]


async def active_items_in_category(
    conn: D1Connection,
    category: ItemCategory,
    *,
    exclude_item_id: int | None = None,
) -> list[Item]:
    """Active items in ``category``; optionally drop ``exclude_item_id``.

    The exclusion supports Signal 4 (comparables) so an item isn't in
    its own peer set.
    """
    try:
        if exclude_item_id is None:
            result = await conn.query(
                f"SELECT {_ITEM_COLUMNS} FROM items "
                "WHERE active = 1 AND category = ? ORDER BY item_id",
                (category,),
            )
        else:
            result = await conn.query(
                f"SELECT {_ITEM_COLUMNS} FROM items "
                "WHERE active = 1 AND category = ? AND item_id != ? "
                "ORDER BY item_id",
                (category, exclude_item_id),
            )
    except D1QueryError as exc:
        raise _translate_storage(
            exc, context=f"active_items_in_category query failed for category={category!r}"
        ) from exc
    return [_row_to_item(row) for row in result.results]


async def increment_ingest_strikes(conn: D1Connection, item_id: int) -> int:
    """Increment ``items.consecutive_ingest_4xx`` for ``item_id`` by 1.

    Returns the new strike count. D1 supports SQLite's RETURNING clause,
    so the increment and read happen in one round-trip.
    """
    try:
        result = await conn.query(
            """
            UPDATE items
            SET consecutive_ingest_4xx = consecutive_ingest_4xx + 1
            WHERE item_id = ?
            RETURNING consecutive_ingest_4xx
            """,
            (item_id,),
        )
    except D1QueryError as exc:
        raise _translate_storage(
            exc, context=f"increment_ingest_strikes failed for item_id={item_id}"
        ) from exc
    if not result.results:
        raise StorageError(f"item_id={item_id} not found in items")
    return int(result.results[0]["consecutive_ingest_4xx"])


async def reset_ingest_strikes(conn: D1Connection, item_id: int) -> None:
    try:
        await conn.execute(
            "UPDATE items SET consecutive_ingest_4xx = 0 WHERE item_id = ?",
            (item_id,),
        )
    except D1QueryError as exc:
        raise _translate_storage(
            exc, context=f"reset_ingest_strikes failed for item_id={item_id}"
        ) from exc


async def set_item_active(conn: D1Connection, item_id: int, *, active: bool) -> None:
    try:
        await conn.execute(
            "UPDATE items SET active = ? WHERE item_id = ?",
            (1 if active else 0, item_id),
        )
    except D1QueryError as exc:
        raise _translate_storage(
            exc, context=f"set_item_active failed for item_id={item_id}"
        ) from exc


# ---- price_history ----


async def insert_price_point(conn: D1Connection, point: PricePoint) -> bool:
    """Insert ``point``; returns ``True`` on insert, ``False`` on PK collision."""
    try:
        changes = await conn.execute(
            """
            INSERT OR IGNORE INTO price_history
                (item_id, observed_at, lowest_cents, median_cents, volume_24h)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                point.item_id,
                point.observed_at.isoformat(),
                point.lowest_cents,
                point.median_cents,
                point.volume_24h,
            ),
        )
    except D1QueryError as exc:
        raise _translate_integrity(
            exc, context=f"price_history insert failed for item_id={point.item_id}"
        ) from exc
    return changes > 0


async def insert_price_points(conn: D1Connection, points: Sequence[PricePoint]) -> int:
    """Batch-insert price points; returns the count of new rows written.

    Re-runs over the same (item_id, observed_at) keys are no-ops thanks to
    ``INSERT OR IGNORE``; the returned count reflects only fresh inserts.
    Idempotency story matches :func:`insert_price_point`.
    """
    if not points:
        return 0
    statements = [
        D1Statement(
            sql=(
                "INSERT OR IGNORE INTO price_history "
                "(item_id, observed_at, lowest_cents, median_cents, volume_24h) "
                "VALUES (?, ?, ?, ?, ?)"
            ),
            params=(
                p.item_id,
                p.observed_at.isoformat(),
                p.lowest_cents,
                p.median_cents,
                p.volume_24h,
            ),
        )
        for p in points
    ]
    try:
        results = await conn.batch(statements)
    except D1QueryError as exc:
        raise _translate_integrity(exc, context="insert_price_points batch failed") from exc
    return sum(r.meta.changes for r in results)


async def recent_prices(
    conn: D1Connection,
    item_id: int,
    days: int,
    *,
    as_of: date,
) -> list[PricePoint]:
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    start = as_of - timedelta(days=days - 1)
    try:
        result = await conn.query(
            """
            SELECT item_id, observed_at, lowest_cents, median_cents, volume_24h
            FROM price_history
            WHERE item_id = ?
              AND date(observed_at) BETWEEN ? AND ?
            ORDER BY observed_at
            """,
            (item_id, start.isoformat(), as_of.isoformat()),
        )
    except D1QueryError as exc:
        raise _translate_storage(
            exc, context=f"recent_prices failed for item_id={item_id}"
        ) from exc
    return [
        PricePoint(
            item_id=int(row["item_id"]),
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
            lowest_cents=int(row["lowest_cents"]),
            median_cents=int(row["median_cents"]) if row["median_cents"] is not None else None,
            volume_24h=int(row["volume_24h"]) if row["volume_24h"] is not None else None,
        )
        for row in result.results
    ]


async def daily_prices(
    conn: D1Connection,
    item_id: int,
    days: int,
    *,
    as_of: date,
) -> list[tuple[date, int]]:
    """Per-day ``(utc_date, median_lowest_cents)`` series for one item.

    Replaces the sync ``v_daily_price`` view-based path: SELECTs raw
    rows from ``price_history`` and groups + medians them in Python.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    start = as_of - timedelta(days=days - 1)
    try:
        result = await conn.query(
            """
            SELECT date(observed_at) AS utc_date, lowest_cents
            FROM price_history
            WHERE item_id = ?
              AND date(observed_at) BETWEEN ? AND ?
            ORDER BY observed_at
            """,
            (item_id, start.isoformat(), as_of.isoformat()),
        )
    except D1QueryError as exc:
        raise _translate_storage(exc, context=f"daily_prices failed for item_id={item_id}") from exc
    by_date: dict[date, list[int]] = {}
    for row in result.results:
        d = date.fromisoformat(str(row["utc_date"]))
        by_date.setdefault(d, []).append(int(row["lowest_cents"]))
    series: list[tuple[date, int]] = []
    for d in sorted(by_date):
        median = _median_cents(by_date[d])
        if median is not None:
            series.append((d, median))
    return series


async def daily_prices_for_items(
    conn: D1Connection,
    item_ids: Sequence[int],
    days: int,
    *,
    as_of: date,
) -> dict[int, list[tuple[date, int]]]:
    """Bulk variant of :func:`daily_prices`.

    The returned dict has one entry per ``item_id`` in ``item_ids``,
    even when an item has no observations in the window (value is
    ``[]``). Chunks the IN clause at :data:`_BULK_QUERY_CHUNK_SIZE`
    so the universe scales past D1's per-statement parameter limit.
    """
    if not item_ids:
        return {}
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    start = as_of - timedelta(days=days - 1)
    by_item: dict[int, dict[date, list[int]]] = {iid: {} for iid in item_ids}
    for chunk in _chunked(list(item_ids), _BULK_QUERY_CHUNK_SIZE):
        placeholders = ",".join("?" * len(chunk))
        sql = (
            "SELECT item_id, date(observed_at) AS utc_date, lowest_cents "
            "FROM price_history "
            f"WHERE item_id IN ({placeholders}) "
            "AND date(observed_at) BETWEEN ? AND ? "
            "ORDER BY item_id, observed_at"
        )
        params = (*chunk, start.isoformat(), as_of.isoformat())
        try:
            result = await conn.query(sql, params)
        except D1QueryError as exc:
            raise _translate_storage(exc, context="daily_prices_for_items failed") from exc
        for row in result.results:
            iid = int(row["item_id"])
            d = date.fromisoformat(str(row["utc_date"]))
            by_item.setdefault(iid, {}).setdefault(d, []).append(int(row["lowest_cents"]))

    out: dict[int, list[tuple[date, int]]] = {}
    for iid, by_date in by_item.items():
        series: list[tuple[date, int]] = []
        for d in sorted(by_date):
            median = _median_cents(by_date[d])
            if median is not None:
                series.append((d, median))
        out[iid] = series
    return out


# ---- listing_history ----


async def insert_listing_point(conn: D1Connection, point: ListingPoint) -> bool:
    try:
        changes = await conn.execute(
            """
            INSERT OR IGNORE INTO listing_history (item_id, observed_at, listings_count)
            VALUES (?, ?, ?)
            """,
            (point.item_id, point.observed_at.isoformat(), point.listings_count),
        )
    except D1QueryError as exc:
        raise _translate_integrity(
            exc, context=f"listing_history insert failed for item_id={point.item_id}"
        ) from exc
    return changes > 0


async def insert_listing_points(conn: D1Connection, points: Sequence[ListingPoint]) -> int:
    if not points:
        return 0
    statements = [
        D1Statement(
            sql=(
                "INSERT OR IGNORE INTO listing_history "
                "(item_id, observed_at, listings_count) VALUES (?, ?, ?)"
            ),
            params=(p.item_id, p.observed_at.isoformat(), p.listings_count),
        )
        for p in points
    ]
    try:
        results = await conn.batch(statements)
    except D1QueryError as exc:
        raise _translate_integrity(exc, context="insert_listing_points batch failed") from exc
    return sum(r.meta.changes for r in results)


async def recent_listings(
    conn: D1Connection,
    item_id: int,
    days: int,
    *,
    as_of: date,
) -> list[ListingPoint]:
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    start = as_of - timedelta(days=days - 1)
    try:
        result = await conn.query(
            """
            SELECT item_id, observed_at, listings_count
            FROM listing_history
            WHERE item_id = ?
              AND date(observed_at) BETWEEN ? AND ?
            ORDER BY observed_at
            """,
            (item_id, start.isoformat(), as_of.isoformat()),
        )
    except D1QueryError as exc:
        raise _translate_storage(
            exc, context=f"recent_listings failed for item_id={item_id}"
        ) from exc
    return [
        ListingPoint(
            item_id=int(row["item_id"]),
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
            listings_count=int(row["listings_count"]),
        )
        for row in result.results
    ]


async def recent_listings_for_items(
    conn: D1Connection,
    item_ids: Sequence[int],
    days: int,
    *,
    as_of: date,
) -> dict[int, list[ListingPoint]]:
    """Bulk variant: returns a dict keyed by ``item_id`` with the same
    per-item series shape as :func:`recent_listings`. Items with no
    observations in the window map to ``[]``."""
    if not item_ids:
        return {}
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    start = as_of - timedelta(days=days - 1)
    out: dict[int, list[ListingPoint]] = {iid: [] for iid in item_ids}
    for chunk in _chunked(list(item_ids), _BULK_QUERY_CHUNK_SIZE):
        placeholders = ",".join("?" * len(chunk))
        sql = (
            "SELECT item_id, observed_at, listings_count "
            "FROM listing_history "
            f"WHERE item_id IN ({placeholders}) "
            "AND date(observed_at) BETWEEN ? AND ? "
            "ORDER BY item_id, observed_at"
        )
        params = (*chunk, start.isoformat(), as_of.isoformat())
        try:
            result = await conn.query(sql, params)
        except D1QueryError as exc:
            raise _translate_storage(exc, context="recent_listings_for_items failed") from exc
        for row in result.results:
            iid = int(row["item_id"])
            out.setdefault(iid, []).append(
                ListingPoint(
                    item_id=iid,
                    observed_at=datetime.fromisoformat(str(row["observed_at"])),
                    listings_count=int(row["listings_count"]),
                )
            )
    return out


# ---- latest_observation ----


async def upsert_latest_observation(
    conn: D1Connection, point: PricePoint, listings_count: int | None
) -> None:
    """Upsert the ``latest_observation`` row for ``point.item_id``.

    Older observed_at values do not overwrite a newer cached row — the
    WHERE clause on the ON CONFLICT branch enforces that invariant.
    """
    try:
        await conn.execute(
            """
            INSERT INTO latest_observation
                (item_id, observed_at, lowest_cents, listings_count)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(item_id) DO UPDATE SET
                observed_at = excluded.observed_at,
                lowest_cents = excluded.lowest_cents,
                listings_count = excluded.listings_count
            WHERE excluded.observed_at >= latest_observation.observed_at
            """,
            (
                point.item_id,
                point.observed_at.isoformat(),
                point.lowest_cents,
                listings_count,
            ),
        )
    except D1QueryError as exc:
        raise _translate_integrity(
            exc, context=f"latest_observation upsert failed for item_id={point.item_id}"
        ) from exc


async def upsert_latest_observations(
    conn: D1Connection,
    pairs: Sequence[tuple[PricePoint, int | None]],
) -> int:
    """Batch variant. ``pairs`` is ``(price_point, listings_count)``."""
    if not pairs:
        return 0
    statements = [
        D1Statement(
            sql=(
                "INSERT INTO latest_observation "
                "(item_id, observed_at, lowest_cents, listings_count) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(item_id) DO UPDATE SET "
                "observed_at = excluded.observed_at, "
                "lowest_cents = excluded.lowest_cents, "
                "listings_count = excluded.listings_count "
                "WHERE excluded.observed_at >= latest_observation.observed_at"
            ),
            params=(
                p.item_id,
                p.observed_at.isoformat(),
                p.lowest_cents,
                listings_count,
            ),
        )
        for p, listings_count in pairs
    ]
    try:
        results = await conn.batch(statements)
    except D1QueryError as exc:
        raise _translate_integrity(exc, context="upsert_latest_observations batch failed") from exc
    return sum(r.meta.changes for r in results)


async def latest_observations_all(conn: D1Connection) -> dict[int, LatestObservation]:
    """Snapshot of the entire ``latest_observation`` table.

    Used by the DataLookup pre-fetch (Phase 9c) so per-signal compute
    functions don't hit the DB. The dict is keyed by ``item_id`` for
    O(1) access from the compute path.
    """
    try:
        result = await conn.query(
            "SELECT item_id, observed_at, lowest_cents, listings_count FROM latest_observation"
        )
    except D1QueryError as exc:
        raise _translate_storage(exc, context="latest_observations_all failed") from exc
    return {
        int(row["item_id"]): LatestObservation(
            item_id=int(row["item_id"]),
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
            lowest_cents=int(row["lowest_cents"]),
            listings_count=(
                int(row["listings_count"]) if row["listings_count"] is not None else None
            ),
        )
        for row in result.results
    }


# ---- events ----


async def insert_event(conn: D1Connection, event: EventRecord) -> int:
    try:
        result = await conn.query(
            """
            INSERT INTO events (kind, name, start_date, end_date, confidence, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            RETURNING event_id
            """,
            (
                event.kind,
                event.name,
                event.start_date.isoformat(),
                event.end_date.isoformat() if event.end_date else None,
                event.confidence,
                event.notes,
            ),
        )
    except D1QueryError as exc:
        raise _translate_integrity(exc, context="events insert failed") from exc
    if not result.results:
        raise StorageError("insert_event returned without a row")
    return int(result.results[0]["event_id"])


async def next_event_within(
    conn: D1Connection, as_of: date, *, days_window: int
) -> EventRecord | None:
    if days_window < 0:
        raise ValueError(f"days_window must be >= 0, got {days_window}")
    upper = as_of + timedelta(days=days_window)
    try:
        result = await conn.query(
            """
            SELECT event_id, kind, name, start_date, end_date, confidence, notes
            FROM events
            WHERE start_date BETWEEN ? AND ?
            ORDER BY start_date
            LIMIT 1
            """,
            (as_of.isoformat(), upper.isoformat()),
        )
    except D1QueryError as exc:
        raise _translate_storage(exc, context="next_event_within query failed") from exc
    return _row_to_event(result.results[0]) if result.results else None


async def past_events_of_kind(conn: D1Connection, kind: str, *, before: date) -> list[EventRecord]:
    try:
        result = await conn.query(
            """
            SELECT event_id, kind, name, start_date, end_date, confidence, notes
            FROM events
            WHERE kind = ? AND start_date < ?
            ORDER BY start_date DESC
            """,
            (kind, before.isoformat()),
        )
    except D1QueryError as exc:
        raise _translate_storage(exc, context="past_events_of_kind query failed") from exc
    return [_row_to_event(row) for row in result.results]


# ---- signals ----


async def insert_signal(conn: D1Connection, signal: Signal) -> bool:
    metadata_json = json.dumps(signal.metadata, sort_keys=True) if signal.metadata else None
    try:
        changes = await conn.execute(
            """
            INSERT OR IGNORE INTO signals
                (item_id, computed_for, signal_name, value, metadata_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                signal.item_id,
                signal.computed_for.isoformat(),
                signal.signal_name,
                signal.value,
                metadata_json,
            ),
        )
    except D1QueryError as exc:
        raise _translate_integrity(
            exc,
            context=(
                f"signals insert failed for item_id={signal.item_id}, "
                f"signal_name={signal.signal_name}"
            ),
        ) from exc
    return changes > 0


async def insert_signals(conn: D1Connection, signals: Sequence[Signal]) -> int:
    if not signals:
        return 0
    statements = [
        D1Statement(
            sql=(
                "INSERT OR IGNORE INTO signals "
                "(item_id, computed_for, signal_name, value, metadata_json) "
                "VALUES (?, ?, ?, ?, ?)"
            ),
            params=(
                s.item_id,
                s.computed_for.isoformat(),
                s.signal_name,
                s.value,
                json.dumps(s.metadata, sort_keys=True) if s.metadata else None,
            ),
        )
        for s in signals
    ]
    try:
        results = await conn.batch(statements)
    except D1QueryError as exc:
        raise _translate_integrity(exc, context="insert_signals batch failed") from exc
    return sum(r.meta.changes for r in results)


def _row_to_signal(row: dict[str, Any]) -> Signal:
    metadata_raw = row["metadata_json"]
    metadata: dict[str, object] = json.loads(str(metadata_raw)) if metadata_raw else {}
    return Signal(
        item_id=int(row["item_id"]),
        computed_for=date.fromisoformat(str(row["computed_for"])),
        signal_name=row["signal_name"],
        value=(float(row["value"]) if row["value"] is not None else None),
        metadata=metadata,
    )


async def signals_for(conn: D1Connection, item_id: int, on: date) -> list[Signal]:
    try:
        result = await conn.query(
            """
            SELECT item_id, computed_for, signal_name, value, metadata_json
            FROM signals
            WHERE item_id = ? AND computed_for = ?
            ORDER BY signal_name
            """,
            (item_id, on.isoformat()),
        )
    except D1QueryError as exc:
        raise _translate_storage(
            exc, context=f"signals_for failed for item_id={item_id}, on={on.isoformat()}"
        ) from exc
    return [_row_to_signal(row) for row in result.results]


async def signals_for_items_on_date(
    conn: D1Connection,
    item_ids: Sequence[int],
    on: date,
) -> dict[int, list[Signal]]:
    """Bulk variant of :func:`signals_for` for the scoring runner.

    Returns a dict keyed by ``item_id`` with the per-item signal list
    (sorted by ``signal_name`` for stable display). Items in ``item_ids``
    with no signals on ``on`` map to ``[]``, matching the empty-list
    contract the other bulk reads use. Chunks the IN clause at
    :data:`_BULK_QUERY_CHUNK_SIZE` so a full universe doesn't blow past
    D1's per-statement parameter limit.
    """
    if not item_ids:
        return {}
    out: dict[int, list[Signal]] = {iid: [] for iid in item_ids}
    for chunk in _chunked(list(item_ids), _BULK_QUERY_CHUNK_SIZE):
        placeholders = ",".join("?" * len(chunk))
        sql = (
            "SELECT item_id, computed_for, signal_name, value, metadata_json "
            "FROM signals "
            f"WHERE computed_for = ? AND item_id IN ({placeholders}) "
            "ORDER BY item_id, signal_name"
        )
        params = (on.isoformat(), *chunk)
        try:
            result = await conn.query(sql, params)
        except D1QueryError as exc:
            raise _translate_storage(exc, context="signals_for_items_on_date failed") from exc
        for row in result.results:
            out.setdefault(int(row["item_id"]), []).append(_row_to_signal(row))
    return out


async def recent_signals(
    conn: D1Connection,
    item_id: int,
    *,
    days: int,
    as_of: date,
) -> list[Signal]:
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    start = as_of - timedelta(days=days - 1)
    try:
        result = await conn.query(
            """
            SELECT item_id, computed_for, signal_name, value, metadata_json
            FROM signals
            WHERE item_id = ?
              AND computed_for BETWEEN ? AND ?
            ORDER BY computed_for, signal_name
            """,
            (item_id, start.isoformat(), as_of.isoformat()),
        )
    except D1QueryError as exc:
        raise _translate_storage(
            exc, context=f"recent_signals failed for item_id={item_id}"
        ) from exc
    return [_row_to_signal(row) for row in result.results]


# ---- buy_scores ----


async def insert_score(conn: D1Connection, score: BuyScore) -> bool:
    try:
        changes = await conn.execute(
            """
            INSERT OR IGNORE INTO scores
                (item_id, computed_for, buy_score, components_json,
                 explanation, data_quality_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                score.item_id,
                score.computed_for.isoformat(),
                score.score,
                json.dumps(score.components, sort_keys=True),
                score.explanation,
                json.dumps(score.data_quality, sort_keys=True) if score.data_quality else None,
            ),
        )
    except D1QueryError as exc:
        raise _translate_integrity(
            exc, context=f"scores insert failed for item_id={score.item_id}"
        ) from exc
    return changes > 0


async def insert_scores(conn: D1Connection, scores: Sequence[BuyScore]) -> int:
    if not scores:
        return 0
    statements = [
        D1Statement(
            sql=(
                "INSERT OR IGNORE INTO scores "
                "(item_id, computed_for, buy_score, components_json, "
                "explanation, data_quality_json) "
                "VALUES (?, ?, ?, ?, ?, ?)"
            ),
            params=(
                s.item_id,
                s.computed_for.isoformat(),
                s.score,
                json.dumps(s.components, sort_keys=True),
                s.explanation,
                json.dumps(s.data_quality, sort_keys=True) if s.data_quality else None,
            ),
        )
        for s in scores
    ]
    try:
        results = await conn.batch(statements)
    except D1QueryError as exc:
        raise _translate_integrity(exc, context="insert_scores batch failed") from exc
    return sum(r.meta.changes for r in results)


async def scores_for(conn: D1Connection, on: date) -> list[BuyScore]:
    try:
        result = await conn.query(
            """
            SELECT item_id, computed_for, buy_score, components_json,
                   explanation, data_quality_json
            FROM scores
            WHERE computed_for = ?
            ORDER BY item_id
            """,
            (on.isoformat(),),
        )
    except D1QueryError as exc:
        raise _translate_storage(
            exc, context=f"scores_for query failed for on={on.isoformat()}"
        ) from exc
    return [_row_to_buy_score(row) for row in result.results]


async def latest_scores(conn: D1Connection, on: date, limit: int) -> list[BuyScore]:
    if limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")
    try:
        result = await conn.query(
            """
            SELECT item_id, computed_for, buy_score, components_json,
                   explanation, data_quality_json
            FROM scores
            WHERE computed_for = ?
            ORDER BY buy_score DESC, item_id ASC
            LIMIT ?
            """,
            (on.isoformat(), limit),
        )
    except D1QueryError as exc:
        raise _translate_storage(exc, context="latest_scores query failed") from exc
    return [_row_to_buy_score(row) for row in result.results]


async def latest_ingest_run_for_date(conn: D1Connection, on: date) -> tuple[str, RunStatus] | None:
    try:
        result = await conn.query(
            """
            SELECT run_id, status
            FROM runs
            WHERE kind = 'ingest'
              AND date(started_at) = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (on.isoformat(),),
        )
    except D1QueryError as exc:
        raise _translate_storage(exc, context="latest_ingest_run_for_date failed") from exc
    if not result.results:
        return None
    row = result.results[0]
    return (str(row["run_id"]), row["status"])


async def items_missing_observation_for_date(conn: D1Connection, on: date) -> list[str]:
    try:
        result = await conn.query(
            """
            SELECT i.market_hash
            FROM items i
            WHERE i.active = 1
              AND NOT EXISTS (
                  SELECT 1 FROM price_history p
                  WHERE p.item_id = i.item_id AND date(p.observed_at) = ?
              )
            ORDER BY i.market_hash
            """,
            (on.isoformat(),),
        )
    except D1QueryError as exc:
        raise _translate_storage(
            exc,
            context=f"items_missing_observation_for_date failed for on={on.isoformat()}",
        ) from exc
    return [str(row["market_hash"]) for row in result.results]


# ---- quarantine ----


async def quarantine_record(
    conn: D1Connection,
    *,
    run_id: str,
    source: str,
    item_hash: str | None,
    raw_payload: str,
    error_type: str,
    error_message: str,
) -> None:
    try:
        await conn.execute(
            """
            INSERT INTO quarantine
                (run_id, source, item_hash, raw_payload,
                 error_type, error_message, quarantined_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                source,
                item_hash,
                raw_payload,
                error_type,
                error_message,
                _utcnow_iso(),
            ),
        )
    except D1QueryError as exc:
        raise _translate_storage(exc, context="quarantine insert failed") from exc


# ---- runs ----


async def insert_run(conn: D1Connection, run: RunSummary) -> None:
    try:
        await conn.execute(
            """
            INSERT INTO runs (
                run_id, parent_run_id, kind, started_at, finished_at, status,
                items_ok, items_quarantined, items_failed, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run.run_id,
                run.parent_run_id,
                run.kind,
                run.started_at.isoformat(),
                run.finished_at.isoformat() if run.finished_at else None,
                run.status,
                run.items_ok,
                run.items_quarantined,
                run.items_failed,
                run.notes,
            ),
        )
    except D1QueryError as exc:
        raise _translate_integrity(
            exc, context=f"runs insert failed for run_id={run.run_id}"
        ) from exc


async def update_run(
    conn: D1Connection,
    run_id: str,
    *,
    status: RunStatus,
    items_ok: int = 0,
    items_quarantined: int = 0,
    items_failed: int = 0,
    notes: str | None = None,
) -> None:
    try:
        await conn.execute(
            """
            UPDATE runs SET
                finished_at = ?,
                status = ?,
                items_ok = ?,
                items_quarantined = ?,
                items_failed = ?,
                notes = COALESCE(?, notes)
            WHERE run_id = ?
            """,
            (
                _utcnow_iso(),
                status,
                items_ok,
                items_quarantined,
                items_failed,
                notes,
                run_id,
            ),
        )
    except D1QueryError as exc:
        raise _translate_storage(exc, context=f"runs update failed for run_id={run_id}") from exc
