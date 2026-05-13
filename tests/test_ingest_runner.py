"""Smoke test: verify :mod:`dota_deals.ingest.runner` imports."""

from __future__ import annotations

import inspect

from dota_deals.ingest import runner


def test_module_imports() -> None:
    assert inspect.iscoroutinefunction(runner.run_ingestion)
