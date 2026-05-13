"""Machine-readable JSON output for downstream consumers.

The emitted JSON is the contract for the eventual frontend: a list of scored
items with their component signals plus a top-level ``data_quality`` block.
"""

from __future__ import annotations

from pathlib import Path

from dota_deals.models.domain import BuyScore


def emit(
    scores: list[BuyScore],
    data_quality: dict[str, object],
    dest: Path,
) -> None:
    """Write ``scores`` + ``data_quality`` as JSON to ``dest``.

    The file is written atomically: data is staged into a sibling temp file
    and renamed into place on success.

    :raises OSError: if ``dest`` cannot be opened for writing.
    """
    raise NotImplementedError
