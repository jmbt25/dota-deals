"""SQLite connection management and schema bootstrap.

Exposes a thin :func:`connect` that produces a typed ``sqlite3.Connection``
configured with foreign keys enabled and row factory set, plus
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
    raise NotImplementedError


def bootstrap_schema(conn: sqlite3.Connection) -> None:
    """Apply ``schema.sql`` to ``conn``.

    Idempotent — every statement in ``schema.sql`` is ``CREATE ... IF NOT
    EXISTS``. Safe to call on a fresh or existing database.

    :raises SchemaError: if the schema file cannot be read or fails to apply.
    """
    raise NotImplementedError
