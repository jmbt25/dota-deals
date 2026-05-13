"""Human-readable stdout report.

Renders the top-N buy candidates with component signal values and a one-line
explanation per pick. Writes to stdout (distinct from the structlog stderr
stream).
"""

from __future__ import annotations

from pathlib import Path

from dota_deals.models.domain import BuyScore


def emit(
    scores: list[BuyScore],
    data_quality: dict[str, object],
    dest: Path | None = None,
) -> None:
    """Write a human-readable report for ``scores`` to ``dest``.

    ``dest=None`` writes to stdout. A non-empty ``data_quality`` block is
    surfaced at the top of the report so degraded coverage is visible at a
    glance.

    :raises OSError: if ``dest`` is set and cannot be opened for writing.
    """
    raise NotImplementedError
