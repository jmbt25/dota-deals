"""Ingestion orchestrator.

Fans out across the supplied item list using a single :class:`SteamMarketClient`,
validates each response, persists valid records to ``price_history`` /
``listing_history`` / ``latest_observation``, routes validation failures to
``quarantine``, and writes a single row to ``runs`` summarizing the outcome.
"""

from __future__ import annotations

from dota_deals.config import Settings
from dota_deals.models.domain import RunSummary


async def run_ingestion(
    items: list[str],
    settings: Settings,
    *,
    run_id: str,
    parent_run_id: str | None = None,
) -> RunSummary:
    """Ingest current price and listing data for every item in ``items``.

    Each item is fetched with both ``fetch_price_overview`` and
    ``fetch_listings``. Valid responses are persisted; validation failures are
    quarantined; transport/HTTP failures are counted but do not abort the run.

    :param items: list of Steam ``market_hash_name`` values to fetch.
    :param settings: process settings (concurrency, timeouts, cool-down).
    :param run_id: UUID4 identifying this ingestion run.
    :param parent_run_id: optional UUID4 grouping this run with sibling
        stage runs from the same CLI invocation.
    """
    raise NotImplementedError
