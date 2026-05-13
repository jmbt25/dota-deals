"""Signal-computation orchestrator.

For each active item, computes all four signals for the given date and
persists the resulting rows to ``signals``. Per-item failures are caught,
logged, and recorded as ``value=None``; they never abort the run.
"""

from __future__ import annotations

from datetime import date

from dota_deals.config import Settings
from dota_deals.models.domain import RunSummary


def compute_signals_for(
    as_of: date,
    settings: Settings,
    *,
    run_id: str,
    parent_run_id: str | None = None,
) -> RunSummary:
    """Compute all four signals for every active item on ``as_of``.

    Writes one row per (item, signal) into the ``signals`` table and one row
    into ``runs``. Items whose signals all return ``None`` are still recorded
    so that downstream scoring can report ``data_quality`` accurately.

    :param as_of: the date the signals pertain to (UTC).
    :param settings: process settings.
    :param run_id: UUID4 identifying this signals run.
    :param parent_run_id: optional UUID4 grouping this run with sibling
        stage runs from the same CLI invocation.
    """
    raise NotImplementedError
