"""Typed repository functions, one section per table.

Every public function takes a :class:`sqlite3.Connection` as its first argument
and either returns a typed domain model or ``None``. Write functions are
idempotent at the DB level (``INSERT OR IGNORE`` on PK-protected tables, upsert
on ``latest_observation``).

Exceptions raised by this module are :class:`dota_deals.storage.db.StorageError`
or subclasses — never raw ``sqlite3.*`` types.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, timedelta

from dota_deals.models.domain import (
    BuyScore,
    Item,
    ItemCategory,
    ListingPoint,
    PricePoint,
    RunStatus,
    RunSummary,
    Signal,
)
from dota_deals.models.events import EventRecord
from dota_deals.storage.db import IntegrityViolation, StorageError


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        item_id=row["item_id"],
        market_hash=row["market_hash"],
        name=row["name"],
        category=row["category"],
        hero=row["hero"],
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        last_seen_at=(datetime.fromisoformat(row["last_seen_at"]) if row["last_seen_at"] else None),
        active=bool(row["active"]),
        consecutive_ingest_4xx=int(row["consecutive_ingest_4xx"]),
    )


_ITEM_COLUMNS = (
    "item_id, market_hash, name, category, hero, "
    "first_seen_at, last_seen_at, active, consecutive_ingest_4xx"
)


# ---- items ----


def upsert_item(conn: sqlite3.Connection, item: Item) -> int:
    """Insert ``item`` or update its mutable fields if ``market_hash`` exists.

    Universe-refresh semantics:

    * ``name``, ``category``, ``hero``, ``last_seen_at`` are overwritten with
      the supplied values (Steam's view wins).
    * ``active`` is forced to ``1`` — a sighting from the universe stage
      reactivates a previously deactivated item.
    * ``consecutive_ingest_4xx`` is reset to ``0`` — the same fresh-start
      principle: if Steam still serves the item, ingest gets to try again.
    * ``first_seen_at`` is preserved from the original row.

    Returns the resolved ``item_id``.
    """
    try:
        conn.execute(
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
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise IntegrityViolation(
            f"items upsert failed for market_hash={item.market_hash!r}: {e}"
        ) from e
    except sqlite3.Error as e:
        raise StorageError(f"items upsert failed for market_hash={item.market_hash!r}: {e}") from e
    # `cursor.lastrowid` is unreliable for ON CONFLICT updates; always query.
    row = conn.execute(
        "SELECT item_id FROM items WHERE market_hash = ?", (item.market_hash,)
    ).fetchone()
    if row is None:
        raise StorageError(f"upsert_item: item_id lookup failed for {item.market_hash!r}")
    return int(row["item_id"])


def get_item_by_hash(conn: sqlite3.Connection, market_hash: str) -> Item | None:
    """Return the item with the given ``market_hash`` if present, else ``None``."""
    try:
        row = conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM items WHERE market_hash = ?",
            (market_hash,),
        ).fetchone()
    except sqlite3.Error as e:
        raise StorageError(f"lookup failed for market_hash={market_hash!r}: {e}") from e
    return _row_to_item(row) if row is not None else None


def get_item_by_id(conn: sqlite3.Connection, item_id: int) -> Item | None:
    """Return the item with the given ``item_id`` if present, else ``None``."""
    try:
        row = conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM items WHERE item_id = ?",
            (item_id,),
        ).fetchone()
    except sqlite3.Error as e:
        raise StorageError(f"lookup failed for item_id={item_id}: {e}") from e
    return _row_to_item(row) if row is not None else None


def active_items(conn: sqlite3.Connection) -> list[Item]:
    """Return all rows in ``items`` with ``active = 1``, ordered by ``item_id``."""
    try:
        rows = conn.execute(
            f"SELECT {_ITEM_COLUMNS} FROM items WHERE active = 1 ORDER BY item_id"
        ).fetchall()
    except sqlite3.Error as e:
        raise StorageError(f"active_items query failed: {e}") from e
    return [_row_to_item(row) for row in rows]


def active_items_in_category(
    conn: sqlite3.Connection,
    category: ItemCategory,
    *,
    exclude_item_id: int | None = None,
) -> list[Item]:
    """Return active items in ``category``; optionally drop ``exclude_item_id``.

    The optional exclusion is used by Signal 4 (comparables) so an item isn't
    in its own peer set.
    """
    try:
        if exclude_item_id is None:
            rows = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM items "
                "WHERE active = 1 AND category = ? ORDER BY item_id",
                (category,),
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {_ITEM_COLUMNS} FROM items "
                "WHERE active = 1 AND category = ? AND item_id != ? "
                "ORDER BY item_id",
                (category, exclude_item_id),
            ).fetchall()
    except sqlite3.Error as e:
        raise StorageError(
            f"active_items_in_category query failed for category={category!r}: {e}"
        ) from e
    return [_row_to_item(row) for row in rows]


def increment_ingest_strikes(conn: sqlite3.Connection, item_id: int) -> int:
    """Increment ``items.consecutive_ingest_4xx`` for ``item_id`` by 1.

    Returns the new strike count. Raises :class:`StorageError` if the item
    doesn't exist (which would indicate a runner bug — the runner should
    only call this for items it just looked up).
    """
    try:
        cursor = conn.execute(
            """
            UPDATE items
            SET consecutive_ingest_4xx = consecutive_ingest_4xx + 1
            WHERE item_id = ?
            RETURNING consecutive_ingest_4xx
            """,
            (item_id,),
        )
        row = cursor.fetchone()
        conn.commit()
    except sqlite3.Error as e:
        raise StorageError(f"increment_ingest_strikes failed for item_id={item_id}: {e}") from e
    if row is None:
        raise StorageError(f"item_id={item_id} not found in items")
    return int(row["consecutive_ingest_4xx"])


def reset_ingest_strikes(conn: sqlite3.Connection, item_id: int) -> None:
    """Reset ``items.consecutive_ingest_4xx`` to ``0`` for ``item_id``.

    Idempotent — calling on an item already at zero is a no-op.
    """
    try:
        conn.execute(
            "UPDATE items SET consecutive_ingest_4xx = 0 WHERE item_id = ?",
            (item_id,),
        )
        conn.commit()
    except sqlite3.Error as e:
        raise StorageError(f"reset_ingest_strikes failed for item_id={item_id}: {e}") from e


def set_item_active(conn: sqlite3.Connection, item_id: int, *, active: bool) -> None:
    """Flip ``items.active`` for ``item_id``.

    Used by ingest to deactivate items that hit the strike threshold; universe
    refresh handles reactivation via :func:`upsert_item`.
    """
    try:
        conn.execute(
            "UPDATE items SET active = ? WHERE item_id = ?",
            (1 if active else 0, item_id),
        )
        conn.commit()
    except sqlite3.Error as e:
        raise StorageError(f"set_item_active failed for item_id={item_id}: {e}") from e


# ---- price_history ----


def insert_price_point(conn: sqlite3.Connection, point: PricePoint) -> bool:
    """Insert ``point`` into ``price_history`` if no row exists for its PK.

    Returns ``True`` if a new row was written, ``False`` if a row with the
    same ``(item_id, observed_at)`` already existed.
    """
    try:
        cursor = conn.execute(
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
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise IntegrityViolation(
            f"price_history insert failed for item_id={point.item_id}: {e}"
        ) from e
    except sqlite3.Error as e:
        raise StorageError(f"price_history insert failed: {e}") from e
    return cursor.rowcount > 0


def recent_prices(
    conn: sqlite3.Connection,
    item_id: int,
    days: int,
    *,
    as_of: date,
) -> list[PricePoint]:
    """Return ``price_history`` rows for ``item_id`` over a UTC-date window.

    Window is ``[as_of - days + 1, as_of]`` inclusive on both ends — i.e. the
    last ``days`` UTC days ending at ``as_of``. Sorted oldest-first.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    start_date = as_of - timedelta(days=days - 1)
    try:
        rows = conn.execute(
            """
            SELECT item_id, observed_at, lowest_cents, median_cents, volume_24h
            FROM price_history
            WHERE item_id = ?
              AND date(observed_at) BETWEEN ? AND ?
            ORDER BY observed_at
            """,
            (item_id, start_date.isoformat(), as_of.isoformat()),
        ).fetchall()
    except sqlite3.Error as e:
        raise StorageError(f"recent_prices failed for item_id={item_id}: {e}") from e
    return [
        PricePoint(
            item_id=row["item_id"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            lowest_cents=int(row["lowest_cents"]),
            median_cents=int(row["median_cents"]) if row["median_cents"] is not None else None,
            volume_24h=int(row["volume_24h"]) if row["volume_24h"] is not None else None,
        )
        for row in rows
    ]


def daily_prices(
    conn: sqlite3.Connection,
    item_id: int,
    days: int,
    *,
    as_of: date,
) -> list[tuple[date, int]]:
    """Return per-day ``(utc_date, median_lowest_cents)`` for ``item_id``.

    Window is ``[as_of - days + 1, as_of]`` inclusive on both ends. Days with
    no observations are simply absent from the result. Sorted oldest-first.

    Queries the ``v_daily_price`` view, which requires the ``MEDIAN``
    aggregate registered by :func:`dota_deals.storage.db.connect`.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    start_date = as_of - timedelta(days=days - 1)
    try:
        rows = conn.execute(
            """
            SELECT utc_date, lowest_cents
            FROM v_daily_price
            WHERE item_id = ?
              AND utc_date BETWEEN ? AND ?
            ORDER BY utc_date
            """,
            (item_id, start_date.isoformat(), as_of.isoformat()),
        ).fetchall()
    except sqlite3.Error as e:
        raise StorageError(f"daily_prices failed for item_id={item_id}: {e}") from e
    return [(date.fromisoformat(row["utc_date"]), int(row["lowest_cents"])) for row in rows]


# ---- listing_history ----


def insert_listing_point(conn: sqlite3.Connection, point: ListingPoint) -> bool:
    """Insert ``point`` into ``listing_history``; idempotent on PK collision.

    Returns ``True`` if a new row was written, ``False`` if a duplicate.
    """
    try:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO listing_history (item_id, observed_at, listings_count)
            VALUES (?, ?, ?)
            """,
            (point.item_id, point.observed_at.isoformat(), point.listings_count),
        )
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise IntegrityViolation(
            f"listing_history insert failed for item_id={point.item_id}: {e}"
        ) from e
    except sqlite3.Error as e:
        raise StorageError(f"listing_history insert failed: {e}") from e
    return cursor.rowcount > 0


