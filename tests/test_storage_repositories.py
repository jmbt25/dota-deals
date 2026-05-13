"""Smoke test: verify :mod:`dota_deals.storage.repositories` imports."""

from __future__ import annotations

from dota_deals.storage import repositories


def test_module_imports() -> None:
    for name in (
        "upsert_item",
        "get_item_by_hash",
        "active_items",
        "insert_price_point",
        "recent_prices",
        "daily_prices",
        "insert_listing_point",
        "recent_listings",
        "upsert_latest_observation",
        "insert_signal",
        "signals_for",
        "latest_scores",
        "quarantine_record",
        "insert_run",
        "update_run",
    ):
        assert callable(getattr(repositories, name)), name
