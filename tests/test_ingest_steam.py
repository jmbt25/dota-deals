"""Smoke test: verify :mod:`dota_deals.ingest.steam` imports."""

from __future__ import annotations

from dota_deals.ingest import steam


def test_module_imports() -> None:
    assert steam.SteamMarketClient is not None
    assert issubclass(steam.IngestError, Exception)