def recent_listings(
    conn: sqlite3.Connection,
    item_id: int,
    days: int,
    *,
    as_of: date,
) -> list[ListingPoint]:
    """Return ``listing_history`` rows for ``item_id`` over a UTC-date window.

    Window is ``[as_of - days + 1, as_of]`` inclusive. Sorted oldest-first.
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    start_date = as_of - timedelta(days=days - 1)
    try:
        rows = conn.execute(
            """
            SELECT item_id, observed_at, listings_count
            FROM listing_history
            WHERE item_id = ?
              AND date(observed_at) BETWEEN ? AND ?
            ORDER BY observed_at
            """,
            (item_id, start_date.isoformat(), as_of.isoformat()),
        ).fetchall()
    except sqlite3.Error as e:
        raise StorageError(f"recent_listings failed for item_id={item_id}: {e}") from e
    return [
        ListingPoint(
            item_id=row["item_id"],
            observed_at=datetime.fromisoformat(row["observed_at"]),
            listings_count=int(row["listings_count"]),
        )
        for row in rows
    ]


# ---- latest_observation ----


def upsert_latest_observation(
    conn: sqlite3.Connection, point: PricePoint, listings_count: int | None
) -> None:
    """Upsert the ``latest_observation`` row for ``point.item_id``.

    Called alongside :func:`insert_price_point` / :func:`insert_listing_point`
    on every successful ingest. Newer observed_at values overwrite older ones;
    older observed_at values (e.g., from a backfill re-run) leave the cache
    alone so the "latest" invariant holds.
    """
    try:
        conn.execute(
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
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise IntegrityViolation(
            f"latest_observation upsert failed for item_id={point.item_id}: {e}"
        ) from e
    except sqlite3.Error as e:
        raise StorageError(f"latest_observation upsert failed: {e}") from e


# ---- events ----


def _row_to_event(row: sqlite3.Row) -> EventRecord:
    return EventRecord(
        event_id=int(row["event_id"]),
        kind=row["kind"],
        name=row["name"],
        start_date=date.fromisoformat(row["start_date"]),
        end_date=date.fromisoformat(row["end_date"]) if row["end_date"] else None,
        confidence=row["confidence"],
        notes=row["notes"],
    )


def insert_event(conn: sqlite3.Connection, event: EventRecord) -> int:
    """Insert ``event`` and return the resolved ``event_id``.

    The events table is hand-curated; this is the entry point used by seed
    scripts and tests. ``event.event_id`` is ignored on input.
    """
    try:
        cursor = conn.execute(
            """
            INSERT INTO events (kind, name, start_date, end_date, confidence, notes)
            VALUES (?, ?, ?, ?, ?, ?)
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
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise IntegrityViolation(f"events insert failed: {e}") from e
    except sqlite3.Error as e:
        raise StorageError(f"events insert failed: {e}") from e
    if cursor.lastrowid is None:
        raise StorageError("insert_event returned without a lastrowid")
    return int(cursor.lastrowid)


