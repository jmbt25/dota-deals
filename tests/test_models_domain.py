"""Smoke test: verify :mod:`dota_deals.models.domain` imports."""

from __future__ import annotations

from pydantic import BaseModel

from dota_deals.models import domain


def test_module_imports() -> None:
    for cls in (
        domain.Item,
        domain.PricePoint,
        domain.ListingPoint,
        domain.Signal,
        domain.BuyScore,
        domain.RunSummary,
    ):
        assert issubclass(cls, BaseModel)
