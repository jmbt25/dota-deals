"""Smoke test: verify :mod:`dota_deals.storage.db` imports."""

from __future__ import annotations

from dota_deals.storage import db


def test_module_imports() -> None:
    assert callable(db.connect)
    assert callable(db.bootstrap_schema)
    assert issubclass(db.StorageError, Exception)
    assert issubclass(db.SchemaError, db.StorageError)
    assert issubclass(db.IntegrityViolation, db.StorageError)
