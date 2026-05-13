"""Smoke test: verify :mod:`dota_deals.signals.comparables` imports."""

from __future__ import annotations

from dota_deals.signals import comparables


def test_module_imports() -> None:
    assert callable(comparables.compute)
