"""In-memory drop-in for :class:`D1Client` used in tests.

The real :class:`dota_deals.storage.d1_client.D1Client` is the HTTP
boundary; mocking at the HTTP layer (with respx) is the right move for
the *D1 client itself*, because the contract under test is wire-level
behavior — status codes, retry timing, envelope shape.

For the *repository layer*, wire mocking is exactly the wrong tool: we'd
end up hand-rolling envelope JSON for every query the repos execute,
and the assertions would drift from "does the repository do the right
thing" toward "does the test correctly mock D1". So this fake replaces
the HTTP path with an in-memory SQLite execution engine that returns
the same :class:`D1Result` shape D1 does. Tests asserting repository
behavior run against it; tests asserting wire behavior keep using
respx.

Fidelity notes:

* Schema is bootstrapped from ``migrations/0001_initial.sql`` so every
  table, index, and CHECK constraint matches what D1 will see after
  ``wrangler d1 migrations apply``.
* ``rows_read`` / ``rows_written`` / ``changes`` / ``last_row_id`` are
  populated from SQLite's own accounting where it has them, and from
  result-row counts where it doesn't (SQLite's ``cursor.rowcount`` is
  unreliable for SELECT).
* Constraint violations raise :class:`D1QueryError` with the same
  attribute shape callers expect from real D1 — a synthetic ``code``
  (``7500`` for UNIQUE, ``7501`` for other integrity errors) chosen so
  tests can branch on it deterministically without depending on
  Cloudflare's undocumented internal codes.
* ``batch()`` runs in a transaction — any statement raising rolls the
  whole batch back, matching D1's documented atomicity.
* Use as an async context manager so the ``async with`` semantics in
  callers (e.g. :func:`dota_deals.storage.db_async.connect`) work
  unchanged.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from dota_deals.storage.d1_client import (
    D1Error,
    D1Meta,
    D1QueryError,
    D1Result,
    D1Statement,
)

_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

_UNIQUE_VIOLATION_CODE = 7500
_OTHER_INTEGRITY_CODE = 7501


class D1FakeClient:
    """In-memory async fake for :class:`D1Client`.

    The public method surface mirrors :class:`D1Client` exactly so callers
    that take ``D1Client | D1FakeClient`` can be parameterized without a
    Protocol. ``async with`` is required before any query/execute/batch
    — calling outside the context manager raises :class:`D1Error`, the
    same way the real client does.
    """

    def __init__(self) -> None:
        # Connection is opened lazily on __aenter__ so the cost of building
        # the in-memory schema isn't paid by tests that never invoke a
        # query.
        self._conn: sqlite3.Connection | None = None

    # ---- lifecycle ----

    async def __aenter__(self) -> Self:
        # Python's sqlite3 module wraps DML in implicit transactions by
        # default; that collides with our explicit BEGIN/COMMIT in
        # :meth:`batch`. ``isolation_level=None`` puts the connection in
        # autocommit mode so single-statement queries commit immediately
        # and batches control transactions explicitly — matching D1's
        # per-statement-autocommit / batch-as-one-transaction model.
        conn = sqlite3.connect(":memory:", isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        self._bootstrap_schema(conn)
        self._conn = conn
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ---- public API ----

    async def query(self, sql: str, params: Sequence[object] | None = None) -> D1Result:
        """Run a single SQL statement against the in-memory DB."""
        conn = self._require_conn()
        return self._execute_one(conn, sql, _coerce_params(params))

    async def execute(self, sql: str, params: Sequence[object] | None = None) -> int:
        result = await self.query(sql, params)
        return result.meta.changes

    async def batch(self, statements: Sequence[D1Statement]) -> list[D1Result]:
        """Run statements as one transaction.

        Any failure raises :class:`D1QueryError` and rolls back every
        statement in the batch — matching D1's documented batch atomicity.
        """
        if not statements:
            return []
        conn = self._require_conn()
        conn.execute("BEGIN")
        results: list[D1Result] = []
        try:
            for stmt in statements:
                results.append(self._execute_one(conn, stmt.sql, _coerce_params(stmt.params)))
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
        return results

    # ---- internals ----

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise D1Error("D1FakeClient used outside of `async with` block")
        return self._conn

    def _bootstrap_schema(self, conn: sqlite3.Connection) -> None:
        """Apply every ``[0-9]*.sql`` file in ``migrations/`` in lex order."""
        files = sorted(_MIGRATIONS_DIR.glob("[0-9]*.sql"))
        if not files:
            raise D1Error(
                f"D1FakeClient found no migrations at {_MIGRATIONS_DIR}; schema cannot be applied."
            )
        for path in files:
            conn.executescript(path.read_text(encoding="utf-8"))
        # No explicit commit needed: the connection is in autocommit
        # mode (``isolation_level=None``), so ``executescript`` flushed
        # each statement as it ran.

    def _execute_one(self, conn: sqlite3.Connection, sql: str, params: list[Any]) -> D1Result:
        """Run one statement; translate SQLite errors to D1 errors and
        package the result into a :class:`D1Result` matching D1's wire
        accounting.
        """
        try:
            cursor = conn.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            message = str(exc)
            code = _UNIQUE_VIOLATION_CODE if "UNIQUE" in message else _OTHER_INTEGRITY_CODE
            raise D1QueryError(
                f"D1 integrity violation: {message}",
                code=code,
                sql=sql,
                params=params,
            ) from exc
        except sqlite3.OperationalError as exc:
            # Operational errors (table missing, syntax error, etc.) surface
            # as D1QueryError so the repository layer doesn't have to know
            # about sqlite3.* exception types.
            raise D1QueryError(
                f"D1 operational error: {exc}",
                code=None,
                sql=sql,
                params=params,
            ) from exc

        rows: list[dict[str, Any]]
        try:
            fetched = cursor.fetchall()
        except sqlite3.ProgrammingError:
            # Some DML statements don't yield rows; fetchall raises rather
            # than returning []. Treat that as "no rows".
            fetched = []
        rows = [dict(r) for r in fetched]

        # `rowcount` is reliable for INSERT/UPDATE/DELETE but undefined for
        # SELECT (Python's docs explicitly call this out). Use it for
        # writes, fall back to len(rows) for reads.
        if _is_write(sql):
            changes = max(cursor.rowcount, 0)
            rows_written = changes
            rows_read = 0
        else:
            changes = 0
            rows_written = 0
            rows_read = len(rows)

        last_row_id = cursor.lastrowid or 0

        return D1Result(
            success=True,
            meta=D1Meta(
                changes=changes,
                last_row_id=last_row_id,
                rows_read=rows_read,
                rows_written=rows_written,
                duration=0.0,
            ),
            results=rows,
        )


# ----------------------------- helpers ----------------------------------------


def _coerce_params(params: Sequence[object] | None) -> list[Any]:
    """Match :func:`D1Client`'s bool-to-int coercion at the wire boundary."""
    if params is None:
        return []
    out: list[Any] = []
    for value in params:
        if isinstance(value, bool):
            out.append(1 if value else 0)
        else:
            out.append(value)
    return out


def _is_write(sql: str) -> bool:
    """Best-effort classification: does this SQL change the database?

    Used only for ``rows_written`` / ``rows_read`` apportioning in the
    fake's :class:`D1Meta`. Imperfect (a CTE-only statement starting with
    ``WITH`` doesn't get caught) but adequate for the repository tests
    that drive this — none of them use such constructs. The real D1
    has accurate per-statement accounting so this approximation only
    affects the fake.
    """
    leading = sql.lstrip().split(None, 1)
    if not leading:
        return False
    verb = leading[0].upper()
    return verb in {"INSERT", "UPDATE", "DELETE", "REPLACE"}
