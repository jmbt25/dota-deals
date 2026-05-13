"""Smoke test: verify :mod:`dota_deals.signals.supply_velocity` imports."""

from __future__ import annotations

from dota_deals.signals import supply_velocity


def test_module_imports() -> None:
    assert callable(supply_velocity.compute)
