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
from datetime import date

from dota_deals.models.domain import (
    BuyScore,
    Item,
    ListingPoint,
    PricePoint,
    RunStatus,
    RunSummary,
    Signal,
)

# ---- items ----


def upsert_item(conn: sqlite3.Connection, item: Item) -> int:
    """Insert ``item`` or update its mutable fields if ``market_hash`` exists.

    Returns the resolved ``item_id``.
    """
    raise NotImplementedError


def get_item_by_hash(conn: sqlite3.Connection, market_hash: str) -> Item | None:
    """Return the item with the given ``market_hash`` if present, else ``None``."""
    raise NotImplementedError


def active_items(conn: sqlite3.Connection) -> list[Item]:
    """Return all rows in ``items`` with ``active = 1``."""
    raise NotImplementedError


# ---- price_history ----


def insert_price_point(conn: sqlite3.Connection, point: PricePoint) -> bool:
    """Insert ``point`` into ``price_history`` if no row exists for its PK.

    Returns ``True`` if a new row was written, ``False`` if a row with the
    same ``(item_id, observed_at)`` already existed.
    """
    raise NotImplementedError


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
    raise NotImplementedError


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
    on every successful ingest.
    """
    raise NotImplementedError


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
    """Persist a failed-validation record for later inspection.

    All keyword arguments are required (the dataclass-style call ensures
    callers cannot accidentally omit ``error_type`` or ``raw_payload``).
    """
    raise NotImplementedError


# ---- runs ----


def insert_run(conn: sqlite3.Connection, run: RunSummary) -> None:
    """Persist a new run row with status ``running``.

    Use :func:`update_run` to mark completion.
    """
    raise NotImplementedError


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
    """Finalize ``run_id`` with status and counts."""
    raise NotImplementedError
