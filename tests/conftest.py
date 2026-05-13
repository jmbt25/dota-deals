"""Shared pytest fixtures for dota-deals tests.

During the D1 cutover (Phase 9c), the sync fixtures (``db_path``,
``db_conn``) and the async fixture (``db_conn_async``) coexist. Each
module-under-cutover swaps its tests to the async fixture as it lands.
Phase 9c-iv will delete the sync fixtures once the last sync test is
gone.
"""

from __future__ import annotations

import sqlite3
from collections.abc import AsyncIterator, Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dota_deals.config import Settings
from dota_deals.storage.db import bootstrap_schema, connect
from dota_deals.storage.db_async import D1Connection
from dota_deals.storage.db_async import connect as connect_async
from tests._d1_fake import D1FakeClient


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
async def db_conn_async(
    tmp_path: Path,
) -> AsyncIterator[tuple[D1Connection, D1FakeClient]]:
    """Yield ``(D1Connection, D1FakeClient)`` backed by an in-memory schema.

    The fake is shared between the yielded ``D1Connection`` (which tests
    use to pre-populate and assert) and any production-code ``connect``
    call that receives the same ``backend=fake`` (the runner under test
    opens its own ``D1Connection`` wrapping the fake — both connections
    see the same data because the fake's in-memory SQLite is the
    underlying store).

    Tests unpack as::

        async def test_x(db_conn_async, settings):
            conn, fake = db_conn_async
            await insert_test_item_async(conn, market_hash="X")
            await run_ingestion(..., backend=fake)
            rows = (await conn.query("SELECT ...")).results
    """
    settings = Settings(
        db_path=tmp_path / "x.db",
        cloudflare_account_id="test-acct",
        cloudflare_d1_database_id="test-db",
        cloudflare_d1_api_token="test-token",
    )
    async with D1FakeClient() as fake:
        async with connect_async(settings, backend=fake) as conn:
            yield conn, fake


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

    Sync helper — used by tests still on the sync ``db_conn`` fixture.
    The async equivalent is :func:`insert_test_item_async`.
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


async def insert_test_item_async(
    conn: D1Connection,
    *,
    market_hash: str,
    name: str | None = None,
    category: str = "arcana",
    hero: str | None = None,
    first_seen_at: datetime | None = None,
) -> int:
    """Async equivalent of :func:`insert_test_item`.

    Returns the resolved ``item_id``. RETURNING is supported by both
    D1 and the in-memory fake, so the insert and the id-lookup happen
    in one round-trip.
    """
    seen_at = (first_seen_at or datetime.now(UTC)).isoformat()
    result = await conn.query(
        """
        INSERT INTO items (market_hash, name, category, hero, first_seen_at, active)
        VALUES (?, ?, ?, ?, ?, 1)
        RETURNING item_id
        """,
        (market_hash, name or market_hash, category, hero, seen_at),
    )
    return int(result.results[0]["item_id"])
