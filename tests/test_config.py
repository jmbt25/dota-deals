"""Smoke test: verify :mod:`dota_deals.config` imports and exposes Settings."""

from __future__ import annotations

from dota_deals import config


def test_module_imports() -> None:
    assert config.Settings is not None
    assert callable(config.load_settings)


def test_settings_has_expected_fields() -> None:
    fields = config.Settings.model_fields
    expected = {
        "db_path",
        "steam_concurrency",
        "request_timeout_s",
        "cooldown_429_s",
        "steam_currency_id",
        "steam_country",
        "ingest_cadence_hours",
        "log_format",
    }
    assert expected.issubset(fields.keys())