def next_event_within(
    conn: sqlite3.Connection, as_of: date, *, days_window: int
) -> EventRecord | None:
    """Return the next event with ``start_date`` in ``[as_of, as_of + days_window]``,
    or ``None`` if no such event exists.

    "Next" = earliest ``start_date``. The window is inclusive on both ends.
    """
    if days_window < 0:
        raise ValueError(f"days_window must be >= 0, got {days_window}")
    upper = as_of + timedelta(days=days_window)
    try:
        row = conn.execute(
            """
            SELECT event_id, kind, name, start_date, end_date, confidence, notes
            FROM events
            WHERE start_date BETWEEN ? AND ?
            ORDER BY start_date
            LIMIT 1
            """,
            (as_of.isoformat(), upper.isoformat()),
        ).fetchone()
    except sqlite3.Error as e:
        raise StorageError(f"next_event_within query failed: {e}") from e
    return _row_to_event(row) if row is not None else None


def past_events_of_kind(conn: sqlite3.Connection, kind: str, *, before: date) -> list[EventRecord]:
    """Return events of ``kind`` whose ``start_date`` is strictly before ``before``.

    Sorted most-recent-first so callers can iterate prior cycles in temporal
    order.
    """
    try:
        rows = conn.execute(
            """
            SELECT event_id, kind, name, start_date, end_date, confidence, notes
            FROM events
            WHERE kind = ? AND start_date < ?
            ORDER BY start_date DESC
            """,
            (kind, before.isoformat()),
        ).fetchall()
    except sqlite3.Error as e:
        raise StorageError(f"past_events_of_kind query failed: {e}") from e
    return [_row_to_event(row) for row in rows]


