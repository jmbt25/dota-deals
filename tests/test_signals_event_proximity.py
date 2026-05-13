"""Smoke test: verify :mod:`dota_deals.signals.event_proximity` imports."""

from __future__ import annotations

from dota_deals.signals import event_proximity


def test_module_imports() -> None:
    assert callable(event_proximity.compute)
