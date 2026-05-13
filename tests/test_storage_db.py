"""Tests for :mod:`dota_deals.storage.db`.

The wrapper's job is small but important: accumulate ``rows_read`` /
``rows_written`` across every call into the underlying backend, and emit
a WARNING on close when cumulative reads exceed
:attr:`Settings.d1_daily_budget_warn`. Both are tested here against
:class:`D1FakeClient` so we don't pay HTTP-mocking cost for what is
fundamentally arithmetic over response metadata.

The full repository surface is exercised in
``test_storage_repositories_async.py``; this file isolates the wrapper.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from structlog.stdlib import BoundLogger

from dota_deals.config import Settings
from dota_deals.storage.d1_client import D1Statement
from dota_deals.storage.db import D1Connection, connect
from tests._d1_fake import D1FakeClient


class _RecordingLogger:
    """Captures structlog ``warning(event, **kw)`` / ``debug(...)`` calls.

    Tests want to assert that the budget-summary log path fired with the
    right level and the right structured fields. The real configured
    logger goes through structlog's :class:`PrintLoggerFactory` direct
    to stderr, which ``caplog`` doesn't capture — easier to inject a
    fake than to rebuild the rendering stack mid-test.
    """

    def __init__(self) -> None:
        self.warnings: list[tuple[str, dict[str, Any]]] = []
        self.debugs: list[tuple[str, dict[str, Any]]] = []

    def warning(self, event: str, **kw: Any) -> None:
        self.warnings.append((event, kw))

    def debug(self, event: str, **kw: Any) -> None:
        self.debugs.append((event, kw))

    def info(self, event: str, **kw: Any) -> None: ...

    def error(self, event: str, **kw: Any) -> None: ...

    def bind(self, **_: Any) -> _RecordingLogger:
        return self


def _d1_settings(tmp_path: Path, *, budget: int = 1_000_000) -> Settings:
    return Settings(
        db_path=tmp_path / "x.db",
        cloudflare_account_id="acct",
        cloudflare_d1_database_id="db",
        cloudflare_d1_api_token="tok",
        d1_daily_budget_warn=budget,
    )


# ----------------------------- accounting -------------------------------------


@pytest.mark.asyncio
async def test_rows_read_accumulates_across_queries(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    async with D1FakeClient() as fake:
        async with connect(settings, backend=fake) as conn:
            # Three reads against an empty table — each returns 0 rows,
            # so cumulative rows_read stays at 0. Verifies the
            # accumulator is wired up and doesn't double-count.
            for _ in range(3):
                await conn.query("SELECT * FROM items")
            assert conn.rows_read == 0

            # Insert two rows, then read them back.
            await conn.execute(
                "INSERT INTO items (market_hash, name, category, "
                "first_seen_at) VALUES (?, ?, ?, ?)",
                ("A", "Alpha", "arcana", "2026-01-01T00:00:00+00:00"),
            )
            await conn.execute(
                "INSERT INTO items (market_hash, name, category, "
                "first_seen_at) VALUES (?, ?, ?, ?)",
                ("B", "Beta", "arcana", "2026-01-01T00:00:00+00:00"),
            )
            assert conn.rows_written == 2

            await conn.query("SELECT * FROM items")
            assert conn.rows_read == 2

            await conn.query("SELECT * FROM items")
            assert conn.rows_read == 4  # accumulator, not last-call value


@pytest.mark.asyncio
async def test_batch_accumulates_per_statement(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    async with D1FakeClient() as fake:
        async with connect(settings, backend=fake) as conn:
            statements = [
                D1Statement(
                    sql=(
                        "INSERT INTO items (market_hash, name, category, "
                        "first_seen_at) VALUES (?, ?, ?, ?)"
                    ),
                    params=(h, h, "arcana", "2026-01-01T00:00:00+00:00"),
                )
                for h in ("X", "Y", "Z")
            ]
            await conn.batch(statements)
        assert conn.rows_written == 3
        assert conn.rows_read == 0


@pytest.mark.asyncio
async def test_reset_counters_zeroes_accumulators(tmp_path: Path) -> None:
    """Long-lived connections may want to reset across phases (e.g.,
    after a warm-up read pass) so the budget reflects the section the
    caller actually cares about."""
    settings = _d1_settings(tmp_path)
    async with D1FakeClient() as fake:
        async with connect(settings, backend=fake) as conn:
            await conn.execute(
                "INSERT INTO items (market_hash, name, category, "
                "first_seen_at) VALUES (?, ?, ?, ?)",
                ("A", "Alpha", "arcana", "2026-01-01T00:00:00+00:00"),
            )
            await conn.query("SELECT * FROM items")
            assert conn.rows_read == 1
            assert conn.rows_written == 1

            conn.reset_counters()
            assert conn.rows_read == 0
            assert conn.rows_written == 0


# ----------------------------- budget warning ---------------------------------


@pytest.mark.asyncio
async def test_over_budget_logs_warning(tmp_path: Path) -> None:
    """At connection close, cumulative rows_read above the budget
    threshold emits a WARNING with structured fields. Operator-visible
    signal that catches an accidental unbounded scan before it
    becomes a billing surprise.
    """
    recorder = _RecordingLogger()
    async with D1FakeClient() as fake:
        conn = D1Connection(
            fake,
            budget_warn=1,
            logger=cast(BoundLogger, recorder),
        )
        await conn.execute(
            "INSERT INTO items (market_hash, name, category, first_seen_at) VALUES (?, ?, ?, ?)",
            ("A", "Alpha", "arcana", "2026-01-01T00:00:00+00:00"),
        )
        await conn.execute(
            "INSERT INTO items (market_hash, name, category, first_seen_at) VALUES (?, ?, ?, ?)",
            ("B", "Beta", "arcana", "2026-01-01T00:00:00+00:00"),
        )
        await conn.query("SELECT * FROM items")
        assert conn.rows_read == 2

        conn.log_budget_summary()

    assert len(recorder.warnings) == 1
    event, fields = recorder.warnings[0]
    assert event == "d1_connection_over_budget"
    assert fields["rows_read"] == 2
    assert fields["budget"] == 1


@pytest.mark.asyncio
async def test_under_budget_logs_debug_not_warning(tmp_path: Path) -> None:
    recorder = _RecordingLogger()
    async with D1FakeClient() as fake:
        conn = D1Connection(
            fake,
            budget_warn=100,
            logger=cast(BoundLogger, recorder),
        )
        await conn.query("SELECT * FROM items")
        conn.log_budget_summary()

    assert recorder.warnings == []
    assert len(recorder.debugs) == 1
    assert recorder.debugs[0][0] == "d1_connection_closed"


@pytest.mark.asyncio
async def test_zero_budget_disables_warning(tmp_path: Path) -> None:
    """Budget = 0 disables the soft cap entirely. Operators who want to
    silence the warning during a deliberate full-table read can set
    it to 0 in their environment.
    """
    recorder = _RecordingLogger()
    async with D1FakeClient() as fake:
        conn = D1Connection(
            fake,
            budget_warn=0,
            logger=cast(BoundLogger, recorder),
        )
        await conn.execute(
            "INSERT INTO items (market_hash, name, category, first_seen_at) VALUES (?, ?, ?, ?)",
            ("A", "Alpha", "arcana", "2026-01-01T00:00:00+00:00"),
        )
        await conn.query("SELECT * FROM items")
        conn.log_budget_summary()

    assert recorder.warnings == []


# ----------------------------- connect lifecycle ------------------------------


@pytest.mark.asyncio
async def test_connect_with_injected_backend_does_not_own_lifecycle(
    tmp_path: Path,
) -> None:
    """When ``backend`` is passed in, ``connect`` does NOT enter/exit it
    — the test owns the lifecycle. This keeps wiring simple in test code
    where the backend is constructed once and shared across multiple
    connect() calls."""
    settings = _d1_settings(tmp_path)
    fake = D1FakeClient()
    async with fake:
        async with connect(settings, backend=fake) as conn1:
            await conn1.execute(
                "INSERT INTO items (market_hash, name, category, "
                "first_seen_at) VALUES (?, ?, ?, ?)",
                ("A", "Alpha", "arcana", "2026-01-01T00:00:00+00:00"),
            )
        # After conn1 exits, the fake is still open.
        async with connect(settings, backend=fake) as conn2:
            result = await conn2.query("SELECT * FROM items")
            assert len(result.results) == 1


@pytest.mark.asyncio
async def test_d1_connection_direct_construction(tmp_path: Path) -> None:
    """``D1Connection`` can be constructed without ``connect``; useful
    for tests that want to skip the context-manager wrapping. The
    backend lifecycle remains the test's responsibility."""
    async with D1FakeClient() as fake:
        conn = D1Connection(fake, budget_warn=10)
        await conn.execute(
            "INSERT INTO items (market_hash, name, category, first_seen_at) VALUES (?, ?, ?, ?)",
            ("A", "Alpha", "arcana", "2026-01-01T00:00:00+00:00"),
        )
        assert conn.rows_written == 1
