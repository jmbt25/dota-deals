"""Smoke test: verify :mod:`dota_deals.models.market` imports."""

from __future__ import annotations

from pydantic import BaseModel

from dota_deals.models import market


def test_module_imports() -> None:
    assert issubclass(market.SteamPriceOverview, BaseModel)
    assert issubclass(market.SteamListingsResponse, BaseModel)
    assert issubclass(market.SteamSearchPage, BaseModel)
    assert issubclass(market.SteamSearchResult, BaseModel)
