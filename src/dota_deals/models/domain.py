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
    # Strikes accumulate when ingest gets a 4xx for the item. 3 consecutive →
    # deactivation. Reset on any successful ingest or on the next universe
    # refresh sighting.
    consecutive_ingest_4xx: int = Field(default=0, ge=0)


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
    """A composite buy score with its component signals exposed.

    ``components`` carries every signal value (including nulls) so the
    display layer can show "we didn't have this one" alongside the active
    contributors. ``data_quality`` records why those nulls happened plus
    any run-level context worth surfacing in the per-item view (e.g. the
    ingest stage for this date was ``partial``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: int
    computed_for: date
    score: float = Field(ge=-1.0, le=1.0)
    components: dict[SignalName, float | None]
    explanation: str
    data_quality: dict[str, object] = Field(default_factory=dict)


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
