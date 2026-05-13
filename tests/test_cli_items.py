"""Tests for ``dota-deals items list-active``.

Output shape is the contract the GHA workflow consumes (``... | items.txt``
→ ``dota-deals ingest --items items.txt``), so the test pins it: one
``market_hash_name`` per line, no header, sorted by ``item_id``, inactive
items excluded.
"""

from __future__ import annotations

import sqlite3

import pytest
from typer.testing import CliRunner

from dota_deals.cli.main import app
from tests.conftest import insert_test_item


def test_list_active_outputs_one_hash_per_line(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert_test_item(db_conn, market_hash="Alpha")
    insert_test_item(db_conn, market_hash="Beta")
    insert_test_item(db_conn, market_hash="Gamma")
    db_path = db_conn.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setenv("DB_PATH", db_path)

    runner = CliRunner()
    result = runner.invoke(app, ["items", "list-active"])

    assert result.exit_code == 0, result.output
    # ``output`` is the captured stdout; ``splitlines()`` drops a trailing
    # newline cleanly so a downstream ``> file.txt`` produces no blank line.
    assert result.output.splitlines() == ["Alpha", "Beta", "Gamma"]
    # No header, no leading/trailing whitespace on any line.
    for line in result.output.splitlines():
        assert line == line.strip()


def test_list_active_excludes_inactive_items(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    insert_test_item(db_conn, market_hash="Active1")
    insert_test_item(db_conn, market_hash="Deactivated")
    insert_test_item(db_conn, market_hash="Active2")
    db_conn.execute("UPDATE items SET active = 0 WHERE market_hash = ?", ("Deactivated",))
    db_conn.commit()
    db_path = db_conn.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setenv("DB_PATH", db_path)

    runner = CliRunner()
    result = runner.invoke(app, ["items", "list-active"])

    assert result.exit_code == 0
    assert result.output.splitlines() == ["Active1", "Active2"]


def test_list_active_empty_when_no_items(
    db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No items → exit 0, empty output. The workflow's ``ingest`` step will
    then fail with its own clear error ("no items found in <file>"), which
    is the right escalation path.
    """
    db_path = db_conn.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setenv("DB_PATH", db_path)

    runner = CliRunner()
    result = runner.invoke(app, ["items", "list-active"])

    assert result.exit_code == 0
    assert result.output == ""
