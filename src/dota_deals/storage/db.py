"""SQLite connection management and schema bootstrap.

Exposes a thin :func:`connect` that produces a typed ``sqlite3.Connection``
configured with foreign keys enabled, row factory set, and a ``MEDIAN``
aggregate registered (used by the ``v_daily_price`` view); plus
:func:`bootstrap_schema` which applies ``schema.sql`` against an empty (or
existing) database.

Repository code does not import ``sqlite3`` directly — it goes through this
module and the helpers in :mod:`dota_deals.storage.repositories`. All
domain-specific exceptions raised by the storage layer derive from
:class:`StorageError`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


class _MedianAggregate:
    """SQLite aggregate function: median of integer values.

    Registered on every connection by :func:`connect` so the
    ``v_daily_price`` view can compute its per-day median in pure SQL.

    For even-count groups, returns the integer floor of the two middles'
    average (i.e. ``(a + b) // 2``), since callers store cents and an
    integer return value avoids float drift in downstream comparisons.
    """

    def __init__(self) -> None:
        self._values: list[int] = []

    def step(self, *args: object) -> None:
        # sqlite passes one positional per row; matches _AggregateProtocol.
        value = args[0] if args else None
        if isinstance(value, int):
            self._values.append(value)

    def finalize(self) -> int | None:
        if not self._values:
            return None
        sorted_values = sorted(self._values)
        n = len(sorted_values)
        mid = n // 2
        if n % 2 == 0:
            return (sorted_values[mid - 1] + sorted_values[mid]) // 2
        return sorted_values[mid]


class StorageError(Exception):
    """Base for any domain-specific exception raised by the storage layer."""


class SchemaError(StorageError):
    """Raised when the database schema is missing, drifted, or invalid."""


class IntegrityViolation(StorageError):
    """Raised when a write violates a non-uniqueness constraint (e.g. FK)."""


def connect(path: Path) -> sqlite3.Connection:
    """Open a connection to ``path``.

    Creates parent directories as needed. Enables foreign-key enforcement and
    sets ``row_factory`` to :class:`sqlite3.Row`. The connection is *not*
    bootstrapped — call :func:`bootstrap_schema` before first use.

    :raises StorageError: if the path cannot be opened or initialized.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise StorageError(f"cannot create parent directory for {path}: {e}") from e
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        # typeshed signature: ``Callable[[], _AggregateProtocol]``. Our class
        # IS such a callable at runtime, but mypy can't recognize that without
        # one of:
        #   1) inheriting from _AggregateProtocol explicitly — blocked because
        #      _AggregateProtocol is private to the sqlite3 stub (the public
        #      `sqlite3` module re-exports nothing of the sort), OR
        #   2) an upstream typeshed fix that loosens the parameter to accept
        #      class types with structurally-matching step()/finalize() —
        #      tracked at python/typeshed.
        # Either path will let us drop this ignore. Until then, it stays.
        conn.create_aggregate("MEDIAN", 1, _MedianAggregate)  # type: ignore[arg-type]
        return conn
    except sqlite3.Error as e:
        raise StorageError(f"failed to open database at {path}: {e}") from e


def bootstrap_schema(conn: sqlite3.Connection) -> None:
    """Apply ``schema.sql`` to ``conn``.

    Idempotent — every statement in ``schema.sql`` is ``CREATE ... IF NOT
    EXISTS``. Safe to call on a fresh or existing database.

    :raises SchemaError: if the schema file cannot be read or fails to apply.
    """
    try:
        sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    except OSError as e:
        raise SchemaError(f"cannot read schema.sql at {_SCHEMA_PATH}: {e}") from e
    try:
        conn.executescript(sql)
        conn.commit()
    except sqlite3.Error as e:
        raise SchemaError(f"schema bootstrap failed: {e}") from e
