"""Tests for :mod:`dota_deals.publish.writer`."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from dota_deals.publish.models import (
    HistoricalReport,
    LatestReport,
    WireDataQuality,
)
from dota_deals.publish.writer import write_atomic


def _empty_latest() -> LatestReport:
    return LatestReport(
        schema_version=1,
        generated_at=datetime(2026, 5, 13, 20, 0, tzinfo=UTC),
        report_date=None,
        status="warmup",
        data_quality=WireDataQuality(ingest_status="missing"),
        scores=[],
    )


def test_writes_parseable_json(tmp_path: Path) -> None:
    out = tmp_path / "latest.json"
    write_atomic(_empty_latest(), out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["status"] == "warmup"
    assert payload["report_date"] is None
    assert payload["scores"] == []


def test_datetime_serialized_with_z_suffix(tmp_path: Path) -> None:
    """The wire convention is Z, not +00:00. Pydantic's default mode='json'
    emits the offset form; the writer's custom encoder normalizes it.
    """
    out = tmp_path / "latest.json"
    write_atomic(_empty_latest(), out)
    text = out.read_text(encoding="utf-8")
    assert "2026-05-13T20:00:00Z" in text
    assert "+00:00" not in text


def test_creates_nested_directories(tmp_path: Path) -> None:
    out = tmp_path / "a" / "b" / "history" / "2026-05-13.json"
    write_atomic(
        HistoricalReport(
            schema_version=1,
            generated_at=datetime(2026, 5, 13, 20, 0, tzinfo=UTC),
            report_date=date(2026, 5, 13),
            data_quality=WireDataQuality(ingest_status="success"),
            scores=[],
        ),
        out,
    )
    assert out.exists()


def test_atomic_no_leftover_temp_on_success(tmp_path: Path) -> None:
    out = tmp_path / "latest.json"
    write_atomic(_empty_latest(), out)
    siblings = [p.name for p in out.parent.iterdir()]
    assert siblings == ["latest.json"]


def test_overwrites_existing_file(tmp_path: Path) -> None:
    out = tmp_path / "latest.json"
    out.write_text("stale", encoding="utf-8")
    write_atomic(_empty_latest(), out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1


def test_trailing_newline_present(tmp_path: Path) -> None:
    out = tmp_path / "latest.json"
    write_atomic(_empty_latest(), out)
    text = out.read_text(encoding="utf-8")
    assert text.endswith("\n")
