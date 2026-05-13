"""Tests for :mod:`dota_deals.publish.models`.

The wire-format contract is what the frontend ships against, so the
boundary-value edge cases (0 cents, 99 cents, 100 cents, large amounts)
get their own assertions rather than being implicit.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from dota_deals.publish.models import (
    Health,
    HistoricalReport,
    LatestReport,
    WireDataCoverage,
    WireDataQuality,
    WireScore,
    WireScoreComponents,
    WireWarmupEstimate,
    cents_to_usd_string,
    iso_utc,
)

# ----------------------------- cents_to_usd_string ----------------------------


@pytest.mark.parametrize(
    "cents,expected",
    [
        (0, "0.00"),
        (1, "0.01"),
        (99, "0.99"),
        (100, "1.00"),
        (101, "1.01"),
        (12345, "123.45"),
        (100099, "1000.99"),
        (1_000_000, "10000.00"),
    ],
)
def test_cents_to_usd_string_boundary_values(cents: int, expected: str) -> None:
    assert cents_to_usd_string(cents) == expected


def test_cents_to_usd_string_rejects_negative() -> None:
    with pytest.raises(ValueError):
        cents_to_usd_string(-1)


# ----------------------------- iso_utc ----------------------------------------


def test_iso_utc_strips_offset_to_z() -> None:
    dt = datetime(2026, 5, 13, 20, 0, 0, tzinfo=UTC)
    assert iso_utc(dt) == "2026-05-13T20:00:00Z"


def test_iso_utc_rejects_naive_datetime() -> None:
    naive = datetime(2026, 5, 13, 20, 0, 0)
    with pytest.raises(ValueError):
        iso_utc(naive)


# ----------------------------- model round-trips ------------------------------


def _components() -> WireScoreComponents:
    return WireScoreComponents(
        price_zscore=0.5,
        supply_velocity=0.4,
        event_proximity=None,
        comparables_delta=0.2,
    )


def _wire_score(item_id: int = 42) -> WireScore:
    return WireScore(
        item_id=item_id,
        market_hash_name="Inscribed Manifold Paradox",
        name="Inscribed Manifold Paradox",
        category="arcana",
        hero=None,
        current_price="34.50",
        computed_for=date(2026, 5, 13),
        buy_score=0.62,
        components=_components(),
        explanation="Priced below recent baseline",
        null_signals=["event_proximity"],
    )


def test_latest_report_serializes_null_fields() -> None:
    """Nulls must be present in serialized output, not omitted."""
    report = LatestReport(
        schema_version=1,
        generated_at=datetime(2026, 5, 13, 20, 0, tzinfo=UTC),
        report_date=date(2026, 5, 13),
        status="operational",
        data_quality=WireDataQuality(ingest_status="success"),
        scores=[_wire_score()],
    )
    dumped = report.model_dump(mode="json")
    # Nullable field with a value
    assert "report_date" in dumped
    # The component with null value is present
    assert "event_proximity" in dumped["scores"][0]["components"]
    assert dumped["scores"][0]["components"]["event_proximity"] is None
    # Optional WireScore.hero is present
    assert dumped["scores"][0]["hero"] is None


def test_latest_report_warmup_envelope() -> None:
    """The warmup envelope: report_date null, status warmup, scores empty."""
    report = LatestReport(
        schema_version=1,
        generated_at=datetime(2026, 5, 13, 20, 0, tzinfo=UTC),
        report_date=None,
        status="warmup",
        data_quality=WireDataQuality(ingest_status="missing"),
        scores=[],
    )
    dumped = report.model_dump(mode="json")
    assert dumped["report_date"] is None
    assert dumped["status"] == "warmup"
    assert dumped["scores"] == []


def test_historical_report_requires_concrete_report_date() -> None:
    """HistoricalReport (unlike LatestReport) must have a concrete date."""
    report = HistoricalReport(
        schema_version=1,
        generated_at=datetime(2026, 5, 13, 20, 0, tzinfo=UTC),
        report_date=date(2026, 5, 13),
        data_quality=WireDataQuality(ingest_status="success"),
        scores=[_wire_score()],
    )
    assert report.report_date == date(2026, 5, 13)


def test_health_warmup_estimate_none_when_past_threshold() -> None:
    health = Health(
        schema_version=1,
        generated_at=datetime(2026, 5, 13, 20, 0, tzinfo=UTC),
        status="operational",
        last_run=None,
        data_coverage=WireDataCoverage(
            items_tracked=100,
            items_with_signals=100,
            days_of_history=45,
            first_observation_at=datetime(2026, 3, 29, tzinfo=UTC),
        ),
        warmup_estimate=WireWarmupEstimate(days_remaining=None),
    )
    dumped = health.model_dump(mode="json")
    assert dumped["warmup_estimate"]["days_remaining"] is None
