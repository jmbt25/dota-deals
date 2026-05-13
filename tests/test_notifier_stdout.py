"""Tests for :mod:`dota_deals.notifier.stdout`.

The report is the product surface. We pin the format byte-for-byte via a
golden expected-string check so any unintentional formatting drift is
visible immediately.
"""

from __future__ import annotations

import io
import sys
from datetime import date
from pathlib import Path

from dota_deals.models.domain import BuyScore, SignalName
from dota_deals.notifier import stdout

AS_OF = date(2026, 5, 12)


def _make_score(
    item_id: int,
    score_value: float,
    *,
    components: dict[SignalName, float | None] | None = None,
    explanation: str = "Priced below recent baseline",
    data_quality: dict[str, object] | None = None,
) -> BuyScore:
    if components is None:
        components = {
            "price_zscore": 0.5,
            "supply_velocity": 0.4,
            "event_proximity": None,
            "comparables_delta": 0.2,
        }
    return BuyScore(
        item_id=item_id,
        computed_for=AS_OF,
        score=score_value,
        components=components,
        explanation=explanation,
        data_quality=data_quality or {},
    )


def test_golden_stdout_two_items(capsys: object) -> None:
    """Pin the output format exactly. If this test fails, look at the diff —
    it's almost always intentional formatting drift somebody forgot to
    update."""
    scores = [
        _make_score(
            42,
            0.62,
            components={
                "price_zscore": 0.85,
                "supply_velocity": 0.50,
                "event_proximity": None,
                "comparables_delta": 0.40,
            },
            explanation="Priced below recent baseline",
            data_quality={
                "null_signals": ["event_proximity"],
                "ingest_status": "success",
                "item_missing_from_ingest": False,
            },
        ),
        _make_score(
            17,
            0.45,
            components={
                "price_zscore": 0.30,
                "supply_velocity": 0.50,
                "event_proximity": 0.20,
                "comparables_delta": None,
            },
            explanation="Listings contracting",
            data_quality={
                "null_signals": ["comparables_delta"],
                "ingest_status": "success",
                "item_missing_from_ingest": False,
            },
        ),
    ]

    expected = (
        "dota-deals report\n"
        "date: 2026-05-12 UTC\n"
        "data_quality: ok\n"
        "top 2 buy candidates:\n"
        "\n"
        " 1. score=+0.620 | item_id=42\n"
        "    reason: Priced below recent baseline\n"
        "    components: price=+0.85 supply=+0.50 event=null peers=+0.40\n"
        "    data_quality: ingest_status='success', "
        "item_missing_from_ingest=False, null_signals=['event_proximity']\n"
        "\n"
        " 2. score=+0.450 | item_id=17\n"
        "    reason: Listings contracting\n"
        "    components: price=+0.30 supply=+0.50 event=+0.20 peers=null\n"
        "    data_quality: ingest_status='success', "
        "item_missing_from_ingest=False, null_signals=['comparables_delta']\n"
        "\n"
    )

    buf = io.StringIO()
    _redirect_stdout(buf, lambda: stdout.emit(scores, data_quality={}, dest=None))
    assert buf.getvalue() == expected


def test_run_level_data_quality_block_rendered(capsys: object) -> None:
    """Non-empty run-level data_quality is surfaced at the top of the report."""
    scores = [_make_score(1, 0.5)]
    data_quality: dict[str, object] = {
        "ingest_status": "partial",
        "missing_items": ["A", "B"],
    }
    buf = io.StringIO()
    _redirect_stdout(buf, lambda: stdout.emit(scores, data_quality, dest=None))
    output = buf.getvalue()
    assert "data_quality: ingest_status='partial'" in output
    assert "missing_items=['A', 'B']" in output


def test_empty_scores_list_renders_no_candidates_message(
    capsys: object,
) -> None:
    buf = io.StringIO()
    _redirect_stdout(buf, lambda: stdout.emit([], data_quality={}, dest=None))
    output = buf.getvalue()
    assert "no scores to report" in output


def test_emit_to_file_writes_same_content(tmp_path: Path) -> None:
    """Passing dest= writes the same rendered string to a file."""
    scores = [_make_score(1, 0.5)]
    out_path = tmp_path / "report.txt"
    stdout.emit(scores, data_quality={}, dest=out_path)
    assert out_path.read_text(encoding="utf-8").startswith("dota-deals report\n")


def _redirect_stdout(buf: io.StringIO, fn: object) -> None:
    """Run ``fn()`` with sys.stdout replaced by ``buf``."""
    orig = sys.stdout
    sys.stdout = buf
    try:
        fn()  # type: ignore[operator]
    finally:
        sys.stdout = orig
