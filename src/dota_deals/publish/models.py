"""Wire-format Pydantic models for published JSON payloads.

Distinct from the internal domain models in :mod:`dota_deals.models.domain`
because the wire contract has different concerns: dates as ``"Z"`` strings,
prices as USD strings, ``schema_version`` for forward compatibility. The
builder layer is the only place that converts between the two.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

type PipelineStatus = Literal["operational", "degraded", "warmup"]


# ----------------------------- price helpers ----------------------------------


def cents_to_usd_string(cents: int) -> str:
    """Format integer cents as a USD string with two decimals.

    Examples (also covered by tests):

    * ``0`` → ``"0.00"``
    * ``99`` → ``"0.99"``
    * ``100`` → ``"1.00"``
    * ``100099`` → ``"1000.99"``

    :raises ValueError: if ``cents`` is negative.
    """
    if cents < 0:
        raise ValueError(f"cents must be >= 0, got {cents}")
    return f"{cents // 100}.{cents % 100:02d}"


def iso_utc(at: datetime) -> str:
    """ISO 8601 with a ``Z`` suffix. Requires a UTC-aware datetime."""
    if at.tzinfo is None or at.utcoffset() != UTC.utcoffset(at):
        raise ValueError(f"iso_utc requires UTC-aware datetime, got {at!r}")
    return at.isoformat().replace("+00:00", "Z")


# ----------------------------- shared structures ------------------------------


class WireDataQuality(BaseModel):
    """Run-level data_quality block surfaced on every report."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ingest_status: str  # "success" | "partial" | "failed" | "missing"
    ingest_run_id: str | None = None
    missing_items: list[str] = Field(default_factory=list)


class WireScoreComponents(BaseModel):
    """The four signal values that fed a score. Nulls included."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    price_zscore: float | None
    supply_velocity: float | None
    event_proximity: float | None
    comparables_delta: float | None


class WireScore(BaseModel):
    """One scored item, complete with metadata the frontend needs to render."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: int
    market_hash_name: str
    name: str
    category: str  # "arcana" | "immortal"
    hero: str | None
    current_price: str | None  # USD string or null if no observation
    computed_for: date
    buy_score: float
    components: WireScoreComponents
    explanation: str
    null_signals: list[str] = Field(default_factory=list)


# ----------------------------- report payloads --------------------------------


class LatestReport(BaseModel):
    """``public/data/latest.json`` — the most recent scored date's top-N.

    When the pipeline is in warmup (no scores yet), this is still emitted
    with ``status="warmup"`` and ``scores=[]`` so the frontend can render
    a proper empty state.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    generated_at: datetime
    report_date: date | None
    status: PipelineStatus
    data_quality: WireDataQuality
    scores: list[WireScore]


class HistoricalReport(BaseModel):
    """``public/data/history/YYYY-MM-DD.json`` — frozen snapshot for one date."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    generated_at: datetime
    report_date: date
    data_quality: WireDataQuality
    scores: list[WireScore]


class WireRunRef(BaseModel):
    """Compact pointer to a row in the ``runs`` table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    kind: str  # "ingest" | "universe" | "signals" | "scoring" | "notify"
    finished_at: datetime | None
    status: str  # "success" | "partial" | "failed" | "running"


class WireDataCoverage(BaseModel):
    """Pipeline-level data coverage snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    items_tracked: int
    items_with_signals: int
    days_of_history: int
    first_observation_at: datetime | None


class WireWarmupEstimate(BaseModel):
    """How long until the cold-start warmup is over.

    ``days_remaining=None`` means the pipeline is past the 30-day
    ``price_zscore`` minimum (the longest of the four signal warmups —
    once that's satisfied, every signal is computable in principle).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    days_remaining: int | None


class Health(BaseModel):
    """``public/data/health.json`` — operational status for the frontend banner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    generated_at: datetime
    status: PipelineStatus
    last_run: WireRunRef | None
    data_coverage: WireDataCoverage
    warmup_estimate: WireWarmupEstimate


# ----------------------------- item detail ------------------------------------


class WirePricePoint(BaseModel):
    """One day's median lowest price."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    lowest_price: str  # USD string


class WireListingPoint(BaseModel):
    """One observation of listing count.

    Listings are point-in-time rather than per-day-aggregated; ``observed_at``
    carries the actual poll timestamp.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at: datetime
    listings_count: int


class WireSignalPoint(BaseModel):
    """One day's value for one signal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    value: float | None


class WireSignalSeries(BaseModel):
    """All recent values for a single named signal."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    signal_name: str
    points: list[WireSignalPoint]


class ItemDetail(BaseModel):
    """``public/data/items/<item_id>.json`` — full picture of one item."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: int = 1
    generated_at: datetime
    item_id: int
    market_hash_name: str
    name: str
    category: str
    hero: str | None
    active: bool
    daily_prices: list[WirePricePoint]
    listings: list[WireListingPoint]
    signals: list[WireSignalSeries]
