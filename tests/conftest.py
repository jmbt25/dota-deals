"""Shared pytest fixtures for dota-deals tests."""

from __future__ import annotations

import sqlite3
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dota_deals.config import Settings
from dota_deals.storage.db import bootstrap_schema, connect


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Return a path to a fresh SQLite database file inside ``tmp_path``.

    The file does not exist when the fixture yields; it's created by the code
    under test via :func:`dota_deals.storage.db.connect`.
    """
    return tmp_path / "test.db"


@pytest.fixture()
def db_conn(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    """Yield an open SQLite connection to an isolated tmp database.

    Uses :func:`dota_deals.storage.db.connect` so the MEDIAN aggregate (and
    any other connection-level setup) is in place; then bootstraps the
    schema. The connection is closed when the fixture tears down.
    """
    conn = connect(db_path)
    bootstrap_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def settings(db_path: Path) -> Settings:
    """Test-flavored Settings.

    Tight timeouts and a tiny 429 cool-down keep retries from blocking tests
    that don't override sleep behavior explicitly.
    """
    return Settings(
        db_path=db_path,
        steam_concurrency=2,
        request_timeout_s=2.0,
        cooldown_429_s=0.01,
        ingest_cadence_hours=8,
        steam_currency_id=1,
        steam_country="US",
        log_format="console",
    )


def insert_test_item(
    conn: sqlite3.Connection,
    *,
    market_hash: str,
    name: str | None = None,
    category: str = "arcana",
    hero: str | None = None,
    first_seen_at: datetime | None = None,
) -> int:
    """Insert a row into ``items`` and return the resolved ``item_id``.

    Test helper — most ingest tests need a couple of pre-populated items.
    """
    seen_at = (first_seen_at or datetime.now(UTC)).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO items (market_hash, name, category, hero, first_seen_at, active)
        VALUES (?, ?, ?, ?, ?, 1)
        """,
        (market_hash, name or market_hash, category, hero, seen_at),
    )
    conn.commit()
    return int(cursor.lastrowid or 0)
