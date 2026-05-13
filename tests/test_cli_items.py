"""Tests for ``dota-deals items list-active``.

Output shape is the contract the GHA workflow consumes (``... | items.txt``
piped into ``dota-deals ingest --items items.txt``), so the test pins it: one
``market_hash_name`` per line, no header, sorted by ``item_id``, inactive
items excluded.

Phase 9c-iv: the CLI command is async-wrapped. The test calls the async
helper :func:`_items_list_active_async` directly with a monkeypatched
``connect`` (sharing the fixture's :class:`D1FakeClient`) so we don't
need typer's CliRunner — same pattern as test_publish_cli.py.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

import dota_deals.cli.main as cli_main
from dota_deals.cli.main import _items_list_active_async
from dota_deals.config import Settings
from dota_deals.storage.db import D1Connection
from tests._d1_fake import D1FakeClient
from tests.conftest import insert_test_item


@asynccontextmanager
async def _shared_connect(fake: D1FakeClient, settings: Settings) -> AsyncIterator[D1Connection]:
    """Build a fresh D1Connection wrapping the test's fake."""
    cli_conn = D1Connection(fake, budget_warn=settings.d1_daily_budget_warn)
    try:
        yield cli_conn
    finally:
        cli_conn.log_budget_summary()


def _patch_connect(monkeypatch: pytest.MonkeyPatch, fake: D1FakeClient) -> None:
    """Make :func:`dota_deals.cli.main.connect` reuse ``fake``."""

    @asynccontextmanager
    async def fake_connect(
        settings: Settings, *, backend: object = None
    ) -> AsyncIterator[D1Connection]:
        async with _shared_connect(fake, settings) as conn:
            yield conn

    monkeypatch.setattr(cli_main, "connect", fake_connect)


@pytest.fixture()
def _test_settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "x.db",
        cloudflare_account_id="test",
        cloudflare_d1_database_id="test",
        cloudflare_d1_api_token="test",
    )


@pytest.mark.asyncio
async def test_list_active_outputs_one_hash_per_line(
    db_conn: tuple[D1Connection, D1FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _test_settings: Settings,
) -> None:
    conn, fake = db_conn
    await insert_test_item(conn, market_hash="Alpha")
    await insert_test_item(conn, market_hash="Beta")
    await insert_test_item(conn, market_hash="Gamma")
    _patch_connect(monkeypatch, fake)

    await _items_list_active_async(_test_settings)

    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["Alpha", "Beta", "Gamma"]
    # No header, no leading/trailing whitespace on any line.
    for line in captured.out.splitlines():
        assert line == line.strip()


@pytest.mark.asyncio
async def test_list_active_excludes_inactive_items(
    db_conn: tuple[D1Connection, D1FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _test_settings: Settings,
) -> None:
    conn, fake = db_conn
    await insert_test_item(conn, market_hash="Active1")
    await insert_test_item(conn, market_hash="Deactivated")
    await insert_test_item(conn, market_hash="Active2")
    await conn.execute("UPDATE items SET active = 0 WHERE market_hash = ?", ("Deactivated",))
    _patch_connect(monkeypatch, fake)

    await _items_list_active_async(_test_settings)

    captured = capsys.readouterr()
    assert captured.out.splitlines() == ["Active1", "Active2"]


@pytest.mark.asyncio
async def test_list_active_empty_when_no_items(
    db_conn: tuple[D1Connection, D1FakeClient],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    _test_settings: Settings,
) -> None:
    """No items → empty output. The workflow's ``ingest`` step will then
    fail with its own clear error ("no items found in <file>"), which is
    the right escalation path.
    """
    _, fake = db_conn
    _patch_connect(monkeypatch, fake)

    await _items_list_active_async(_test_settings)

    captured = capsys.readouterr()
    assert captured.out == ""
