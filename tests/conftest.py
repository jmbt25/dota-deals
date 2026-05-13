"""Shared pytest fixtures for dota-deals tests."""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Generator, Iterator
from pathlib import Path

import pytest


@pytest.fixture()
def event_loop() -> Iterator[asyncio.AbstractEventLoop]:
    """Provide a fresh asyncio event loop per test.

    pytest-asyncio in ``auto`` mode supplies its own loop by default, but some
    tests want explicit control (e.g. to drive a long-running task and observe
    cancellation).
    """
    loop = asyncio.new_event_loop()
    try:
        yield loop
    finally:
        loop.close()


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

    Foreign keys are enabled and ``schema.sql`` has been applied. The
    connection is closed and the file removed when the fixture tears down.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    schema = (
        Path(__file__).resolve().parent.parent / "src" / "dota_deals" / "storage" / "schema.sql"
    ).read_text(encoding="utf-8")
    conn.executescript(schema)
    try:
        yield conn
    finally:
        conn.close()
