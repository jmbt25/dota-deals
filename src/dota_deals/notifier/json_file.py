"""Machine-readable JSON output for downstream consumers.

The emitted JSON is the contract for the eventual frontend: a list of scored
items with their component signals plus a top-level ``data_quality`` block.
Written atomically via a sibling temp file + rename so partial writes never
leak out.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from dota_deals.models.domain import BuyScore


def emit(
    scores: list[BuyScore],
    data_quality: dict[str, object],
    dest: Path,
) -> None:
    """Write ``scores`` + ``data_quality`` as JSON to ``dest``.

    The file is written atomically: data is staged into a sibling temp file
    and renamed into place on success. The destination's parent directory
    is created if missing.

    :raises OSError: if ``dest`` cannot be opened or replaced.
    """
    payload = _build_payload(scores, data_quality)
    serialized = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".tmp", dir=dest.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(serialized)
        os.replace(tmp_path, dest)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise


def _build_payload(scores: list[BuyScore], data_quality: dict[str, object]) -> dict[str, object]:
    """Build the dict that gets serialized to JSON. Pure function — used by
    tests to assert structure without touching the filesystem."""
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "report_date": scores[0].computed_for.isoformat() if scores else None,
        "data_quality": dict(data_quality),
        "scores": [_score_to_dict(s) for s in scores],
    }


def _score_to_dict(score: BuyScore) -> dict[str, object]:
    return {
        "item_id": score.item_id,
        "computed_for": score.computed_for.isoformat(),
        "buy_score": score.score,
        "components": dict(score.components),
        "explanation": score.explanation,
        "data_quality": dict(score.data_quality),
    }
