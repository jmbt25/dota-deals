"""Smoke test: verify :mod:`dota_deals.models.events` imports."""

from __future__ import annotations

from pydantic import BaseModel

from dota_deals.models import events


def test_module_imports() -> None:
    assert issubclass(events.EventRecord, BaseModel)
