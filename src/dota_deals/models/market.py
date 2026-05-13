"""Wire-format models for Steam Market responses.

These types validate Steam's responses strictly, then expose the data in
canonical form (prices as ``int`` cents, USD). Internal code never sees raw
Steam payloads — they pass through these models first.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Self

from pydantic import BaseModel, ConfigDict, Field


class SteamPriceOverview(BaseModel):
    """Normalized ``/market/priceoverview`` response.

    The raw Steam response carries localized currency strings (e.g.
    ``"$3.45"``); :meth:`from_raw` converts those to ``int`` cents.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    lowest_cents: int | None = Field(default=None, gt=0)
    median_cents: int | None = Field(default=None, gt=0)
    volume_24h: int | None = Field(default=None, ge=0)

    @classmethod
    def from_raw(cls, payload: Mapping[str, object]) -> Self:
        """Parse a raw Steam JSON payload into a validated instance.

        Handles the localized currency string format and the absence of fields
        when ``success`` is false.

        :raises pydantic.ValidationError: if required fields are missing or
            unparseable.
        """
        raise NotImplementedError


class SteamListingsResponse(BaseModel):
    """Normalized listings histogram response.

    Used to count items currently for sale (Signal 2 input).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    listings_count: int = Field(ge=0)

    @classmethod
    def from_raw(cls, payload: Mapping[str, object]) -> Self:
        """Parse the raw histogram payload into total listings count.

        :raises pydantic.ValidationError: if required fields are missing or
            unparseable.
        """
        raise NotImplementedError
