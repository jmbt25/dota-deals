"""Human-readable stdout report.

Format is intentionally plain text and column-aligned so a snapshot test
can pin the output byte-for-byte. The SPEC.md "always show reasoning"
product principle is enforced by structure: every score line is followed
by its four component values, never just the score.

A non-empty ``data_quality`` block is surfaced at the top so degraded runs
are visible at first glance.
"""

from __future__ import annotations

import sys
from io import StringIO
from pathlib import Path
from typing import TextIO

from dota_deals.models.domain import BuyScore, SignalName

_SIGNAL_ORDER: tuple[SignalName, ...] = (
    "price_zscore",
    "supply_velocity",
    "event_proximity",
    "comparables_delta",
)
_SIGNAL_DISPLAY: dict[SignalName, str] = {
    "price_zscore": "price",
    "supply_velocity": "supply",
    "event_proximity": "event",
    "comparables_delta": "peers",
}


def emit(
    scores: list[BuyScore],
    data_quality: dict[str, object],
    dest: Path | None = None,
) -> None:
    """Write a human-readable report for ``scores`` to ``dest``.

    ``dest=None`` writes to stdout. ``data_quality`` is the run-level
    block (ingest status, missing items, etc.); per-score data quality is
    rendered alongside each score row.

    :raises OSError: if ``dest`` is set and cannot be opened for writing.
    """
    buf = StringIO()
    _render(buf, scores, data_quality)
    rendered = buf.getvalue()

    if dest is None:
        out: TextIO = sys.stdout
        out.write(rendered)
        out.flush()
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered, encoding="utf-8")


def _render(out: TextIO, scores: list[BuyScore], data_quality: dict[str, object]) -> None:
    if not scores:
        out.write("dota-deals report\n")
        _write_data_quality(out, data_quality)
        out.write("no scores to report\n")
        return

    report_date = scores[0].computed_for.isoformat()
    out.write("dota-deals report\n")
    out.write(f"date: {report_date} UTC\n")
    _write_data_quality(out, data_quality)
    out.write(f"top {len(scores)} buy candidates:\n")
    out.write("\n")

    for idx, score in enumerate(scores, start=1):
        sign = "+" if score.score >= 0 else "-"
        out.write(f"{idx:>2}. score={sign}{abs(score.score):.3f} | item_id={score.item_id}\n")
        out.write(f"    reason: {score.explanation}\n")
        out.write(f"    components: {_format_components(score)}\n")
        per_score_dq = _format_per_score_dq(score)
        if per_score_dq:
            out.write(f"    data_quality: {per_score_dq}\n")
        out.write("\n")


def _write_data_quality(out: TextIO, data_quality: dict[str, object]) -> None:
    if not data_quality:
        out.write("data_quality: ok\n")
        return
    parts: list[str] = []
    for key in sorted(data_quality):
        parts.append(f"{key}={data_quality[key]!r}")
    out.write(f"data_quality: {', '.join(parts)}\n")


def _format_components(score: BuyScore) -> str:
    parts: list[str] = []
    for name in _SIGNAL_ORDER:
        val = score.components.get(name)
        label = _SIGNAL_DISPLAY[name]
        if val is None:
            parts.append(f"{label}=null")
        else:
            sign = "+" if val >= 0 else "-"
            parts.append(f"{label}={sign}{abs(val):.2f}")
    return " ".join(parts)


def _format_per_score_dq(score: BuyScore) -> str:
    """Render the per-score data_quality dict, or empty string when clean."""
    if not score.data_quality:
        return ""
    parts: list[str] = []
    for key in sorted(score.data_quality):
        parts.append(f"{key}={score.data_quality[key]!r}")
    return ", ".join(parts)
