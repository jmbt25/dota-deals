"""Smoke test: verify :mod:`dota_deals.notifier.json_file` imports."""

from __future__ import annotations

from dota_deals.notifier import json_file


def test_module_imports() -> None:
    assert callable(json_file.emit)
