"""Async HTTP client for Cloudflare D1.

D1's public REST API is the only way to reach a D1 database from outside
a Worker. Everything in this module is a thin, typed wrapper around two
endpoints:

* ``POST /accounts/{account}/d1/database/{db}/query`` — single SQL with
  bound parameters; result is a list of one ``D1Result`` (D1 always
  wraps in a list to match the batch shape).
* ``POST /accounts/{account}/d1/database/{db}/raw`` — same shape but
  returns a list of column-name / row-array pairs instead of dict rows.
  Not used by the repository layer (dict rows are more ergonomic); we
  expose it because tests for batch chunking want the lighter shape.

The repository layer (Phase 9b) will sit on top of :class:`D1Client.query`,
:class:`D1Client.execute`, and :class:`D1Client.batch`. This file is the
HTTP boundary — no SQL is constructed here.

Error model
-----------
Errors are split so callers know whether to retry, abort, or reroute to
quarantine:

* :class:`D1ConfigError` — credentials missing at construction. Fail fast.
* :class:`D1AuthError` — Cloudflare rejected the token (401, 403). The
  CLI surfaces this with a clear message; retrying without a new token is
  pointless.
* :class:`D1NotFoundError` — account or database ID is wrong (404).
* :class:`D1RateLimitError` — 429 from the API gateway. Carries the
  ``Retry-After`` value when present so the caller can sleep that long.
* :class:`D1QueryError` — D1 ran the SQL but reported ``success=False``
  (constraint violations, schema errors, type mismatches). Carries the
  D1 error code/message and the originating SQL for debugging.
* :class:`D1TransportError` — network failure or 5xx after retries
  exhausted. Caller's choice to abort the run or back off further.

Retries
-------
Network/5xx retries are hand-rolled to mirror :mod:`dota_deals.ingest.steam`:
three attempts with exponential backoff + jitter. 429 is special-cased to
honor ``Retry-After`` when D1 sets it. 4xx other than 429 is not retried.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from types import TracebackType
from typing import Any, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field
from structlog.stdlib import BoundLogger

from dota_deals.config import Settings
from dota_deals.logging import get_logger

_API_BASE = "https://api.cloudflare.com/client/v4"
_USER_AGENT = "dota-deals/0.1 (+https://github.com/RsdNoob/dota-deals)"
_MAX_NETWORK_ATTEMPTS = 3
_INITIAL_BACKOFF_S = 1.0
_MAX_BACKOFF_S = 30.0
_DEFAULT_429_BACKOFF_S = 30.0

SleepFn = Callable[[float], Awaitable[None]]


# ----------------------------- exceptions -------------------------------------


class D1Error(Exception):
    """Base for any D1-layer failure.

    Subclasses encode disposition (retry, abort, fix-credentials) so callers
    can branch without inspecting messages.
    """


class D1ConfigError(D1Error):
    """One or more of ``CLOUDFLARE_ACCOUNT_ID``, ``CLOUDFLARE_D1_DATABASE_ID``,
    or ``CLOUDFLARE_D1_API_TOKEN`` is missing. Caller must fix the
    environment, not retry.
    """


class D1AuthError(D1Error):
    """Cloudflare rejected the token (401 or 403). Not retried."""


class D1NotFoundError(D1Error):
    """Account or database ID is unknown to Cloudflare (404). Not retried."""


class D1RateLimitError(D1Error):
    """Hit Cloudflare's API rate limit (429). Carries ``retry_after_s`` when
    the response sets the ``Retry-After`` header so the caller can sleep
    exactly that long before retrying.
    """

    def __init__(self, message: str, *, retry_after_s: float | None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class D1QueryError(D1Error):
    """D1 ran the request but reported failure for at least one statement.

    Carries the per-statement error code/message and the originating SQL
    for debugging. Constraint violations (unique, FK, check) surface here.
    """

    def __init__(
        self,
        message: str,
        *,
        code: int | None,
        sql: str,
        params: Sequence[object] | None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.sql = sql
        self.params = list(params) if params is not None else None


class D1TransportError(D1Error):
    """Network failure or 5xx after retries exhausted. Caller decides whether
    to abort the run or back off further.
    """


# --------------------------- response models ----------------------------------


class D1Meta(BaseModel):
    """Per-statement metadata returned by D1.

    D1 returns more fields than we model here (``served_by_*``, ``timings``);
    those are useful for runbook diagnostics but not required by code, so
    they're tolerated via ``extra='ignore'`` rather than enumerated.
    """

    model_config = ConfigDict(extra="ignore")

    changes: int = 0
    last_row_id: int = Field(default=0, alias="last_row_id")
    rows_read: int = 0
    rows_written: int = 0
    duration: float = 0.0


class D1ErrorEntry(BaseModel):
    """One entry in the top-level ``errors`` array."""

    model_config = ConfigDict(extra="ignore")

    code: int | None = None
    message: str = ""


class D1Result(BaseModel):
    """One entry in the top-level ``result`` array.

    ``results`` is the list of row dicts for SELECT-like statements;
    DML statements return an empty list with ``meta.changes`` populated.
    """

    model_config = ConfigDict(extra="ignore")

    success: bool
    meta: D1Meta = Field(default_factory=D1Meta)
    results: list[dict[str, Any]] = Field(default_factory=list)


class D1Envelope(BaseModel):
    """Top-level Cloudflare API response envelope.

    The envelope is identical for ``/query``, ``/raw``, and batch invocations:
    ``success`` is the API-gateway-level pass/fail, ``errors`` is populated
    when ``success=False`` *or* when individual statements failed.
    """

    model_config = ConfigDict(extra="ignore")

    success: bool
    errors: list[D1ErrorEntry] = Field(default_factory=list)
    messages: list[D1ErrorEntry] = Field(default_factory=list)
    result: list[D1Result] = Field(default_factory=list)


# -------------------------------- client --------------------------------------


class D1Statement(BaseModel):
    """A single SQL statement + its bound parameters.

    Used as the unit of batch construction. Booleans are coerced to int
    at construction time because D1's parameter binder accepts ints for
    boolean columns but does not always accept native Python ``bool``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    sql: str
    params: tuple[Any, ...] = ()


