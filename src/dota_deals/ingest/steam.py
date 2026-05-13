"""Async HTTP client for Steam Market endpoints.

Implements the concurrency model and error-handling table documented in
``docs/ARCHITECTURE.md``: bounded concurrency via :class:`asyncio.Semaphore`,
per-request timeouts, tenacity retries with distinct policies for transient
errors, 429s, and 5xx responses, and a global cool-down following any 429.

All responses are validated through :mod:`dota_deals.models.market` before
being returned. Domain-specific failures (4xx, malformed JSON, etc.) are
surfaced via :class:`IngestError` so the runner can route them appropriately.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from dota_deals.config import Settings
from dota_deals.models.market import SteamListingsResponse, SteamPriceOverview


class IngestError(Exception):
    """An ingest request failed in a way the caller should record as a failure.

    Distinct from network errors (which are retried internally) and from
    validation failures (which go to quarantine, not ``items_failed``).
    """

    def __init__(self, message: str, *, item: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.item = item
        self.status_code = status_code


class SteamMarketClient:
    """Async context-managed client for Steam Market endpoints.

    Construction stores configuration; the underlying ``httpx.AsyncClient`` is
    opened on ``__aenter__`` and closed on ``__aexit__``. The configured
    semaphore gates concurrent in-flight requests. 429 responses trigger a
    process-global cool-down before the next request is issued.

    Usage::

        async with SteamMarketClient(settings) as client:
            overview = await client.fetch_price_overview("Inscribed Manifold Paradox")
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def __aenter__(self) -> Self:
        """Open the underlying HTTP client and prepare the semaphore."""
        raise NotImplementedError

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the underlying HTTP client."""
        raise NotImplementedError

    async def fetch_price_overview(self, item_name: str) -> SteamPriceOverview:
        """Fetch and validate a price overview for ``item_name``.

        Applies the per-request retry policy (3 attempts on timeout / 5xx /
        transport error, 4 attempts on 429 with longer backoff). Validation
        runs through :class:`SteamPriceOverview`.

        :raises IngestError: on non-retriable 4xx, exhausted retries, or
            malformed JSON.
        :raises pydantic.ValidationError: on a response that parses as JSON but
            fails wire-format validation. The runner catches these and
            quarantines them.
        """
        raise NotImplementedError

    async def fetch_listings(self, item_name: str) -> SteamListingsResponse:
        """Fetch and validate a listings histogram response for ``item_name``.

        Same retry/error semantics as :meth:`fetch_price_overview`.

        :raises IngestError: see :meth:`fetch_price_overview`.
        :raises pydantic.ValidationError: see :meth:`fetch_price_overview`.
        """
        raise NotImplementedError
