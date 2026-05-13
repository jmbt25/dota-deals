"""Smoke test: verify :mod:`dota_deals.notifier.stdout` imports."""

from __future__ import annotations

from dota_deals.notifier import stdout


def test_module_imports() -> None:
    assert callable(stdout.emit)
