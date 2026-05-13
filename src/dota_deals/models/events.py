"""Pydantic model for hand-curated Dota events.

Used by the ``event_proximity`` signal. The events table is small and curated
by a human; this model is the validation gate for both seed files and any
future editor.
"""

from __future__ import annotations

from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, model_validator

type EventKind = Literal[
    "ti",
    "treasure_release",
    "major_patch",
    "frostivus",
    "crownfall",
]
type EventConfidence = Literal["confirmed", "tentative"]


class EventRecord(BaseModel):
    """One row of the ``events`` table."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: int | None
    kind: EventKind
    name: str
    start_date: date
    end_date: date | None
    confidence: EventConfidence = "confirmed"
    notes: str | None = None

    @model_validator(mode="after")
    def _check_date_ordering(self) -> Self:
        """Ensure ``end_date`` is on or after ``start_date`` when present.

        :raises ValueError: if ``end_date < start_date``.
        """
        raise NotImplementedError