# ---- signals ----


def insert_signal(conn: sqlite3.Connection, signal: Signal) -> bool:
    """Insert a signal row. Idempotent on ``(item_id, computed_for, signal_name)``
    via ``INSERT OR IGNORE``.

    Returns ``True`` if a new row was written, ``False`` on PK collision.
    """
    metadata_json: str | None
    metadata_json = json.dumps(signal.metadata, sort_keys=True) if signal.metadata else None
    try:
        cursor = conn.execute(
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
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise IntegrityViolation(
            f"signals insert failed for item_id={signal.item_id}, "
            f"signal_name={signal.signal_name}: {e}"
        ) from e
    except sqlite3.Error as e:
        raise StorageError(f"signals insert failed: {e}") from e
    return cursor.rowcount > 0


def signals_for(conn: sqlite3.Connection, item_id: int, on: date) -> list[Signal]:
    """Return every signal row computed for ``item_id`` on date ``on``.

    Sorted by ``signal_name`` for stable display.
    """
    try:
        rows = conn.execute(
            """
            SELECT item_id, computed_for, signal_name, value, metadata_json
            FROM signals
            WHERE item_id = ? AND computed_for = ?
            ORDER BY signal_name
            """,
            (item_id, on.isoformat()),
        ).fetchall()
    except sqlite3.Error as e:
        raise StorageError(
            f"signals_for failed for item_id={item_id}, on={on.isoformat()}: {e}"
        ) from e
    return [
        Signal(
            item_id=row["item_id"],
            computed_for=date.fromisoformat(row["computed_for"]),
            signal_name=row["signal_name"],
            value=row["value"],
            metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        )
        for row in rows
    ]


def recent_signals(
    conn: sqlite3.Connection,
    item_id: int,
    *,
    days: int,
    as_of: date,
) -> list[Signal]:
    """Return all signal rows for ``item_id`` over ``[as_of - days + 1, as_of]``.

    Sorted by ``(computed_for, signal_name)`` so per-signal series are
    reconstructable by a simple group-by in the caller (the publish layer
    needs this for ItemDetail's signal_series field).
    """
    if days < 1:
        raise ValueError(f"days must be >= 1, got {days}")
    start_date = as_of - timedelta(days=days - 1)
    try:
        rows = conn.execute(
            """
            SELECT item_id, computed_for, signal_name, value, metadata_json
            FROM signals
            WHERE item_id = ?
              AND computed_for BETWEEN ? AND ?
            ORDER BY computed_for, signal_name
            """,
            (item_id, start_date.isoformat(), as_of.isoformat()),
        ).fetchall()
    except sqlite3.Error as e:
        raise StorageError(f"recent_signals failed for item_id={item_id}: {e}") from e
    return [
        Signal(
            item_id=row["item_id"],
            computed_for=date.fromisoformat(row["computed_for"]),
            signal_name=row["signal_name"],
            value=row["value"],
            metadata=json.loads(row["metadata_json"]) if row["metadata_json"] else {},
        )
        for row in rows
    ]


# ---- buy_scores ----


def _row_to_buy_score(row: sqlite3.Row) -> BuyScore:
    return BuyScore(
        item_id=int(row["item_id"]),
        computed_for=date.fromisoformat(row["computed_for"]),
        score=float(row["buy_score"]),
        components=json.loads(row["components_json"]),
        explanation=row["explanation"],
        data_quality=(json.loads(row["data_quality_json"]) if row["data_quality_json"] else {}),
    )


def insert_score(conn: sqlite3.Connection, score: BuyScore) -> bool:
    """Insert a buy score row. Idempotent via PK ``(item_id, computed_for)``.

    Returns ``True`` if a new row was written, ``False`` on collision.
    """
    try:
        cursor = conn.execute(
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
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise IntegrityViolation(f"scores insert failed for item_id={score.item_id}: {e}") from e
    except sqlite3.Error as e:
        raise StorageError(f"scores insert failed: {e}") from e
    return cursor.rowcount > 0


def scores_for(conn: sqlite3.Connection, on: date) -> list[BuyScore]:
    """Return every score row for the given UTC date, ordered by item_id.

    Used by the notifier to read back what was written, and by debug
    queries. Apply :func:`dota_deals.scoring.buy_score.rank_top_n` to sort
    by score.
    """
    try:
        rows = conn.execute(
            """
            SELECT item_id, computed_for, buy_score, components_json,
                   explanation, data_quality_json
            FROM scores
            WHERE computed_for = ?
            ORDER BY item_id
            """,
            (on.isoformat(),),
        ).fetchall()
    except sqlite3.Error as e:
        raise StorageError(f"scores_for query failed for on={on.isoformat()}: {e}") from e
    return [_row_to_buy_score(row) for row in rows]


def latest_scores(conn: sqlite3.Connection, on: date, limit: int) -> list[BuyScore]:
    """Return the top ``limit`` scores for ``on``, ranked by ``buy_score`` desc.

    Tie-breaker is ``item_id`` ascending for deterministic ordering.
    """
    if limit < 0:
        raise ValueError(f"limit must be >= 0, got {limit}")
    try:
        rows = conn.execute(
            """
            SELECT item_id, computed_for, buy_score, components_json,
                   explanation, data_quality_json
            FROM scores
            WHERE computed_for = ?
            ORDER BY buy_score DESC, item_id ASC
            LIMIT ?
            """,
            (on.isoformat(), limit),
        ).fetchall()
    except sqlite3.Error as e:
        raise StorageError(f"latest_scores query failed: {e}") from e
    return [_row_to_buy_score(row) for row in rows]


def latest_ingest_run_for_date(conn: sqlite3.Connection, on: date) -> tuple[str, RunStatus] | None:
    """Return ``(run_id, status)`` for the most recent ingest run whose
    ``started_at`` falls on the given UTC date, or ``None`` if no ingest
    has run on that date.

    Used by the scoring stage to propagate ingest data-quality into the
    per-score ``data_quality_json``.
    """
    try:
        row = conn.execute(
            """
            SELECT run_id, status
            FROM runs
            WHERE kind = 'ingest'
              AND date(started_at) = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (on.isoformat(),),
        ).fetchone()
    except sqlite3.Error as e:
        raise StorageError(f"latest_ingest_run_for_date failed: {e}") from e
    if row is None:
        return None
    return (str(row["run_id"]), row["status"])


def items_missing_observation_for_date(conn: sqlite3.Connection, on: date) -> list[str]:
    """Return ``market_hash`` for every active item that has no
    ``price_history`` row on ``on``. Used to surface "what ingest missed".
    """
    try:
        rows = conn.execute(
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
        ).fetchall()
    except sqlite3.Error as e:
        raise StorageError(
            f"items_missing_observation_for_date failed for on={on.isoformat()}: {e}"
        ) from e
    return [str(row["market_hash"]) for row in rows]


# ---- quarantine ----


def quarantine_record(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    source: str,
    item_hash: str | None,
    raw_payload: str,
    error_type: str,
    error_message: str,
) -> None:
    """Persist a failed-validation record for later inspection."""
    try:
        conn.execute(
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
        conn.commit()
    except sqlite3.Error as e:
        raise StorageError(f"quarantine insert failed: {e}") from e


# ---- runs ----


def insert_run(conn: sqlite3.Connection, run: RunSummary) -> None:
    """Persist a new run row.

    Typical pattern: insert with ``status='running'`` at the top of a stage,
    then call :func:`update_run` to mark completion.
    """
    try:
        conn.execute(
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
        conn.commit()
    except sqlite3.IntegrityError as e:
        raise IntegrityViolation(f"runs insert failed for run_id={run.run_id}: {e}") from e
    except sqlite3.Error as e:
        raise StorageError(f"runs insert failed: {e}") from e


def update_run(
    conn: sqlite3.Connection,
    run_id: str,
    *,
    status: RunStatus,
    items_ok: int = 0,
    items_quarantined: int = 0,
    items_failed: int = 0,
    notes: str | None = None,
) -> None:
    """Finalize ``run_id`` with status, counts, and ``finished_at = now`` (UTC)."""
    try:
        conn.execute(
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
        conn.commit()
    except sqlite3.Error as e:
        raise StorageError(f"runs update failed for run_id={run_id}: {e}") from e
