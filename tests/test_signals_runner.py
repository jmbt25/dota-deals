"""Smoke test: verify :mod:`dota_deals.signals.runner` imports."""

from __future__ import annotations

from dota_deals.signals import runner


def test_module_imports() -> None:
    assert callable(runner.compute_signals_for)
