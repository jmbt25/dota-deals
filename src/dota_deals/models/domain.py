"""Internal domain models.

All money-typed fields are ``int`` cents (USD). Conversion to display strings
happens only in the notifier layer.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

type Cents = int
type ItemCategory = Literal["arcana", "immortal"]
type SignalName = Literal[
    "price_zscore",
    "supply_velocity",
    "event_proximity",
    "comparables_delta",
]
type RunKind = Literal["ingest", "universe", "signals", "scoring", "notify"]
type RunStatus = Literal["running", "success", "partial", "failed"]


class Item(BaseModel):
    """An item tracked by the pipeline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: int
    market_hash: str
    name: str
    category: ItemCategory
    hero: str | None
    first_seen_at: datetime
    last_seen_at: datetime | None
    active: bool


class PricePoint(BaseModel):
    """A single price observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: int
    observed_at: datetime
    lowest_cents: Cents = Field(gt=0)
    median_cents: Cents | None = Field(default=None, gt=0)
    volume_24h: int | None = Field(default=None, ge=0)


class ListingPoint(BaseModel):
    """A single listing-count observation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: int
    observed_at: datetime
    listings_count: int = Field(ge=0)


class Signal(BaseModel):
    """A computed signal value for one item on one date."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: int
    computed_for: date
    signal_name: SignalName
    value: float | None
    metadata: dict[str, object] = Field(default_factory=dict)


class BuyScore(BaseModel):
    """A composite buy score with its component signals exposed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: int
    computed_for: date
    score: float
    components: dict[SignalName, float | None]
    explanation: str


class RunSummary(BaseModel):
    """Outcome of a single pipeline-stage execution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    parent_run_id: str | None
    kind: RunKind
    started_at: datetime
    finished_at: datetime | None
    status: RunStatus
    items_ok: int = Field(default=0, ge=0)
    items_quarantined: int = Field(default=0, ge=0)
    items_failed: int = Field(default=0, ge=0)
    notes: str | None = None
