"""SQLite storage layer.

The ``schema.sql`` file is the single source of truth for table definitions;
:func:`dota_deals.storage.db.bootstrap_schema` applies it to a fresh database.
Repositories raise domain-specific exceptions, never ``sqlite3.*`` to callers.
"""
