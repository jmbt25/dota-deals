"""Universe discovery — build the list of items to track.

Pages ``https://steamcommunity.com/market/search/render`` for ``appid=570``,
filtered to the relevant category tags for arcanas and immortals. Each
discovered item is upserted into ``items``; the ``last_seen_at`` column tracks
when we last observed it on Steam. Items not seen for three consecutive
refreshes are flipped to ``active = 0``.
"""

from __future__ import annotations

from dota_deals.config import Settings
from dota_deals.models.domain import RunSummary


async def refresh_universe(
    settings: Settings,
    *,
    run_id: str,
    parent_run_id: str | None = None,
) -> RunSummary:
    """Refresh the ``items`` table from Steam's market search.

    Idempotent across invocations: re-running on a quiet day yields zero new
    items and an updated ``last_seen_at`` for everything still on the market.

    :param settings: process settings (currency / country params, concurrency).
    :param run_id: UUID4 identifying this universe-refresh run.
    :param parent_run_id: optional UUID4 grouping this run with sibling
        stage runs from the same CLI invocation.
    """
    raise NotImplementedError
