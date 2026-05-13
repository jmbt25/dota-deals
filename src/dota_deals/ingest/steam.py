"""Async HTTP client for Steam Market endpoints.

Implements the concurrency model and error-handling table documented in
``docs/ARCHITECTURE.md``: bounded concurrency via :class:`asyncio.Semaphore`,
per-request timeouts, hand-rolled retries with distinct policies for transient
errors, 429s, and 5xx responses, and a global cool-down following any 429.

All responses are validated through :mod:`dota_deals.models.market` before
being returned. Domain-specific failures (4xx, malformed JSON, validation
errors) are surfaced via :class:`IngestError` and its subclasses so the
runner can route them appropriately.

Retry strategy isn't ``tenacity``-based because we need three distinct wait
patterns (5xx/timeout vs. 429 vs. no-retry-4xx) plus a global side-effect on
429. A hand-rolled loop is clearer than coercing tenacity to do that, and
keeps the test surface to a single ``sleep`` injection.
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable, Mapping
from types import TracebackType
from typing import Self, cast
from urllib.parse import quote

import httpx
from pydantic import ValidationError
from structlog.stdlib import BoundLogger

from dota_deals.config import Settings
from dota_deals.logging import get_logger
from dota_deals.models.market import (
    SteamListingsResponse,
    SteamPriceOverview,
    SteamSearchPage,
)

_PRICE_OVERVIEW_URL = "https://steamcommunity.com/market/priceoverview/"
_LISTINGS_URL_TEMPLATE = "https://steamcommunity.com/market/listings/570/{name}/render"
_SEARCH_URL = "https://steamcommunity.com/market/search/render"
_USER_AGENT = "dota-deals/0.1 (+https://github.com/RsdNoob/dota-deals)"
_MAX_NETWORK_ATTEMPTS = 3
_MAX_429_ATTEMPTS = 4
_INITIAL_BACKOFF_NETWORK_S = 1.0
_MAX_BACKOFF_NETWORK_S = 30.0
_INITIAL_BACKOFF_429_S = 30.0

SleepFn = Callable[[float], Awaitable[None]]


class IngestError(Exception):
    """Network or response-level failure the runner should count as failed.

    Distinct from network errors (which are retried internally before being
    raised) and from validation failures (which carry their own subclass).
    """

    def __init__(self, message: str, *, item: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.item = item
        self.status_code = status_code


class IngestValidationError(IngestError):
    """Response was received but cannot be validated. Routed to quarantine.

    Carries the raw payload (as text) so the runner can persist it for
    inspection without re-fetching.
    """

    def __init__(
        self,
        *,
        item: str,
        source: str,
        raw_payload: str,
        error_type: str,
        error_message: str,
    ) -> None:
        super().__init__(f"Validation failed for {source} {item}: {error_message}", item=item)
        self.source = source
        self.raw_payload = raw_payload
        self.error_type = error_type
        self.error_message = error_message


class SteamMarketClient:
    """Async context-managed client for Steam Market endpoints.

    Construction stores configuration; the underlying ``httpx.AsyncClient`` is
    opened on ``__aenter__`` and closed on ``__aexit__``. A semaphore gates
    concurrent in-flight requests; any 429 trips a process-global cool-down
    before the next request is issued.

    For tests, ``sleep`` can be overridden with a no-op or recording fake so
    timing-sensitive scenarios (429 backoff, retry intervals) finish quickly
    and assert on call patterns rather than wall-clock waits.
    """

    def __init__(self, settings: Settings, *, sleep: SleepFn | None = None) -> None:
        self._settings = settings
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep
        self._client: httpx.AsyncClient | None = None
        self._semaphore = asyncio.Semaphore(settings.steam_concurrency)
        self._ready = asyncio.Event()
        self._ready.set()
        self._cooldown_task: asyncio.Task[None] | None = None
        self._log: BoundLogger = get_logger("dota_deals.ingest.steam")

    async def __aenter__(self) -> Self:
        """Open the underlying HTTP client and prepare the semaphore."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._settings.request_timeout_s),
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            follow_redirects=False,
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the underlying HTTP client and any pending cooldown task."""
        if self._cooldown_task is not None and not self._cooldown_task.done():
            self._cooldown_task.cancel()
            try:
                await self._cooldown_task
            except asyncio.CancelledError:
                # Cancellation is expected during shutdown.
                pass
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def fetch_price_overview(self, item_name: str) -> SteamPriceOverview:
        """Fetch and validate a price overview for ``item_name``.

        Applies the per-request retry policy (3 attempts on timeout / 5xx /
        transport error, 4 attempts on 429 with longer backoff). Validation
        runs through :class:`SteamPriceOverview`.

        :raises IngestError: on non-retriable 4xx, exhausted retries.
        :raises IngestValidationError: on malformed JSON or schema mismatch
            (routed to quarantine by the runner).
        """
        params: Mapping[str, str] = {
            "appid": "570",
            "currency": str(self._settings.steam_currency_id),
            "market_hash_name": item_name,
        }
        raw_text, payload = await self._get_json(
            _PRICE_OVERVIEW_URL,
            params=params,
            source="steam_price_overview",
            item=item_name,
        )
        try:
            return SteamPriceOverview.from_raw(payload)
        except ValidationError as ve:
            raise IngestValidationError(
                item=item_name,
                source="steam_price_overview",
                raw_payload=raw_text,
                error_type=type(ve).__name__,
                error_message=str(ve),
            ) from ve

    async def fetch_listings(self, item_name: str) -> SteamListingsResponse:
        """Fetch and validate the listings render response for ``item_name``.

        Same retry / error semantics as :meth:`fetch_price_overview`. Only
        ``total_count`` is parsed from the response; ``count=1`` is sent to
        minimize body size since this endpoint is heavier than priceoverview.

        :raises IngestError: see :meth:`fetch_price_overview`.
        :raises IngestValidationError: see :meth:`fetch_price_overview`.
        """
        url = _LISTINGS_URL_TEMPLATE.format(name=quote(item_name, safe=""))
        params: Mapping[str, str] = {
            "start": "0",
            "count": "1",
            "currency": str(self._settings.steam_currency_id),
            "country": self._settings.steam_country,
            "language": "english",
            "format": "json",
        }
        raw_text, payload = await self._get_json(
            url,
            params=params,
            source="steam_listings",
            item=item_name,
        )
        try:
            return SteamListingsResponse.from_raw(payload)
        except ValidationError as ve:
            raise IngestValidationError(
                item=item_name,
                source="steam_listings",
                raw_payload=raw_text,
                error_type=type(ve).__name__,
                error_message=str(ve),
            ) from ve

    async def fetch_search_page(
        self, *, rarity_tag: str, start: int = 0, count: int = 100
    ) -> SteamSearchPage:
        """Fetch one page of market search results filtered by ``rarity_tag``.

        Uses the ``?norender=1`` form so the response is a JSON ``results``
        array rather than HTML the client would have to parse. Pagination is
        the caller's responsibility — increment ``start`` by the size of the
        returned ``results`` list until ``start >= total_count``.

        :param rarity_tag: undocumented Steam internal, e.g. ``tag_Rarity_Arcana``
            or ``tag_Rarity_Immortal`` for Dota 2 (appid 570).
        :param start: zero-based offset into the result set.
        :param count: page size; Steam caps this somewhere around 100.

        :raises IngestError: see :meth:`fetch_price_overview`.
        :raises IngestValidationError: see :meth:`fetch_price_overview`.
        """
        params: Mapping[str, str] = {
            "norender": "1",
            "appid": "570",
            "category_570_Rarity[]": rarity_tag,
            "start": str(start),
            "count": str(count),
            "currency": str(self._settings.steam_currency_id),
        }
        raw_text, payload = await self._get_json(
            _SEARCH_URL,
            params=params,
            source="steam_market_search",
            item=rarity_tag,
        )
        try:
            return SteamSearchPage.from_raw(payload)
        except ValidationError as ve:
            raise IngestValidationError(
                item=rarity_tag,
                source="steam_market_search",
                raw_payload=raw_text,
                error_type=type(ve).__name__,
                error_message=str(ve),
            ) from ve

    # ------------------------------------------------------------------ internals

    async def _get_json(
        self,
        url: str,
        *,
        params: Mapping[str, str],
        source: str,
        item: str,
    ) -> tuple[str, Mapping[str, object]]:
        """Fetch ``url`` and return ``(raw_text, parsed_json)`` on success.

        Implements the full retry policy described in the module docstring.
        """
        if self._client is None:
            raise RuntimeError("SteamMarketClient used outside async-with context")

        log = self._log.bind(source=source, item_id=item)

        network_attempts = 0
        rate_limit_attempts = 0

        while True:
            await self._wait_ready()
            attempt_log = log.bind(
                attempt=network_attempts + rate_limit_attempts + 1,
            )
            try:
                async with self._semaphore:
                    response = await self._client.get(url, params=params)
            except httpx.TimeoutException as e:
                network_attempts += 1
                if network_attempts >= _MAX_NETWORK_ATTEMPTS:
                    attempt_log.error("timeout exhausted retries", attempts=network_attempts)
                    raise IngestError(
                        f"timeout after {network_attempts} attempts: {e}",
                        item=item,
                    ) from e
                backoff = _network_backoff(network_attempts)
                attempt_log.warning("timeout, retrying", backoff_s=backoff)
                await self._sleep(backoff)
                continue
            except httpx.RequestError as e:
                network_attempts += 1
                if network_attempts >= _MAX_NETWORK_ATTEMPTS:
                    attempt_log.error(
                        "transport error exhausted retries", attempts=network_attempts
                    )
                    raise IngestError(
                        f"transport error after {network_attempts} attempts: {e}",
                        item=item,
                    ) from e
                backoff = _network_backoff(network_attempts)
                attempt_log.warning(
                    "transport error, retrying",
                    error=type(e).__name__,
                    backoff_s=backoff,
                )
                await self._sleep(backoff)
                continue

            status = response.status_code

            if 200 <= status < 300:
                raw_text = response.text
                try:
                    payload = cast(Mapping[str, object], response.json())
                except json.JSONDecodeError as e:
                    attempt_log.warning("malformed JSON response", error=str(e))
                    raise IngestValidationError(
                        item=item,
                        source=source,
                        raw_payload=raw_text,
                        error_type="JSONDecodeError",
                        error_message=str(e),
                    ) from e
                return raw_text, payload

            if status == 429:
                rate_limit_attempts += 1
                self._trigger_cooldown()
                if rate_limit_attempts >= _MAX_429_ATTEMPTS:
                    attempt_log.error("429 exhausted retries", attempts=rate_limit_attempts)
                    raise IngestError(
                        f"rate-limited after {rate_limit_attempts} attempts",
                        item=item,
                        status_code=429,
                    )
                backoff = _rate_limit_backoff(rate_limit_attempts)
                attempt_log.warning("429 received, backing off", backoff_s=backoff)
                await self._sleep(backoff)
                continue

            if 500 <= status < 600:
                network_attempts += 1
                if network_attempts >= _MAX_NETWORK_ATTEMPTS:
                    attempt_log.error(
                        "5xx exhausted retries",
                        attempts=network_attempts,
                        status_code=status,
                    )
                    raise IngestError(
                        f"5xx after {network_attempts} attempts (last {status})",
                        item=item,
                        status_code=status,
                    )
                backoff = _network_backoff(network_attempts)
                attempt_log.warning("5xx, retrying", status_code=status, backoff_s=backoff)
                await self._sleep(backoff)
                continue

            # 3xx redirect (we don't follow) or 4xx non-429 → hard failure.
            attempt_log.error("non-retriable status", status_code=status)
            raise IngestError(f"non-retriable HTTP {status}", item=item, status_code=status)

    async def _wait_ready(self) -> None:
        if not self._ready.is_set():
            await self._ready.wait()

    def _trigger_cooldown(self) -> None:
        """Schedule a global cooldown if one isn't already in flight.

        The release task is fire-and-forget. We attach an explicit done-callback
        so any exception raised inside :meth:`_release_after_cooldown` is logged
        instead of silently disappearing into ``asyncio``'s "Task exception was
        never retrieved" warning.
        """
        if self._ready.is_set():
            self._ready.clear()
            task = asyncio.create_task(self._release_after_cooldown())
            task.add_done_callback(self._on_cooldown_done)
            self._cooldown_task = task

    def _on_cooldown_done(self, task: asyncio.Task[None]) -> None:
        """Surface any non-cancellation exception from the cooldown task."""
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            self._log.error(
                "cooldown release raised",
                exc_type=type(exc).__name__,
                error=str(exc),
            )

    async def _release_after_cooldown(self) -> None:
        try:
            await self._sleep(self._settings.cooldown_429_s)
        finally:
            # Always release `_ready` — even on cancellation — so a shutdown
            # mid-cooldown doesn't leave queued requests blocked forever.
            self._ready.set()


def _network_backoff(attempt: int) -> float:
    """Exponential backoff with jitter for timeouts / transport / 5xx.

    Attempt 1 → ~1s; attempt 2 → ~2s; clipped at _MAX_BACKOFF_NETWORK_S.
    """
    base: float = _INITIAL_BACKOFF_NETWORK_S * float(2 ** (attempt - 1))
    jitter: float = random.uniform(0.0, base * 0.25)
    return float(min(base + jitter, _MAX_BACKOFF_NETWORK_S))


def _rate_limit_backoff(attempt: int) -> float:
    """Longer backoff for 429s. Attempt 1 → 30s; doubles each subsequent attempt."""
    return _INITIAL_BACKOFF_429_S * float(2 ** (attempt - 1))
