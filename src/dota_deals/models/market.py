"""Wire-format models for Steam Market responses.

These types validate Steam's responses strictly, then expose the data in
canonical form (prices as ``int`` cents, USD). Internal code never sees raw
Steam payloads — they pass through these models first.

Pricing string parsing is locked to USD because every request the
:mod:`dota_deals.ingest.steam` client makes pins ``currency=1``. Non-USD
strings will fail validation and route to quarantine, which is the right
behavior — we'd rather surface the misconfiguration than silently mis-store
amounts in the wrong currency.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_USD_PATTERN = re.compile(r"^\s*\$([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(?:\.([0-9]{2}))?\s*$")
_INT_WITH_COMMAS = re.compile(r"^\s*([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)\s*$")


def _parse_usd_cents(raw: str) -> int:
    """Convert a USD currency string (e.g. ``"$3.45"``, ``"$1,234.56"``) to int cents.

    :raises ValueError: if ``raw`` does not match the expected USD format. The
        steam client wraps this as :class:`IngestValidationError` and routes
        the response to the quarantine table.
    """
    match = _USD_PATTERN.match(raw)
    if match is None:
        raise ValueError(f"unparseable USD price: {raw!r}")
    dollars_part = match.group(1).replace(",", "")
    cents_part = match.group(2) or "00"
    try:
        amount = Decimal(f"{dollars_part}.{cents_part}")
    except InvalidOperation as e:
        raise ValueError(f"invalid USD decimal: {raw!r}") from e
    if amount <= 0:
        raise ValueError(f"price must be positive: {raw!r}")
    cents = (amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def _parse_int_with_commas(raw: str) -> int:
    """Parse Steam's volume string (e.g. ``"12"``, ``"1,234"``) to int.

    :raises ValueError: if ``raw`` is not a comma-grouped integer.
    """
    match = _INT_WITH_COMMAS.match(raw)
    if match is None:
        raise ValueError(f"unparseable integer: {raw!r}")
    return int(match.group(1).replace(",", ""))


def _maybe_str(value: object) -> str | None:
    """Return ``value`` if it's a non-empty string, else ``None``."""
    if isinstance(value, str) and value.strip():
        return value
    return None


class SteamPriceOverview(BaseModel):
    """Normalized ``/market/priceoverview`` response.

    The raw Steam response carries localized currency strings (e.g.
    ``"$3.45"``); the ``mode="before"`` model validator converts those to
    ``int`` cents. Construction with keyword arguments (already-normalized
    shape) is also supported, which is what tests use.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    success: bool
    lowest_cents: int | None = Field(default=None, gt=0)
    median_cents: int | None = Field(default=None, gt=0)
    volume_24h: int | None = Field(default=None, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _normalize_steam_shape(cls, data: object) -> object:
        if not isinstance(data, Mapping):
            return data
        # Detect raw Steam shape (presence of any string-typed price field).
        if any(k in data for k in ("lowest_price", "median_price", "volume")):
            lowest = _maybe_str(data.get("lowest_price"))
            median = _maybe_str(data.get("median_price"))
            volume = _maybe_str(data.get("volume"))
            return {
                "success": bool(data.get("success", False)),
                "lowest_cents": _parse_usd_cents(lowest) if lowest else None,
                "median_cents": _parse_usd_cents(median) if median else None,
                "volume_24h": _parse_int_with_commas(volume) if volume else None,
            }
        return data

    @classmethod
    def from_raw(cls, payload: Mapping[str, object]) -> Self:
        """Parse a raw Steam JSON payload into a validated instance.

        Thin alias for :meth:`pydantic.BaseModel.model_validate`; preserves the
        public surface promised in ``docs/ARCHITECTURE.md``.

        :raises pydantic.ValidationError: if the payload cannot be normalized
            or fails field validation.
        """
        return cls.model_validate(payload)


class SteamListingsResponse(BaseModel):
    """Normalized listings render response.

    Steam's ``/market/listings/<appid>/<name>/render`` endpoint returns a
    JSON body containing ``total_count`` (and a lot of HTML the client doesn't
    care about); we keep only the count.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    success: bool
    listings_count: int = Field(ge=0)

    @model_validator(mode="before")
    @classmethod
    def _normalize_steam_shape(cls, data: object) -> object:
        if not isinstance(data, Mapping):
            return data
        if "total_count" in data:
            total = data["total_count"]
            if isinstance(total, str):
                listings_count = _parse_int_with_commas(total)
            elif isinstance(total, int):
                listings_count = total
            else:
                raise ValueError(f"unparseable total_count: {total!r}")
            return {
                "success": bool(data.get("success", False)),
                "listings_count": listings_count,
            }
        return data

    @classmethod
    def from_raw(cls, payload: Mapping[str, object]) -> Self:
        """Parse the raw render payload into total listings count.

        :raises pydantic.ValidationError: if ``total_count`` is missing or
            unparseable.
        """
        return cls.model_validate(payload)


class SteamSearchResult(BaseModel):
    """One row from a ``/market/search/render?norender=1`` response.

    The raw Steam shape carries each item's metadata under
    ``asset_description``; we flatten that to the three fields we actually
    need for universe discovery.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    market_hash_name: str = Field(min_length=1)
    name: str = Field(min_length=1)
    type: str  # display string, e.g. "Arcana" or "Immortal Item"


class SteamSearchPage(BaseModel):
    """One page of a market search response.

    Steam's ``/market/search/render?norender=1`` endpoint returns a list of
    results with ``total_count`` so callers can paginate by incrementing
    ``start`` until ``start >= total_count``.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    success: bool
    start: int = Field(ge=0)
    pagesize: int = Field(ge=0)
    total_count: int = Field(ge=0)
    results: list[SteamSearchResult]

    @model_validator(mode="before")
    @classmethod
    def _normalize_steam_shape(cls, data: object) -> object:
        if not isinstance(data, Mapping):
            return data
        # Already-normalized shape (test construction) passes through.
        if "results" not in data:
            return data
        raw_results = data.get("results")
        if not isinstance(raw_results, list):
            raise ValueError(f"expected `results` to be a list, got {type(raw_results).__name__}")
        normalized: list[Mapping[str, object]] = []
        for raw_entry in raw_results:
            if not isinstance(raw_entry, Mapping):
                raise ValueError(f"each result must be a mapping, got {type(raw_entry).__name__}")
            desc = raw_entry.get("asset_description")
            desc_map: Mapping[str, object] = desc if isinstance(desc, Mapping) else {}
            market_hash_name = (
                desc_map.get("market_hash_name")
                or raw_entry.get("hash_name")
                or raw_entry.get("name")
            )
            display_name = desc_map.get("name") or raw_entry.get("name")
            type_str = desc_map.get("type") or ""
            normalized.append(
                {
                    "market_hash_name": market_hash_name,
                    "name": display_name,
                    "type": type_str,
                }
            )
        return {
            "success": bool(data.get("success", False)),
            "start": _coerce_int(data.get("start", 0)),
            "pagesize": _coerce_int(data.get("pagesize", 0)),
            "total_count": _coerce_int(data.get("total_count", 0)),
            "results": normalized,
        }

    @classmethod
    def from_raw(cls, payload: Mapping[str, object]) -> Self:
        """Parse a raw Steam search page response.

        :raises pydantic.ValidationError: if pagination fields are missing or
            any result is malformed.
        """
        return cls.model_validate(payload)


def _coerce_int(value: object) -> int:
    """Convert int-like inputs (Steam mixes ``int`` and string) to ``int``."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _parse_int_with_commas(value)
    raise ValueError(f"cannot coerce to int: {value!r}")
