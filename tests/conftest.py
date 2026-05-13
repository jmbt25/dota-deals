"""Shared pytest fixtures for dota-deals tests.

Storage is async D1 throughout (Phase 9c cutover complete). The two
fixtures here — ``db_conn`` and ``settings`` — plus the
``insert_test_item`` helper are the test-side equivalents of "open a
connection and seed an item"; everything else builds on them.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from dota_deals.config import Settings
from dota_deals.logging import configure_logging
from dota_deals.storage.db import D1Connection, connect
from tests._d1_fake import D1FakeClient


@pytest.fixture(autouse=True, scope="session")
def _configure_test_logging() -> None:
    """Pin structlog config for the test session.

    Without this, structlog uses its permissive default (DEBUG level,
    factory writing to stdout). That's fine for production where the
    CLI calls :func:`configure_logging` at startup — but in tests it
    pollutes ``capsys.readouterr().out`` with debug lines (the
    ``d1_connection_closed`` budget summary in particular), breaking
    any test that asserts on ``typer.echo`` output. Session-scoped so
    we configure once per test run.
    """
    configure_logging("test-session", "json")


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Return a path inside ``tmp_path``.

    The path is unused by D1-backed code (storage lives at Cloudflare),
    but stays here because :class:`Settings` still requires a
    ``db_path`` for one or two legacy fields (R2 sync), and many tests
    construct ``Settings`` via the fixture below.
    """
    return tmp_path / "test.db"


@pytest.fixture()
async def db_conn(
    tmp_path: Path,
) -> AsyncIterator[tuple[D1Connection, D1FakeClient]]:
    """Yield ``(D1Connection, D1FakeClient)`` backed by an in-memory schema.

    The fake is shared between the yielded :class:`D1Connection` (which
    tests use to pre-populate and assert) and any production-code
    ``connect`` call that receives the same ``backend=fake`` (the
    runner under test opens its own connection wrapping the fake —
    both see the same in-memory store).

    Tests unpack as::

        async def test_x(db_conn, settings):
            conn, fake = db_conn
            await insert_test_item(conn, market_hash="X")
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
        async with connect(settings, backend=fake) as conn:
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


async def insert_test_item(
    conn: D1Connection,
    *,
    market_hash: str,
    name: str | None = None,
    category: str = "arcana",
    hero: str | None = None,
    first_seen_at: datetime | None = None,
) -> int:
    """Insert a row into ``items`` and return the resolved ``item_id``.

    Async helper used by every test that needs a pre-existing item.
    Uses D1's ``RETURNING`` clause so the insert and id-lookup happen
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
