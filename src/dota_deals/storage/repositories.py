"""Typed repository functions, one section per table.

Every public function takes a :class:`sqlite3.Connection` as its first argument
and either returns a typed domain model or ``None``. Write functions are
idempotent at the DB level (``INSERT OR IGNORE`` on PK-protected tables, upsert
on ``latest_observation``).

Exceptions raised by this module are :class:`dota_deals.storage.db.StorageError`
or subclasses — never raw ``sqlite3.*`` types.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from dota_deals.models.domain import (
    BuyScore,
    Item,
    ListingPoint,
    PricePoint,
    RunStatus,
    RunSummary,
    Signal,
)
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
    )


# ---- items ----


def upsert_item(conn: sqlite3.Connection, item: Item) -> int:
    """Insert ``item`` or update its mutable fields if ``market_hash`` exists.

    Returns the resolved ``item_id``.
    """
    raise NotImplementedError


def get_item_by_hash(conn: sqlite3.Connection, market_hash: str) -> Item | None:
    """Return the item with the given ``market_hash`` if present, else ``None``."""
    try:
        row = conn.execute(
            """
            SELECT item_id, market_hash, name, category, hero,
                   first_seen_at, last_seen_at, active
            FROM items
            WHERE market_hash = ?
            """,
            (market_hash,),
        ).fetchone()
    except sqlite3.Error as e:
        raise StorageError(f"lookup failed for market_hash={market_hash!r}: {e}") from e
    return _row_to_item(row) if row is not None else None


def active_items(conn: sqlite3.Connection) -> list[Item]:
    """Return all rows in ``items`` with ``active = 1``."""
    raise NotImplementedError


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


def recent_prices(conn: sqlite3.Connection, item_id: int, days: int) -> list[PricePoint]:
    """Return all ``price_history`` rows for ``item_id`` within the last
    ``days`` days, oldest first.
    """
    raise NotImplementedError


def daily_prices(conn: sqlite3.Connection, item_id: int, days: int) -> list[tuple[date, int]]:
    """Return ``(utc_date, median_lowest_cents)`` for each of the last ``days``
    UTC days that has at least one observation for ``item_id``.

    This is the canonical "daily price series" consumed by Signal 1.
    """
    raise NotImplementedError


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


def recent_listings(conn: sqlite3.Connection, item_id: int, days: int) -> list[ListingPoint]:
    """Return ``listing_history`` rows for ``item_id`` within the last ``days``
    days, oldest first.
    """
    raise NotImplementedError


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


# ---- signals ----


def insert_signal(conn: sqlite3.Connection, signal: Signal) -> bool:
    """Insert (or replace) a signal row. Idempotent on PK collision."""
    raise NotImplementedError


def signals_for(conn: sqlite3.Connection, item_id: int, on: date) -> list[Signal]:
    """Return every signal computed for ``item_id`` on date ``on``."""
    raise NotImplementedError


# ---- buy_scores (derived; not stored separately yet) ----


def latest_scores(conn: sqlite3.Connection, on: date, limit: int) -> list[BuyScore]:
    """Return the top ``limit`` :class:`BuyScore` values for ``on``."""
    raise NotImplementedError


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
