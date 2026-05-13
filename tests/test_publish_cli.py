"""Smoke test for the publish CLI body.

End-to-end: populate a fake-backed D1 with one scored item, invoke the
publish helper directly (rather than via typer's CliRunner — the
runner can't host an ``asyncio.run`` inside an existing event loop),
assert the expected JSON files exist and parse.

Phase 9c-iii: the CLI command runs ``asyncio.run(_publish_async(...))``
internally. ``CliRunner`` would call into the typer-wrapped sync entry,
which would then re-call ``asyncio.run`` — incompatible with the test's
event loop. Calling ``_publish_async`` directly keeps the meaningful
coverage (the four-builder orchestration with shared D1Connection)
without fighting the runner. The typer-parsed defaults are trivial.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

import pytest

import dota_deals.cli.main as cli_main
from dota_deals.cli.main import _publish_async
from dota_deals.config import Settings
from dota_deals.storage.db_async import D1Connection
from tests._d1_fake import D1FakeClient
from tests.conftest import insert_test_item_async

AS_OF = date(2026, 5, 13)


@pytest.mark.asyncio
async def test_publish_writes_latest_and_health(
    tmp_path: Path,
    db_conn_async: tuple[D1Connection, D1FakeClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, fake = db_conn_async
    item_id = await insert_test_item_async(conn, market_hash="X", category="arcana")
    await conn.execute(
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
    await conn.execute(
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

    # Patch connect_async so the publish helper opens a fresh
    # D1Connection wrapping the *same* fake the fixture is using;
    # both connections see the same in-memory store.
    @asynccontextmanager
    async def fake_connect(
        settings: Settings, *, backend: object = None
    ) -> AsyncIterator[D1Connection]:
        cli_conn = D1Connection(fake, budget_warn=settings.d1_daily_budget_warn)
        try:
            yield cli_conn
        finally:
            cli_conn.log_budget_summary()

    monkeypatch.setattr(cli_main, "connect_async", fake_connect)

    settings = Settings(
        _env_file=None,
        db_path=tmp_path / "x.db",
        cloudflare_account_id="test",
        cloudflare_d1_database_id="test",
        cloudflare_d1_api_token="test",
    )

    out_dir = tmp_path / "out"

    class _NoopLog:
        def info(self, *_args: Any, **_kwargs: Any) -> None: ...

    await _publish_async(settings, top=5, out_dir=out_dir, include_items=False, log=_NoopLog())

    latest = json.loads((out_dir / "latest.json").read_text(encoding="utf-8"))
    assert latest["schema_version"] == 1
    assert len(latest["scores"]) == 1
    assert latest["scores"][0]["item_id"] == item_id
    assert latest["scores"][0]["current_price"] == "34.50"

    health = json.loads((out_dir / "health.json").read_text(encoding="utf-8"))
    assert health["schema_version"] == 1
    assert health["status"] in ("operational", "degraded", "warmup")
