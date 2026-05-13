"""Smoke test: verify :mod:`dota_deals.ingest.universe` imports."""

from __future__ import annotations

import inspect

from dota_deals.ingest import universe


def test_module_imports() -> None:
    assert inspect.iscoroutinefunction(universe.refresh_universe)
