"""Async D1-backed connection layer.

This module is the async counterpart to :mod:`dota_deals.storage.db`.
The sync module stays in place for now — every caller still goes
through it; the async path lives alongside until Phase 9c flips
runners and the CLI over.

What this layer adds on top of :class:`D1Client`:

* A small :class:`D1Connection` wrapper that funnels every read/write
  through itself and accumulates per-connection ``rows_read`` /
  ``rows_written`` counters. On context exit, if cumulative reads
  exceed :attr:`Settings.d1_daily_budget_warn`, the connection logs
  a structured WARNING with the totals so an accidentally-unbounded
  scan surfaces in the run logs before it surfaces in the bill.

* An :func:`asynccontextmanager`-flavored :func:`connect` that mirrors
  the sync ``connect(path)`` ergonomics. Repository functions take
  ``conn: D1Connection`` as their first argument — same shape as the
  sync ``conn: sqlite3.Connection``, just async.

This module deliberately does *not* re-implement schema bootstrap
(:func:`dota_deals.storage.db.bootstrap_schema`). D1 schemas are
applied via ``wrangler d1 migrations apply``, not by application code;
trying to bootstrap on every connect would risk a runaway DDL
attempt on a misconfigured production database.

Exception hierarchy is shared with the sync module: callers can keep
catching :class:`StorageError` and friends. Where useful, D1-specific
exceptions (e.g. :class:`D1ConfigError`) are translated at the
:func:`connect` boundary.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from types import TracebackType
from typing import Protocol, Self

from structlog.stdlib import BoundLogger

from dota_deals.config import Settings
from dota_deals.logging import get_logger
from dota_deals.storage.d1_client import (
    D1Client,
    D1Result,
    D1Statement,
)


class D1Backend(Protocol):
    """Common surface for :class:`D1Client` and ``D1FakeClient``.

    Defined as a Protocol so the connection wrapper can accept either
    the real HTTP client or the in-memory test fake without an
    ``isinstance`` dance. Exposed publicly so runners (e.g.
    :mod:`dota_deals.ingest.runner`) can declare an optional
    ``backend`` parameter as their test seam without importing the
    fake into production code.
    """

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def query(self, sql: str, params: Sequence[object] | None = None) -> D1Result: ...

    async def execute(self, sql: str, params: Sequence[object] | None = None) -> int: ...

    async def batch(self, statements: Sequence[D1Statement]) -> list[D1Result]: ...


class D1Connection:
    """Application-level handle wrapping a D1 backend with accounting.

    Repository functions take this as their first argument; on close, it
    surfaces cumulative read totals via structured logging. Mirrors the
    ``conn: sqlite3.Connection`` contract of the sync layer but async.

    The wrapper does not own the backend — :func:`connect` owns the
    lifecycle. ``D1Connection`` is constructed with an *already-open*
    backend; this keeps test wiring with :class:`D1FakeClient` simple
    (the test enters the fake itself, then passes the bound client in).
    """

    def __init__(
        self,
        backend: D1Backend,
        *,
        budget_warn: int,
        logger: BoundLogger | None = None,
    ) -> None:
        self._backend = backend
        self._budget_warn = budget_warn
        self._log = logger if logger is not None else get_logger(__name__)
        self._rows_read = 0
        self._rows_written = 0

    # ---- accounting ----

    @property
    def rows_read(self) -> int:
        return self._rows_read

    @property
    def rows_written(self) -> int:
        return self._rows_written

    def reset_counters(self) -> None:
        """Zero the accumulators. Useful for tests that want to assert on
        per-call totals across a long-lived connection."""
        self._rows_read = 0
        self._rows_written = 0

    # ---- public API (mirrors D1Client) ----

    async def query(self, sql: str, params: Sequence[object] | None = None) -> D1Result:
        result = await self._backend.query(sql, params)
        self._rows_read += result.meta.rows_read
        self._rows_written += result.meta.rows_written
        return result

    async def execute(self, sql: str, params: Sequence[object] | None = None) -> int:
        """Run a write; return ``meta.changes``.

        Implemented in terms of :meth:`query` (not delegating to
        ``self._backend.execute``) so the rows_written counter is
        updated even when the caller doesn't need the row count.
        """
        result = await self.query(sql, params)
        return result.meta.changes

    async def batch(self, statements: Sequence[D1Statement]) -> list[D1Result]:
        results = await self._backend.batch(statements)
        for r in results:
            self._rows_read += r.meta.rows_read
            self._rows_written += r.meta.rows_written
        return results

    # ---- lifecycle helpers ----

    def log_budget_summary(self) -> None:
        """Emit a per-connection rows-read summary line.

        WARNING if over budget, DEBUG otherwise. Public so tests can
        drive it directly; in production, :func:`connect` calls it on
        context exit.
        """
        if self._budget_warn > 0 and self._rows_read > self._budget_warn:
            self._log.warning(
                "d1_connection_over_budget",
                rows_read=self._rows_read,
                rows_written=self._rows_written,
                budget=self._budget_warn,
            )
        else:
            self._log.debug(
                "d1_connection_closed",
                rows_read=self._rows_read,
                rows_written=self._rows_written,
                budget=self._budget_warn,
            )


@asynccontextmanager
async def connect(
    settings: Settings,
    *,
    backend: D1Backend | None = None,
) -> AsyncIterator[D1Connection]:
    """Yield a :class:`D1Connection` bound to a D1 backend.

    Default path: builds a fresh :class:`D1Client` from ``settings`` and
    drives it through its own ``async with``. The yielded connection is
    closed (and its budget summary logged) when the context exits.

    Tests inject an alternative backend (typically ``D1FakeClient``) by
    passing it as ``backend``; ownership stays with the caller in that
    case — the caller's own ``async with`` is what opens / closes it.

    :raises D1ConfigError: if any of the required ``CLOUDFLARE_*``
        settings is missing. Surfaced from :class:`D1Client`'s
        constructor without translation — the failure mode is "fix the
        env, not retry", same as before.
    """
    if backend is None:
        async with D1Client(settings) as owned:
            conn = D1Connection(
                owned,
                budget_warn=settings.d1_daily_budget_warn,
            )
            try:
                yield conn
            finally:
                conn.log_budget_summary()
        return

    conn = D1Connection(
        backend,
        budget_warn=settings.d1_daily_budget_warn,
    )
    try:
        yield conn
    finally:
        conn.log_budget_summary()
