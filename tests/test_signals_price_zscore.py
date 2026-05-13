"""Smoke test: verify :mod:`dota_deals.signals.price_zscore` imports."""

from __future__ import annotations

from dota_deals.signals import price_zscore


def test_module_imports() -> None:
    assert callable(price_zscore.compute)
