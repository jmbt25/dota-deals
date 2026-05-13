"""Confirm that ``storage/schema.sql`` applies cleanly to a fresh database.

This catches CHECK-constraint syntax errors and trailing-comma typos before
they reach :func:`dota_deals.storage.db.bootstrap_schema`.
"""

from __future__ import annotations

import sqlite3


def test_schema_applies(db_conn: sqlite3.Connection) -> None:
    tables = {
        row["name"]
        for row in db_conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    expected = {
        "items",
        "price_history",
        "listing_history",
        "latest_observation",
        "events",
        "signals",
        "quarantine",
        "runs",
    }
    missing = expected - tables
    assert not missing, f"missing tables: {sorted(missing)}"
