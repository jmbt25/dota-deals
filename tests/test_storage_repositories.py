"""Tests for :mod:`dota_deals.storage.repositories`.

The sync layer is exercised indirectly through the signal / scoring /
ingest test suites; the async layer doesn't have callers yet (Phase 9c
flips them over), so we test it directly here. Coverage targets:

* Per-function parity with the sync surface: upsert semantics,
  idempotency on PK collision, RETURNING-clause behavior.
* The new batch-write functions return the same per-statement count
  story as their single-row counterparts (no row was written twice,
  re-runs return 0).
* The new bulk-read functions return ``{item_id: []}`` for items with
  no rows in the window (never omit the key).
* Python median replaces the SQLite ``MEDIAN`` aggregate without
  changing the per-day result for odd-count, even-count, and
  flat-price cases.

All tests run against :class:`D1FakeClient` via ``connect(backend=...)``
— same wiring future repository callers will use. The fake's schema
comes from ``migrations/0001_initial.sql``, so every CHECK constraint
in the production schema is in force here too.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from dota_deals.config import Settings
from dota_deals.models.domain import (
    BuyScore,
    Item,
    ListingPoint,
    PricePoint,
    RunSummary,
    Signal,
)
from dota_deals.models.events import EventRecord
from dota_deals.storage import repositories as repo
from dota_deals.storage.db import D1Connection, IntegrityViolation, connect
from tests._d1_fake import D1FakeClient

# ----------------------------- fixtures ---------------------------------------


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        db_path=tmp_path / "x.db",
        cloudflare_account_id="acct",
        cloudflare_d1_database_id="db",
        cloudflare_d1_api_token="tok",
    )


@pytest.fixture
async def conn(tmp_path: Path) -> AsyncIterator[D1Connection]:
    """Yield a fresh :class:`D1Connection` backed by an in-memory fake."""
    settings = _settings(tmp_path)
    async with D1FakeClient() as fake:
        async with connect(settings, backend=fake) as c:
            yield c


def _make_item(market_hash: str, *, category: str = "arcana") -> Item:
    return Item(
        item_id=0,  # ignored by upsert_item
        market_hash=market_hash,
        name=market_hash,
        category=category,  # type: ignore[arg-type]
        hero=None,
        first_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        last_seen_at=datetime(2026, 1, 1, tzinfo=UTC),
        active=True,
    )


# ----------------------------- items ------------------------------------------


@pytest.mark.asyncio
async def test_upsert_item_inserts_new_row(conn: D1Connection) -> None:
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    assert item_id > 0
    got = await repo.get_item_by_id(conn, item_id)
    assert got is not None
    assert got.market_hash == "Alpha"
    assert got.active is True


@pytest.mark.asyncio
async def test_upsert_item_returns_same_id_on_conflict(conn: D1Connection) -> None:
    first = await repo.upsert_item(conn, _make_item("Alpha"))
    second = await repo.upsert_item(conn, _make_item("Alpha"))
    assert first == second


@pytest.mark.asyncio
async def test_upsert_item_reactivates(conn: D1Connection) -> None:
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    await repo.set_item_active(conn, item_id, active=False)
    await repo.upsert_item(conn, _make_item("Alpha"))
    got = await repo.get_item_by_id(conn, item_id)
    assert got is not None
    assert got.active is True


@pytest.mark.asyncio
async def test_get_item_by_hash_missing_returns_none(conn: D1Connection) -> None:
    assert await repo.get_item_by_hash(conn, "Nope") is None


@pytest.mark.asyncio
async def test_upsert_items_batch_inserts_and_reactivates(conn: D1Connection) -> None:
    """Batch insert reuses ON CONFLICT to reactivate previously-deactivated items.

    The universe runner relies on this property: items that disappear are
    deactivated by ingest's strike rule, but if they reappear in the
    next universe refresh the same upsert path resets ``active=1`` and
    zeros the strike counter.
    """
    a = await repo.upsert_item(conn, _make_item("A"))
    await repo.set_item_active(conn, a, active=False)
    # Bump the strike counter so we can verify it resets to 0.
    await conn.execute("UPDATE items SET consecutive_ingest_4xx = 3 WHERE item_id = ?", (a,))

    items = [_make_item("A"), _make_item("B"), _make_item("C")]
    changes = await repo.upsert_items_batch(conn, items)
    assert changes >= 3  # 1 update + 2 inserts

    after = {i.market_hash: i for i in await repo.active_items(conn)}
    assert set(after) == {"A", "B", "C"}
    assert after["A"].active is True
    assert after["A"].consecutive_ingest_4xx == 0


@pytest.mark.asyncio
async def test_upsert_items_batch_empty_returns_zero(conn: D1Connection) -> None:
    assert await repo.upsert_items_batch(conn, []) == 0


@pytest.mark.asyncio
async def test_active_items_filters_and_orders(conn: D1Connection) -> None:
    a = await repo.upsert_item(conn, _make_item("Alpha"))
    b = await repo.upsert_item(conn, _make_item("Beta"))
    c = await repo.upsert_item(conn, _make_item("Gamma"))
    await repo.set_item_active(conn, b, active=False)
    items = await repo.active_items(conn)
    assert [i.item_id for i in items] == [a, c]


@pytest.mark.asyncio
async def test_active_items_in_category_excludes_self(conn: D1Connection) -> None:
    a = await repo.upsert_item(conn, _make_item("A", category="arcana"))
    b = await repo.upsert_item(conn, _make_item("B", category="arcana"))
    await repo.upsert_item(conn, _make_item("I", category="immortal"))
    items = await repo.active_items_in_category(conn, "arcana", exclude_item_id=a)
    assert [i.item_id for i in items] == [b]


@pytest.mark.asyncio
async def test_strike_increment_and_reset(conn: D1Connection) -> None:
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    assert await repo.increment_ingest_strikes(conn, item_id) == 1
    assert await repo.increment_ingest_strikes(conn, item_id) == 2
    await repo.reset_ingest_strikes(conn, item_id)
    got = await repo.get_item_by_id(conn, item_id)
    assert got is not None
    assert got.consecutive_ingest_4xx == 0


# ----------------------------- price_history ----------------------------------


@pytest.mark.asyncio
async def test_insert_price_point_idempotent(conn: D1Connection) -> None:
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    point = PricePoint(
        item_id=item_id,
        observed_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
        lowest_cents=345,
    )
    assert await repo.insert_price_point(conn, point) is True
    assert await repo.insert_price_point(conn, point) is False


@pytest.mark.asyncio
async def test_insert_price_points_batch(conn: D1Connection) -> None:
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    points = [
        PricePoint(
            item_id=item_id,
            observed_at=datetime(2026, 5, 1, h, 0, tzinfo=UTC),
            lowest_cents=345 + h,
        )
        for h in (0, 8, 16)
    ]
    inserted = await repo.insert_price_points(conn, points)
    assert inserted == 3
    # Re-running the same batch is idempotent: PK conflict on every row.
    again = await repo.insert_price_points(conn, points)
    assert again == 0


@pytest.mark.asyncio
async def test_recent_prices_filters_by_date(conn: D1Connection) -> None:
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    points = [
        PricePoint(
            item_id=item_id,
            observed_at=datetime(2026, 5, d, 0, 0, tzinfo=UTC),
            lowest_cents=100 + d,
        )
        for d in range(1, 6)
    ]
    await repo.insert_price_points(conn, points)
    series = await repo.recent_prices(conn, item_id, days=3, as_of=date(2026, 5, 5))
    # [as_of - days + 1, as_of] = [May 3, May 5]
    assert [p.observed_at.day for p in series] == [3, 4, 5]


@pytest.mark.asyncio
async def test_daily_prices_python_median_odd_count(conn: D1Connection) -> None:
    """3 observations → median is the middle value."""
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    await repo.insert_price_points(
        conn,
        [
            PricePoint(
                item_id=item_id,
                observed_at=datetime(2026, 5, 1, h, 0, tzinfo=UTC),
                lowest_cents=cents,
            )
            for h, cents in ((0, 100), (8, 200), (16, 300))
        ],
    )
    series = await repo.daily_prices(conn, item_id, days=1, as_of=date(2026, 5, 1))
    assert series == [(date(2026, 5, 1), 200)]


@pytest.mark.asyncio
async def test_daily_prices_python_median_even_count_integer_floor(
    conn: D1Connection,
) -> None:
    """4 observations → integer-floor of the two middles, matching the
    legacy MEDIAN aggregate. Critical for downstream signal arithmetic
    not to drift on float rounding."""
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    await repo.insert_price_points(
        conn,
        [
            PricePoint(
                item_id=item_id,
                observed_at=datetime(2026, 5, 1, h, 0, tzinfo=UTC),
                lowest_cents=cents,
            )
            for h, cents in ((0, 100), (4, 201), (8, 300), (12, 400))
        ],
    )
    # sorted [100, 201, 300, 400] → middles 201, 300 → (201+300)//2 = 250
    series = await repo.daily_prices(conn, item_id, days=1, as_of=date(2026, 5, 1))
    assert series == [(date(2026, 5, 1), 250)]


@pytest.mark.asyncio
async def test_daily_prices_for_items_includes_empty_keys(conn: D1Connection) -> None:
    """An item with no observations in the window still appears in the
    output dict (as ``[]``) so the DataLookup caller doesn't have to
    test for membership before indexing."""
    a = await repo.upsert_item(conn, _make_item("A"))
    b = await repo.upsert_item(conn, _make_item("B"))
    await repo.insert_price_points(
        conn,
        [
            PricePoint(
                item_id=a,
                observed_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                lowest_cents=100,
            )
        ],
    )
    out = await repo.daily_prices_for_items(conn, [a, b], days=1, as_of=date(2026, 5, 1))
    assert out[a] == [(date(2026, 5, 1), 100)]
    assert out[b] == []


# ----------------------------- listing_history --------------------------------


@pytest.mark.asyncio
async def test_insert_listing_point_idempotent(conn: D1Connection) -> None:
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    point = ListingPoint(
        item_id=item_id,
        observed_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
        listings_count=12,
    )
    assert await repo.insert_listing_point(conn, point) is True
    assert await repo.insert_listing_point(conn, point) is False


@pytest.mark.asyncio
async def test_insert_listing_points_batch(conn: D1Connection) -> None:
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    points = [
        ListingPoint(
            item_id=item_id,
            observed_at=datetime(2026, 5, 1, h, 0, tzinfo=UTC),
            listings_count=10 + h,
        )
        for h in (0, 8, 16)
    ]
    assert await repo.insert_listing_points(conn, points) == 3


@pytest.mark.asyncio
async def test_recent_listings_for_items(conn: D1Connection) -> None:
    a = await repo.upsert_item(conn, _make_item("A"))
    b = await repo.upsert_item(conn, _make_item("B"))
    await repo.insert_listing_points(
        conn,
        [
            ListingPoint(
                item_id=a,
                observed_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                listings_count=5,
            )
        ],
    )
    out = await repo.recent_listings_for_items(conn, [a, b], days=1, as_of=date(2026, 5, 1))
    assert len(out[a]) == 1
    assert out[b] == []


# ----------------------------- latest_observation -----------------------------


@pytest.mark.asyncio
async def test_upsert_latest_observation_keeps_newest(conn: D1Connection) -> None:
    """Older observed_at must not overwrite a newer cached row."""
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    newer = PricePoint(
        item_id=item_id,
        observed_at=datetime(2026, 5, 10, 0, 0, tzinfo=UTC),
        lowest_cents=500,
    )
    older = PricePoint(
        item_id=item_id,
        observed_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
        lowest_cents=100,
    )
    await repo.upsert_latest_observation(conn, newer, listings_count=10)
    await repo.upsert_latest_observation(conn, older, listings_count=99)
    snap = await repo.latest_observations_all(conn)
    assert snap[item_id].lowest_cents == 500
    assert snap[item_id].listings_count == 10


@pytest.mark.asyncio
async def test_upsert_latest_observations_batch(conn: D1Connection) -> None:
    a = await repo.upsert_item(conn, _make_item("A"))
    b = await repo.upsert_item(conn, _make_item("B"))
    pairs = [
        (
            PricePoint(
                item_id=a,
                observed_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                lowest_cents=100,
            ),
            5,
        ),
        (
            PricePoint(
                item_id=b,
                observed_at=datetime(2026, 5, 1, 0, 0, tzinfo=UTC),
                lowest_cents=200,
            ),
            None,
        ),
    ]
    await repo.upsert_latest_observations(conn, pairs)
    snap = await repo.latest_observations_all(conn)
    assert snap[a].lowest_cents == 100
    assert snap[a].listings_count == 5
    assert snap[b].listings_count is None


# ----------------------------- events -----------------------------------------


@pytest.mark.asyncio
async def test_insert_event_and_lookups(conn: D1Connection) -> None:
    ti25 = EventRecord(
        event_id=0,
        kind="ti",
        name="The International 2025",
        start_date=date(2025, 9, 1),
        end_date=None,
        confidence="confirmed",
        notes=None,
    )
    ti26 = EventRecord(
        event_id=0,
        kind="ti",
        name="The International 2026",
        start_date=date(2026, 8, 15),
        end_date=None,
        confidence="confirmed",
        notes=None,
    )
    await repo.insert_event(conn, ti25)
    await repo.insert_event(conn, ti26)

    nxt = await repo.next_event_within(conn, date(2026, 7, 1), days_window=60)
    assert nxt is not None
    assert nxt.start_date == date(2026, 8, 15)

    past = await repo.past_events_of_kind(conn, "ti", before=date(2026, 7, 1))
    assert [e.start_date for e in past] == [date(2025, 9, 1)]


@pytest.mark.asyncio
async def test_next_event_within_returns_none_when_outside_window(
    conn: D1Connection,
) -> None:
    await repo.insert_event(
        conn,
        EventRecord(
            event_id=0,
            kind="ti",
            name="TI",
            start_date=date(2030, 1, 1),
            end_date=None,
            confidence="confirmed",
            notes=None,
        ),
    )
    assert await repo.next_event_within(conn, date(2026, 5, 1), days_window=60) is None


# ----------------------------- signals ----------------------------------------


@pytest.mark.asyncio
async def test_insert_signal_idempotent(conn: D1Connection) -> None:
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    signal = Signal(
        item_id=item_id,
        computed_for=date(2026, 5, 1),
        signal_name="price_zscore",
        value=0.5,
    )
    assert await repo.insert_signal(conn, signal) is True
    assert await repo.insert_signal(conn, signal) is False


@pytest.mark.asyncio
async def test_insert_signals_batch_and_signals_for(conn: D1Connection) -> None:
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    signals = [
        Signal(
            item_id=item_id,
            computed_for=date(2026, 5, 1),
            signal_name=name,  # type: ignore[arg-type]
            value=0.1 * i,
        )
        for i, name in enumerate(
            ["price_zscore", "supply_velocity", "event_proximity", "comparables_delta"]
        )
    ]
    assert await repo.insert_signals(conn, signals) == 4
    got = await repo.signals_for(conn, item_id, date(2026, 5, 1))
    assert [s.signal_name for s in got] == [
        "comparables_delta",
        "event_proximity",
        "price_zscore",
        "supply_velocity",
    ]  # sorted by signal_name


@pytest.mark.asyncio
async def test_signal_null_value_persists(conn: D1Connection) -> None:
    """A signal with value=None (insufficient history) must persist as
    SQL NULL, not be coerced to 0.0 or rejected. The metadata records
    why so the publish layer can render the reason."""
    item_id = await repo.upsert_item(conn, _make_item("Alpha"))
    await repo.insert_signal(
        conn,
        Signal(
            item_id=item_id,
            computed_for=date(2026, 5, 1),
            signal_name="price_zscore",
            value=None,
            metadata={"reason": "insufficient_history"},
        ),
    )
    got = await repo.signals_for(conn, item_id, date(2026, 5, 1))
    assert len(got) == 1
    assert got[0].value is None
    assert got[0].metadata == {"reason": "insufficient_history"}


@pytest.mark.asyncio
async def test_signals_for_items_on_date_bulk(conn: D1Connection) -> None:
    """Bulk read returns ``{item_id: list[Signal]}`` for every requested id,
    with ``[]`` for items with no signals on the date (empty-list contract
    matches the other bulk reads).
    """
    a = await repo.upsert_item(conn, _make_item("A"))
    b = await repo.upsert_item(conn, _make_item("B"))
    c = await repo.upsert_item(conn, _make_item("C"))
    on = date(2026, 5, 1)
    other = date(2026, 5, 2)

    await repo.insert_signals(
        conn,
        [
            Signal(item_id=a, computed_for=on, signal_name="price_zscore", value=0.5),
            Signal(item_id=a, computed_for=on, signal_name="supply_velocity", value=0.3),
            # b has signals but only on a different date → bulk read for `on` returns [].
            Signal(item_id=b, computed_for=other, signal_name="price_zscore", value=0.1),
            # c has none.
        ],
    )

    out = await repo.signals_for_items_on_date(conn, [a, b, c], on)
    assert [s.signal_name for s in out[a]] == ["price_zscore", "supply_velocity"]
    assert out[b] == []
    assert out[c] == []


@pytest.mark.asyncio
async def test_signals_for_items_on_date_empty_input(conn: D1Connection) -> None:
    """Empty item_ids → empty dict, no query issued."""
    out = await repo.signals_for_items_on_date(conn, [], date(2026, 5, 1))
    assert out == {}


# ----------------------------- scores -----------------------------------------


def _score(item_id: int, *, value: float, on: date = date(2026, 5, 1)) -> BuyScore:
    return BuyScore(
        item_id=item_id,
        computed_for=on,
        score=value,
        components={
            "price_zscore": 0.5,
            "supply_velocity": None,
            "event_proximity": None,
            "comparables_delta": 0.2,
        },
        explanation="testing",
    )


@pytest.mark.asyncio
async def test_insert_score_idempotent(conn: D1Connection) -> None:
    a = await repo.upsert_item(conn, _make_item("A"))
    score = _score(a, value=0.3)
    assert await repo.insert_score(conn, score) is True
    assert await repo.insert_score(conn, score) is False


@pytest.mark.asyncio
async def test_latest_scores_orders_by_buy_score_desc(conn: D1Connection) -> None:
    a = await repo.upsert_item(conn, _make_item("A"))
    b = await repo.upsert_item(conn, _make_item("B"))
    c = await repo.upsert_item(conn, _make_item("C"))
    await repo.insert_scores(
        conn,
        [
            _score(a, value=0.1),
            _score(b, value=0.5),
            _score(c, value=0.3),
        ],
    )
    top = await repo.latest_scores(conn, date(2026, 5, 1), limit=10)
    assert [s.item_id for s in top] == [b, c, a]


@pytest.mark.asyncio
async def test_latest_scores_limit_zero_is_allowed(conn: D1Connection) -> None:
    a = await repo.upsert_item(conn, _make_item("A"))
    await repo.insert_score(conn, _score(a, value=0.5))
    assert await repo.latest_scores(conn, date(2026, 5, 1), limit=0) == []


# ----------------------------- runs / quarantine ------------------------------


def _run(run_id: str, kind: str = "ingest") -> RunSummary:
    return RunSummary(
        run_id=run_id,
        parent_run_id=None,
        kind=kind,  # type: ignore[arg-type]
        started_at=datetime(2026, 5, 1, 16, 0, tzinfo=UTC),
        finished_at=None,
        status="running",
    )


@pytest.mark.asyncio
async def test_insert_and_update_run(conn: D1Connection) -> None:
    run = _run("run-1")
    await repo.insert_run(conn, run)
    await repo.update_run(
        conn,
        "run-1",
        status="success",
        items_ok=10,
        items_quarantined=0,
        items_failed=0,
        notes="all good",
    )
    found = await repo.latest_ingest_run_for_date(conn, date(2026, 5, 1))
    assert found == ("run-1", "success")


@pytest.mark.asyncio
async def test_insert_run_duplicate_raises_integrity_violation(
    conn: D1Connection,
) -> None:
    run = _run("dup")
    await repo.insert_run(conn, run)
    with pytest.raises(IntegrityViolation):
        await repo.insert_run(conn, run)


@pytest.mark.asyncio
async def test_quarantine_record_persists(conn: D1Connection) -> None:
    """Smoke test that quarantine writes succeed; the actual schema
    constraint is enforced by the D1FakeClient's migrated schema."""
    await repo.quarantine_record(
        conn,
        run_id="run-x",
        source="steam_price_overview",
        item_hash="Foo",
        raw_payload='{"bogus": true}',
        error_type="ValidationError",
        error_message="missing price",
    )
    # Read back via raw query — we don't expose a typed reader because
    # nothing in the pipeline reads quarantine; it's an inspection table.
    result = await conn.query("SELECT count(*) AS n FROM quarantine")
    assert int(result.results[0]["n"]) == 1


# ----------------------------- items_missing_observation ----------------------


@pytest.mark.asyncio
async def test_items_missing_observation_for_date(conn: D1Connection) -> None:
    """Only active items missing a price row on the target date appear."""
    a = await repo.upsert_item(conn, _make_item("A"))
    b = await repo.upsert_item(conn, _make_item("B"))
    c = await repo.upsert_item(conn, _make_item("C"))
    await repo.set_item_active(conn, c, active=False)

    # A has a row on the target date; B and C do not. C is inactive →
    # excluded. Only B should surface.
    await repo.insert_price_point(
        conn,
        PricePoint(
            item_id=a,
            observed_at=datetime(2026, 5, 1, 16, 0, tzinfo=UTC),
            lowest_cents=100,
        ),
    )
    missing = await repo.items_missing_observation_for_date(conn, date(2026, 5, 1))
    assert missing == ["B"]
    # Avoid the unused-var warning while still keeping `b` named for clarity.
    assert b > 0
