"""Tests for :mod:`dota_deals.notifier.json_file`."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from dota_deals.models.domain import BuyScore, SignalName
from dota_deals.notifier import json_file

AS_OF = date(2026, 5, 12)


def _score(item_id: int, score_value: float) -> BuyScore:
    components: dict[SignalName, float | None] = {
        "price_zscore": 0.5,
        "supply_velocity": 0.4,
        "event_proximity": None,
        "comparables_delta": 0.2,
    }
    data_quality: dict[str, object] = {
        "null_signals": ["event_proximity"],
        "ingest_status": "success",
        "item_missing_from_ingest": False,
    }
    return BuyScore(
        item_id=item_id,
        computed_for=AS_OF,
        score=score_value,
        components=components,
        explanation="Priced below recent baseline",
        data_quality=data_quality,
    )


def test_writes_parseable_json_to_dest(tmp_path: Path) -> None:
    scores = [_score(42, 0.62), _score(17, 0.45)]
    data_quality: dict[str, object] = {"ingest_status": "success", "missing_items": []}
    out = tmp_path / "reports" / "2026-05-12.json"

    json_file.emit(scores, data_quality, dest=out)

    assert out.exists()
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["report_date"] == "2026-05-12"
    assert payload["data_quality"] == {"ingest_status": "success", "missing_items": []}
    assert len(payload["scores"]) == 2


def test_score_dict_structure(tmp_path: Path) -> None:
    scores = [_score(42, 0.62)]
    out = tmp_path / "r.json"
    json_file.emit(scores, data_quality={}, dest=out)

    payload = json.loads(out.read_text(encoding="utf-8"))
    score = payload["scores"][0]
    assert score["item_id"] == 42
    assert score["computed_for"] == "2026-05-12"
    assert score["buy_score"] == 0.62
    assert score["components"] == {
        "price_zscore": 0.5,
        "supply_velocity": 0.4,
        "event_proximity": None,
        "comparables_delta": 0.2,
    }
    assert score["explanation"] == "Priced below recent baseline"
    assert score["data_quality"]["null_signals"] == ["event_proximity"]


def test_data_quality_block_present_at_top_level(tmp_path: Path) -> None:
    """data_quality is a top-level key so consumers can see degraded state
    without parsing scores. SPEC's "honesty in failure" criterion."""
    scores = [_score(1, 0.5)]
    data_quality_partial: dict[str, object] = {
        "ingest_status": "partial",
        "ingest_run_id": "abc-123",
        "missing_items": ["DelistedItem"],
    }
    out = tmp_path / "r.json"
    json_file.emit(scores, data_quality_partial, dest=out)

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["data_quality"]["ingest_status"] == "partial"
    assert payload["data_quality"]["ingest_run_id"] == "abc-123"
    assert payload["data_quality"]["missing_items"] == ["DelistedItem"]


def test_empty_scores_list_writes_null_report_date(tmp_path: Path) -> None:
    out = tmp_path / "empty.json"
    json_file.emit([], data_quality={"ingest_status": "missing"}, dest=out)

    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["scores"] == []
    assert payload["report_date"] is None
    assert payload["data_quality"] == {"ingest_status": "missing"}


def test_write_is_atomic_no_partial_file_on_directory_creation(tmp_path: Path) -> None:
    """Parent directory is created on the fly; no temp leftover after success."""
    scores = [_score(1, 0.5)]
    nested = tmp_path / "a" / "b" / "c" / "report.json"
    json_file.emit(scores, data_quality={}, dest=nested)

    assert nested.exists()
    # Verify there's no leftover temp file in the directory.
    siblings = [p.name for p in nested.parent.iterdir()]
    assert siblings == ["report.json"]