def _coerce_params(params: Sequence[object] | None) -> list[Any]:
    """Convert ``bool`` to ``int`` and pass everything else through.

    D1 binds bools inconsistently across language SDKs; the public REST
    docs are explicit that integers and strings are the safe types for
    boolean columns. Coercion at the wire boundary keeps caller code free
    of casts.
    """
    if params is None:
        return []
    out: list[Any] = []
    for value in params:
        if isinstance(value, bool):
            out.append(1 if value else 0)
        else:
            out.append(value)
    return out


class D1Client:
    """Async D1 HTTP client.

    Construct with a :class:`Settings`; use as an async context manager so
    the underlying :class:`httpx.AsyncClient` is opened and closed deterministically.

    ``query`` / ``execute`` / ``batch`` are the three public entry points;
    the repository layer (Phase 9b) builds on these. Pre-flight validation
    happens in ``__init__`` — :class:`D1ConfigError` is raised the moment
    credentials are missing, before any request is attempted.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: SleepFn | None = None,
        logger: BoundLogger | None = None,
    ) -> None:
        if not settings.cloudflare_account_id:
            raise D1ConfigError("CLOUDFLARE_ACCOUNT_ID is not set")
        if not settings.cloudflare_d1_database_id:
            raise D1ConfigError("CLOUDFLARE_D1_DATABASE_ID is not set")
        if not settings.cloudflare_d1_api_token:
            raise D1ConfigError("CLOUDFLARE_D1_API_TOKEN is not set")

        self._account_id = settings.cloudflare_account_id
        self._database_id = settings.cloudflare_d1_database_id
        self._token = settings.cloudflare_d1_api_token
        self._timeout_s = settings.d1_timeout_s
        self._max_batch_size = settings.d1_max_batch_size

        self._owns_client = client is None
        self._client = client
        self._sleep: SleepFn = sleep if sleep is not None else asyncio.sleep
        self._log = logger if logger is not None else get_logger(__name__)

    # ---- lifecycle ----

    async def __aenter__(self) -> Self:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=_API_BASE,
                timeout=self._timeout_s,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                    "User-Agent": _USER_AGENT,
                },
            )
            self._owns_client = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---- public API ----

    async def query(self, sql: str, params: Sequence[object] | None = None) -> D1Result:
        """Run a single SQL statement; return the first (only) :class:`D1Result`.

        Use for both SELECT and DML statements. The returned ``D1Result.results``
        is the row list; ``D1Result.meta.changes`` is the affected-rows count
        for writes.
        """
        envelope = await self._post_json(
            "/query",
            {"sql": sql, "params": _coerce_params(params)},
            sql_for_error=sql,
            params_for_error=params,
        )
        return self._unwrap_single(envelope, sql=sql, params=params)

    async def execute(self, sql: str, params: Sequence[object] | None = None) -> int:
        """Run a write statement; return ``meta.changes``.

        Convenience wrapper around :meth:`query` for callers that don't care
        about the (empty) result rows. Idempotent INSERTs with ``OR IGNORE``
        return ``0`` on PK collision, matching the existing sqlite3 ``rowcount``
        contract.
        """
        result = await self.query(sql, params)
        return result.meta.changes

    async def batch(self, statements: Sequence[D1Statement]) -> list[D1Result]:
        """Run a batch of statements in a single HTTP request.

        D1's batch endpoint runs the statements as one transaction; if any
        fails, the whole batch is rolled back and :class:`D1QueryError` is
        raised. Inputs longer than :attr:`Settings.d1_max_batch_size` are
        split into sequential sub-batches and concatenated — callers can
        pass a thousand inserts without worrying about request limits.

        Order is preserved: the returned ``D1Result`` list lines up
        positionally with ``statements``.
        """
        if not statements:
            return []
        # Chunk on the client side so callers can pass arbitrarily long
        # sequences. Sub-batches are sequential (not parallel) to preserve
        # the all-or-nothing semantics each transaction promises locally;
        # parallelizing would lose that within a single logical batch.
        results: list[D1Result] = []
        for chunk in _chunked(statements, self._max_batch_size):
            # D1's /query endpoint accepts either ``{"sql": ..., "params": ...}``
            # for a single statement or ``{"batch": [...]}`` for a multi-
            # statement transaction. Phase 9a wrongly POSTed a bare array
            # here (the respx-mocked tests didn't catch it because they
            # don't validate request shape against the real wire schema);
            # the Phase 9c-ii real-D1 smoke test surfaced it with code
            # 7400 "Expected object, received array".
            payload = {
                "batch": [
                    {"sql": s.sql, "params": _coerce_params(s.params)} for s in chunk
                ]
            }
            envelope = await self._post_json(
                "/query",
                payload,
                sql_for_error=chunk[0].sql,
                params_for_error=chunk[0].params,
            )
            results.extend(self._unwrap_batch(envelope, statements=chunk))
        return results

    # ---- internal HTTP ----

    async def _post_json(
        self,
        path: str,
        body: object,
        *,
        sql_for_error: str,
        params_for_error: Sequence[object] | None,
    ) -> D1Envelope:
        """POST JSON to ``path`` with retry on transient/5xx; raise typed
        exception on hard failures.

        ``sql_for_error`` / ``params_for_error`` are propagated into
        :class:`D1QueryError` for debugging, not used to construct the
        request.
        """
        if self._client is None:
            raise D1Error("D1Client used outside of `async with` block")

        url = f"/accounts/{self._account_id}/d1/database/{self._database_id}{path}"

        last_transport_exc: Exception | None = None
        for attempt in range(1, _MAX_NETWORK_ATTEMPTS + 1):
            try:
                response = await self._client.post(url, json=body)
            except httpx.TimeoutException as exc:
                last_transport_exc = exc
                if attempt < _MAX_NETWORK_ATTEMPTS:
                    await self._sleep(_backoff(attempt))
                    continue
                raise D1TransportError(
                    f"D1 request timed out after {_MAX_NETWORK_ATTEMPTS} attempts: {exc}"
                ) from exc
            except httpx.RequestError as exc:
                last_transport_exc = exc
                if attempt < _MAX_NETWORK_ATTEMPTS:
                    await self._sleep(_backoff(attempt))
                    continue
                raise D1TransportError(
                    f"D1 transport error after {_MAX_NETWORK_ATTEMPTS} attempts: {exc}"
                ) from exc

            status = response.status_code

            if status == 200:
                return _parse_envelope(response, sql_for_error, params_for_error)

            if status in (401, 403):
                raise D1AuthError(
                    f"D1 authentication failed (HTTP {status}); "
                    "check CLOUDFLARE_D1_API_TOKEN scopes."
                )

            if status == 404:
                raise D1NotFoundError(
                    "D1 endpoint returned 404; "
                    "check CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_D1_DATABASE_ID."
                )

            if status == 429:
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                # Honor Retry-After when it's set; otherwise use our default.
                if attempt < _MAX_NETWORK_ATTEMPTS:
                    await self._sleep(retry_after or _DEFAULT_429_BACKOFF_S)
                    continue
                raise D1RateLimitError(
                    f"D1 rate limit hit after {_MAX_NETWORK_ATTEMPTS} attempts",
                    retry_after_s=retry_after,
                )

            if 500 <= status < 600:
                if attempt < _MAX_NETWORK_ATTEMPTS:
                    await self._sleep(_backoff(attempt))
                    continue
                raise D1TransportError(
                    f"D1 returned {status} after {_MAX_NETWORK_ATTEMPTS} attempts: "
                    f"{_truncate(response.text)}"
                )

            # Other 4xx — not retried. Surface as a query error so the caller
            # gets the SQL context, not just a status code.
            raise D1QueryError(
                f"D1 returned HTTP {status}: {_truncate(response.text)}",
                code=None,
                sql=sql_for_error,
                params=params_for_error,
            )

        # Should be unreachable — every loop iteration either returns,
        # continues, or raises. Defensive code path for type-checker happiness.
        raise D1TransportError(
            f"D1 request loop exhausted unexpectedly (last error: {last_transport_exc})"
        )

    # ---- envelope unwrapping ----

    def _unwrap_single(
        self,
        envelope: D1Envelope,
        *,
        sql: str,
        params: Sequence[object] | None,
    ) -> D1Result:
        if not envelope.success or not envelope.result:
            self._raise_query_error(envelope, sql=sql, params=params)
        first = envelope.result[0]
        if not first.success:
            self._raise_query_error(envelope, sql=sql, params=params)
        return first

    def _unwrap_batch(
        self,
        envelope: D1Envelope,
        *,
        statements: Sequence[D1Statement],
    ) -> list[D1Result]:
        if not envelope.success:
            # Identify which statement failed for the error message; D1's
            # response doesn't index errors, so we fall back to the first
            # not-success result if any, else the first statement.
            failing_index = next(
                (i for i, r in enumerate(envelope.result) if not r.success),
                0,
            )
            failing = statements[failing_index]
            self._raise_query_error(envelope, sql=failing.sql, params=failing.params)
        if len(envelope.result) != len(statements):
            raise D1QueryError(
                f"D1 batch returned {len(envelope.result)} results for "
                f"{len(statements)} statements",
                code=None,
                sql=statements[0].sql,
                params=statements[0].params,
            )
        for i, r in enumerate(envelope.result):
            if not r.success:
                self._raise_query_error(
                    envelope, sql=statements[i].sql, params=statements[i].params
                )
        return list(envelope.result)

    def _raise_query_error(
        self,
        envelope: D1Envelope,
        *,
        sql: str,
        params: Sequence[object] | None,
    ) -> None:
        code = envelope.errors[0].code if envelope.errors else None
        message = (
            envelope.errors[0].message
            if envelope.errors
            else "D1 reported failure with no error message"
        )
        raise D1QueryError(message, code=code, sql=sql, params=params)


# ----------------------------- utilities --------------------------------------


def _chunked(seq: Sequence[D1Statement], n: int) -> list[Sequence[D1Statement]]:
    """Split ``seq`` into contiguous chunks of length ``n`` (last may be shorter)."""
    if n < 1:
        raise ValueError(f"chunk size must be >= 1, got {n}")
    return [seq[i : i + n] for i in range(0, len(seq), n)]


def _backoff(attempt: int) -> float:
    """Exponential backoff with full jitter, clamped to ``_MAX_BACKOFF_S``."""
    base = min(_INITIAL_BACKOFF_S * (2 ** (attempt - 1)), _MAX_BACKOFF_S)
    return random.uniform(0.0, base)


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a numeric ``Retry-After`` header. HTTP-date variants are
    ignored — D1 always returns seconds in practice, and a fallback
    default already covers the unknown case.
    """
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds < 0:
        return None
    return seconds


def _parse_envelope(
    response: httpx.Response,
    sql_for_error: str,
    params_for_error: Sequence[object] | None,
) -> D1Envelope:
    """Decode ``response`` into a :class:`D1Envelope`, mapping JSON / shape
    errors to :class:`D1QueryError` so callers don't have to handle raw
    pydantic / json exceptions.
    """
    try:
        data = response.json()
    except ValueError as exc:
        raise D1QueryError(
            f"D1 returned non-JSON body: {_truncate(response.text)}",
            code=None,
            sql=sql_for_error,
            params=params_for_error,
        ) from exc
    try:
        return D1Envelope.model_validate(data)
    except ValueError as exc:
        # ValidationError is a ValueError in pydantic v2.
        raise D1QueryError(
            f"D1 response did not match expected envelope: {exc}",
            code=None,
            sql=sql_for_error,
            params=params_for_error,
        ) from exc


def _truncate(text: str, limit: int = 500) -> str:
    """Trim long response bodies for error messages."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"... ({len(text) - limit} bytes truncated)"
