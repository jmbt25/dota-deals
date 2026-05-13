"""Tests for :mod:`dota_deals.storage.d1_client`.

The D1 REST API is the only path from outside a Worker to a D1 database,
so this module is the single HTTP boundary the rest of the codebase will
sit on top of. The tests below pin the contract at that boundary:

* Construction-time validation of credentials.
* Happy-path single-statement query/execute, with bool→int coercion.
* Batch chunking across :attr:`Settings.d1_max_batch_size`.
* Each status-code branch (200, 401/403, 404, 429, 5xx, 4xx other).
* Network retries with a recorded sleep so timing assertions don't burn
  wall-clock time.
* Envelope-shape edge cases (success=False, non-JSON body, mismatched
  batch result count).

respx mocks the Cloudflare API base URL; a recording sleep replaces
:func:`asyncio.sleep` so retry intervals can be asserted without real
delays.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
import respx

from dota_deals.config import Settings
from dota_deals.storage.d1_client import (
    D1AuthError,
    D1Client,
    D1ConfigError,
    D1NotFoundError,
    D1QueryError,
    D1RateLimitError,
    D1Statement,
    D1TransportError,
)

_ACCOUNT = "test-account"
_DATABASE = "test-database"
_QUERY_URL = (
    f"https://api.cloudflare.com/client/v4/accounts/{_ACCOUNT}/d1/database/{_DATABASE}/query"
)


# ----------------------------- helpers ----------------------------------------


def _d1_settings(tmp_path: Path, *, max_batch_size: int = 100) -> Settings:
    """Settings populated with valid-shape D1 credentials.

    No real network calls are made; respx intercepts everything.
    """
    return Settings(
        db_path=tmp_path / "x.db",
        cloudflare_account_id=_ACCOUNT,
        cloudflare_d1_database_id=_DATABASE,
        cloudflare_d1_api_token="test-token",
        d1_timeout_s=2.0,
        d1_max_batch_size=max_batch_size,
    )


def _ok_envelope(results: list[dict[str, object]], *, changes: int = 0) -> dict[str, object]:
    """Build a successful single-statement D1 envelope."""
    return {
        "success": True,
        "errors": [],
        "messages": [],
        "result": [
            {
                "success": True,
                "meta": {
                    "changes": changes,
                    "last_row_id": 0,
                    "rows_read": len(results),
                    "rows_written": changes,
                    "duration": 0.5,
                },
                "results": results,
            }
        ],
    }


def _ok_batch_envelope(per_statement: list[list[dict[str, object]]]) -> dict[str, object]:
    """Build a successful batch envelope with one result entry per statement."""
    return {
        "success": True,
        "errors": [],
        "messages": [],
        "result": [
            {
                "success": True,
                "meta": {
                    "changes": 1,
                    "last_row_id": 0,
                    "rows_read": 0,
                    "rows_written": 1,
                    "duration": 0.1,
                },
                "results": rows,
            }
            for rows in per_statement
        ],
    }


def _make_recorded_sleep() -> tuple[Callable[[float], Awaitable[None]], list[float]]:
    """Same pattern as the Steam-client tests: record requested sleeps,
    yield with ``asyncio.sleep(0)`` so other coroutines can run.
    """
    calls: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        calls.append(seconds)
        await asyncio.sleep(0)

    return fake_sleep, calls


# ----------------------------- configuration ----------------------------------


# ``_env_file=None`` opts these construction-error tests out of .env
# loading. Without it, a developer who has CLOUDFLARE_* populated in
# their local .env (a normal state once the real-D1 setup is done) gets
# Settings filled in from the file and the "missing" field is no longer
# missing — false negative.


def test_missing_account_id_raises_config_error(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        db_path=tmp_path / "x.db",
        cloudflare_d1_database_id=_DATABASE,
        cloudflare_d1_api_token="t",
    )
    with pytest.raises(D1ConfigError) as exc:
        D1Client(settings)
    assert "CLOUDFLARE_ACCOUNT_ID" in str(exc.value)


def test_missing_database_id_raises_config_error(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        db_path=tmp_path / "x.db",
        cloudflare_account_id=_ACCOUNT,
        cloudflare_d1_api_token="t",
    )
    with pytest.raises(D1ConfigError) as exc:
        D1Client(settings)
    assert "CLOUDFLARE_D1_DATABASE_ID" in str(exc.value)


def test_missing_token_raises_config_error(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        db_path=tmp_path / "x.db",
        cloudflare_account_id=_ACCOUNT,
        cloudflare_d1_database_id=_DATABASE,
    )
    with pytest.raises(D1ConfigError) as exc:
        D1Client(settings)
    assert "CLOUDFLARE_D1_API_TOKEN" in str(exc.value)


# ----------------------------- happy path -------------------------------------


@pytest.mark.asyncio
async def test_query_returns_first_result(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    with respx.mock() as router:
        route = router.post(_QUERY_URL).mock(
            return_value=httpx.Response(
                200,
                json=_ok_envelope([{"item_id": 1, "market_hash": "Foo"}]),
            )
        )
        async with D1Client(settings) as client:
            result = await client.query("SELECT * FROM items WHERE item_id = ?", (1,))
        assert route.called
        sent = json.loads(route.calls[0].request.content)
        assert sent == {
            "sql": "SELECT * FROM items WHERE item_id = ?",
            "params": [1],
        }
        assert result.success is True
        assert result.results == [{"item_id": 1, "market_hash": "Foo"}]


@pytest.mark.asyncio
async def test_execute_returns_changes(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    with respx.mock() as router:
        router.post(_QUERY_URL).mock(
            return_value=httpx.Response(200, json=_ok_envelope([], changes=3))
        )
        async with D1Client(settings) as client:
            changes = await client.execute(
                "UPDATE items SET active = 0 WHERE category = ?", ("arcana",)
            )
        assert changes == 3


@pytest.mark.asyncio
async def test_authorization_header_carries_token(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    with respx.mock() as router:
        route = router.post(_QUERY_URL).mock(
            return_value=httpx.Response(200, json=_ok_envelope([]))
        )
        async with D1Client(settings) as client:
            await client.query("SELECT 1", ())
        auth = route.calls[0].request.headers["Authorization"]
        assert auth == "Bearer test-token"


@pytest.mark.asyncio
async def test_bool_params_coerced_to_int(tmp_path: Path) -> None:
    """D1's wire protocol doesn't accept native bools; the client coerces."""
    settings = _d1_settings(tmp_path)
    with respx.mock() as router:
        route = router.post(_QUERY_URL).mock(
            return_value=httpx.Response(200, json=_ok_envelope([]))
        )
        async with D1Client(settings) as client:
            await client.query("UPDATE items SET active = ?", (True,))
        sent = json.loads(route.calls[0].request.content)
        assert sent["params"] == [1]


# ----------------------------- batch ------------------------------------------


@pytest.mark.asyncio
async def test_batch_sends_all_statements_in_one_request(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    statements = [
        D1Statement(sql="INSERT INTO items (market_hash) VALUES (?)", params=("A",)),
        D1Statement(sql="INSERT INTO items (market_hash) VALUES (?)", params=("B",)),
        D1Statement(sql="INSERT INTO items (market_hash) VALUES (?)", params=("C",)),
    ]
    with respx.mock() as router:
        route = router.post(_QUERY_URL).mock(
            return_value=httpx.Response(200, json=_ok_batch_envelope([[], [], []]))
        )
        async with D1Client(settings) as client:
            results = await client.batch(statements)
        assert route.call_count == 1
        sent = json.loads(route.calls[0].request.content)
        assert isinstance(sent, list)
        assert len(sent) == 3
        assert sent[0] == {
            "sql": "INSERT INTO items (market_hash) VALUES (?)",
            "params": ["A"],
        }
        assert len(results) == 3


@pytest.mark.asyncio
async def test_batch_chunks_above_max_batch_size(tmp_path: Path) -> None:
    """A batch of 5 statements with max_batch_size=2 must produce 3
    sequential HTTP requests (sizes 2 + 2 + 1) and concatenate results in
    original order.
    """
    settings = _d1_settings(tmp_path, max_batch_size=2)
    statements = [
        D1Statement(sql="INSERT INTO items (market_hash) VALUES (?)", params=(h,))
        for h in ("A", "B", "C", "D", "E")
    ]
    with respx.mock() as router:
        route = router.post(_QUERY_URL).mock(
            side_effect=[
                httpx.Response(200, json=_ok_batch_envelope([[], []])),
                httpx.Response(200, json=_ok_batch_envelope([[], []])),
                httpx.Response(200, json=_ok_batch_envelope([[]])),
            ]
        )
        async with D1Client(settings) as client:
            results = await client.batch(statements)
        assert route.call_count == 3
        # Verify chunk shape: the second request carries the 3rd/4th SQL.
        body_two = json.loads(route.calls[1].request.content)
        assert [s["params"] for s in body_two] == [["C"], ["D"]]
        assert len(results) == 5


@pytest.mark.asyncio
async def test_batch_empty_does_not_call(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    with respx.mock(assert_all_called=False) as router:
        route = router.post(_QUERY_URL).mock(
            return_value=httpx.Response(200, json=_ok_batch_envelope([]))
        )
        async with D1Client(settings) as client:
            results = await client.batch([])
        assert results == []
        assert route.call_count == 0


# ----------------------------- error branches ---------------------------------


@pytest.mark.asyncio
async def test_401_raises_auth_error_no_retry(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    sleep, sleep_calls = _make_recorded_sleep()
    with respx.mock() as router:
        route = router.post(_QUERY_URL).mock(return_value=httpx.Response(401, json={"errors": []}))
        async with D1Client(settings, sleep=sleep) as client:
            with pytest.raises(D1AuthError):
                await client.query("SELECT 1", ())
        assert route.call_count == 1  # not retried
        assert sleep_calls == []


@pytest.mark.asyncio
async def test_403_raises_auth_error(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    with respx.mock() as router:
        router.post(_QUERY_URL).mock(return_value=httpx.Response(403))
        async with D1Client(settings) as client:
            with pytest.raises(D1AuthError):
                await client.query("SELECT 1", ())


@pytest.mark.asyncio
async def test_404_raises_not_found_error(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    with respx.mock() as router:
        router.post(_QUERY_URL).mock(return_value=httpx.Response(404))
        async with D1Client(settings) as client:
            with pytest.raises(D1NotFoundError):
                await client.query("SELECT 1", ())


@pytest.mark.asyncio
async def test_429_retries_with_retry_after(tmp_path: Path) -> None:
    """A 429 with Retry-After=2 should sleep 2 seconds, then succeed on retry."""
    settings = _d1_settings(tmp_path)
    sleep, sleep_calls = _make_recorded_sleep()
    with respx.mock() as router:
        router.post(_QUERY_URL).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "2"}),
                httpx.Response(200, json=_ok_envelope([])),
            ]
        )
        async with D1Client(settings, sleep=sleep) as client:
            result = await client.query("SELECT 1", ())
        assert result.success is True
        assert sleep_calls == [2.0]


@pytest.mark.asyncio
async def test_429_exhausts_raises_rate_limit_error(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    sleep, sleep_calls = _make_recorded_sleep()
    with respx.mock() as router:
        router.post(_QUERY_URL).mock(return_value=httpx.Response(429, headers={"Retry-After": "5"}))
        async with D1Client(settings, sleep=sleep) as client:
            with pytest.raises(D1RateLimitError) as exc_info:
                await client.query("SELECT 1", ())
        # 3 attempts total (initial + 2 retries that each slept).
        assert len(sleep_calls) == 2
        assert exc_info.value.retry_after_s == 5.0


@pytest.mark.asyncio
async def test_5xx_retried_then_succeeds(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    sleep, sleep_calls = _make_recorded_sleep()
    with respx.mock() as router:
        router.post(_QUERY_URL).mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(503),
                httpx.Response(200, json=_ok_envelope([{"x": 1}])),
            ]
        )
        async with D1Client(settings, sleep=sleep) as client:
            result = await client.query("SELECT 1", ())
        assert result.results == [{"x": 1}]
        assert len(sleep_calls) == 2  # slept between each retry


@pytest.mark.asyncio
async def test_5xx_exhausted_raises_transport_error(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    sleep, _ = _make_recorded_sleep()
    with respx.mock() as router:
        router.post(_QUERY_URL).mock(return_value=httpx.Response(500, text="boom"))
        async with D1Client(settings, sleep=sleep) as client:
            with pytest.raises(D1TransportError) as exc:
                await client.query("SELECT 1", ())
        assert "500" in str(exc.value)


@pytest.mark.asyncio
async def test_400_raises_query_error_no_retry(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    sleep, sleep_calls = _make_recorded_sleep()
    with respx.mock() as router:
        route = router.post(_QUERY_URL).mock(return_value=httpx.Response(400, text="bad sql"))
        async with D1Client(settings, sleep=sleep) as client:
            with pytest.raises(D1QueryError) as exc:
                await client.query("BOGUS SQL", ())
        assert exc.value.sql == "BOGUS SQL"
        assert route.call_count == 1
        assert sleep_calls == []


@pytest.mark.asyncio
async def test_timeout_retried_then_succeeds(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    sleep, sleep_calls = _make_recorded_sleep()
    with respx.mock() as router:
        router.post(_QUERY_URL).mock(
            side_effect=[
                httpx.TimeoutException("t1"),
                httpx.TimeoutException("t2"),
                httpx.Response(200, json=_ok_envelope([])),
            ]
        )
        async with D1Client(settings, sleep=sleep) as client:
            result = await client.query("SELECT 1", ())
        assert result.success is True
        assert len(sleep_calls) == 2


@pytest.mark.asyncio
async def test_timeout_exhausted_raises_transport_error(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    sleep, _ = _make_recorded_sleep()
    with respx.mock() as router:
        router.post(_QUERY_URL).mock(side_effect=httpx.TimeoutException("nope"))
        async with D1Client(settings, sleep=sleep) as client:
            with pytest.raises(D1TransportError):
                await client.query("SELECT 1", ())


# ----------------------------- envelope edge cases ----------------------------


@pytest.mark.asyncio
async def test_envelope_success_false_raises_query_error(tmp_path: Path) -> None:
    """200 OK but ``success=False`` is D1's signal that the SQL failed
    (e.g., constraint violation). Surface as D1QueryError with code + message.
    """
    settings = _d1_settings(tmp_path)
    body = {
        "success": False,
        "errors": [{"code": 7500, "message": "UNIQUE constraint failed: items.market_hash"}],
        "messages": [],
        "result": [],
    }
    with respx.mock() as router:
        router.post(_QUERY_URL).mock(return_value=httpx.Response(200, json=body))
        async with D1Client(settings) as client:
            with pytest.raises(D1QueryError) as exc:
                await client.query("INSERT INTO items VALUES (?)", ("dup",))
        assert exc.value.code == 7500
        assert "UNIQUE" in str(exc.value)
        assert exc.value.sql.startswith("INSERT INTO items")


@pytest.mark.asyncio
async def test_envelope_non_json_body_raises_query_error(tmp_path: Path) -> None:
    settings = _d1_settings(tmp_path)
    with respx.mock() as router:
        router.post(_QUERY_URL).mock(return_value=httpx.Response(200, text="<html>oops</html>"))
        async with D1Client(settings) as client:
            with pytest.raises(D1QueryError) as exc:
                await client.query("SELECT 1", ())
        assert "non-JSON" in str(exc.value)


@pytest.mark.asyncio
async def test_batch_mismatched_result_count_raises(tmp_path: Path) -> None:
    """D1 must return one result per statement; anything else is corruption
    we shouldn't paper over.
    """
    settings = _d1_settings(tmp_path)
    body = _ok_batch_envelope([[], []])  # 2 results for 3 statements below
    statements = [
        D1Statement(sql="INSERT INTO items (market_hash) VALUES (?)", params=("A",)),
        D1Statement(sql="INSERT INTO items (market_hash) VALUES (?)", params=("B",)),
        D1Statement(sql="INSERT INTO items (market_hash) VALUES (?)", params=("C",)),
    ]
    with respx.mock() as router:
        router.post(_QUERY_URL).mock(return_value=httpx.Response(200, json=body))
        async with D1Client(settings) as client:
            with pytest.raises(D1QueryError) as exc:
                await client.batch(statements)
        assert "2 results" in str(exc.value)
        assert "3 statements" in str(exc.value)
