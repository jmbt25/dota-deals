"""Smoke test: verify :mod:`dota_deals.logging` imports."""

from __future__ import annotations

from dota_deals import logging as dd_logging


def test_module_imports() -> None:
    assert callable(dd_logging.configure_logging)
    assert callable(dd_logging.get_logger)
