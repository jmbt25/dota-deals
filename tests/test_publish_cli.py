"""Smoke test for ``dota-deals publish``.

End-to-end: populate a DB with one scored item, invoke the CLI's
publish command through Typer's testing helper, assert the expected
JSON files exist and parse.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, date, datetime, time
from pathlib import Path

import pytest
from typer.testing import CliRunner

from dota_deals.cli.main import app
from tests.conftest import insert_test_item

AS_OF = date(2026, 5, 13)


def test_publish_writes_latest_and_health(
    tmp_path: Path, db_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    item_id = insert_test_item(db_conn, market_hash="X", category="arcana")
    db_conn.execute(
        """
        INSERT INTO scores (item_id, computed_for, buy_score, components_json,
                            explanation, data_quality_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            AS_OF.isoformat(),
            0.5,
            json.dumps(
                {
                    "price_zscore": 0.5,
                    "supply_velocity": 0.4,
                    "event_proximity": None,
                    "comparables_delta": 0.3,
                },
                sort_keys=True,
            ),
            "Priced below recent baseline",
            json.dumps({"null_signals": ["event_proximity"]}),
        ),
    )
    db_conn.execute(
        """
        INSERT INTO latest_observation
            (item_id, observed_at, lowest_cents, listings_count)
        VALUES (?, ?, ?, ?)
        """,
        (
            item_id,
            datetime.combine(AS_OF, time(8), tzinfo=UTC).isoformat(),
            3450,
            42,
        ),
    )
    db_conn.commit()
    # Point load_settings at the test DB path via env.
    db_path = db_conn.execute("PRAGMA database_list").fetchone()["file"]
    monkeypatch.setenv("DB_PATH", db_path)
    out_dir = tmp_path / "out"

    runner = CliRunner()
    result = runner.invoke(app, ["publish", "--top", "5", "--out-dir", str(out_dir)])
    assert result.exit_code == 0, result.output

    latest = json.loads((out_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["schema_version"] == 1
    assert len(latest["scores"]) == 1
    assert latest["scores"][0]["item_id"] == item_id
    assert latest["scores"][0]["current_price"] == "34.50"

    health = json.loads((out_dir / "health.json").read_text(encoding="utf-8"))
    assert health["schema_version"] == 1
    assert health["status"] in ("operational", "degraded", "warmup")
