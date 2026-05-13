"""Signal-computation orchestrator.

For each active item, computes all four signals for the given UTC date and
persists the resulting rows to ``signals``. Idempotent at the row level via
the ``(item_id, computed_for, signal_name)`` primary key — re-running for
the same date is a no-op for already-written rows and back-fills any missing
ones.

Per-(item, signal) exceptions are caught at the boundary CLAUDE.md whitelists
for the signal computation loop; they emit a ``value=None`` row with the
exception type in metadata so the day's coverage is fully recorded.
:class:`StorageError` is NOT caught here — DB-level problems abort the run
per the architecture's error table.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime
from types import ModuleType

from structlog.stdlib import BoundLogger

from dota_deals.config import Settings
from dota_deals.logging import get_logger
from dota_deals.models.domain import Item, RunStatus, RunSummary, Signal, SignalName
from dota_deals.signals import (
    comparables,
    event_proximity,
    price_zscore,
    supply_velocity,
)
from dota_deals.storage.db import StorageError, bootstrap_schema, connect
from dota_deals.storage.repositories import (
    active_items,
    insert_run,
    insert_signal,
    update_run,
)

# A "signal computer" is a callable that returns a populated :class:`Signal`
# (with value or None) for a given (item_id, date). Insufficient-data cases
# are encoded in the returned Signal's metadata, not raised.
SignalComputeFn = Callable[[sqlite3.Connection, int, date], Signal]

# Module references rather than direct function references so attribute
# resolution happens at *call* time. Chosen for future signal
# instrumentation — timing per signal, error rates, fallback frequency, and
# null-cause distribution are all things we'll want to observe in
# production. Wrapping ``module.compute`` (replacing it with a decorated
# version that records metrics, then restoring it) only works when callers
# resolve the attribute at call time; caching the function reference at
# import would defeat that.
_SIGNAL_MODULES: tuple[tuple[SignalName, ModuleType], ...] = (
    ("price_zscore", price_zscore),
    ("supply_velocity", supply_velocity),
    ("event_proximity", event_proximity),
    ("comparables_delta", comparables),
)


def compute_signals_for(
    as_of: date,
    settings: Settings,
    *,
    run_id: str,
    parent_run_id: str | None = None,
    now: datetime | None = None,
) -> RunSummary:
    """Compute all four signals for every active item on ``as_of`` (UTC).

    :param as_of: UTC date the signals pertain to.
    :param settings: process settings (DB path).
    :param run_id: UUID4 identifying this signals run.
    :param parent_run_id: optional UUID4 grouping this run with sibling stage
        runs from the same CLI invocation.
    :param now: optional override for the run's wall-clock time.

    :raises StorageError: on any DB-level failure (the run is marked
        ``failed`` in ``runs`` before re-raise so observability stays clean).
    """
    started_at = now if now is not None else datetime.now(UTC)
    log = get_logger("dota_deals.signals.runner").bind(
        source="signals",
        run_id=run_id,
        as_of=as_of.isoformat(),
    )

    conn = connect(settings.db_path)
    try:
        bootstrap_schema(conn)
        insert_run(
            conn,
            RunSummary(
                run_id=run_id,
                parent_run_id=parent_run_id,
                kind="signals",
                started_at=started_at,
                finished_at=None,
                status="running",
                items_ok=0,
                items_quarantined=0,
                items_failed=0,
                notes=None,
            ),
        )

        items_ok = 0
        items_failed = 0

        try:
            for item in active_items(conn):
                item_log = log.bind(item_id=item.item_id)
                item_ok = _compute_all_for_item(conn, item, as_of, item_log)
                if item_ok:
                    items_ok += 1
                else:
                    items_failed += 1
        except StorageError as e:
            log.error("DB error aborting signals run", error=str(e))
            update_run(
                conn,
                run_id,
                status="failed",
                items_ok=items_ok,
                items_failed=items_failed,
                notes=f"{type(e).__name__}: {e}",
            )
            raise

        final_status: RunStatus = "success" if items_failed == 0 else "partial"
        finished_at = datetime.now(UTC)
        update_run(
            conn,
            run_id,
            status=final_status,
            items_ok=items_ok,
            items_failed=items_failed,
        )

        log.info(
            "signals run finished",
            status=final_status,
            items_ok=items_ok,
            items_failed=items_failed,
        )

        return RunSummary(
            run_id=run_id,
            parent_run_id=parent_run_id,
            kind="signals",
            started_at=started_at,
            finished_at=finished_at,
            status=final_status,
            items_ok=items_ok,
            items_quarantined=0,
            items_failed=items_failed,
            notes=None,
        )
    finally:
        conn.close()


def _compute_all_for_item(
    conn: sqlite3.Connection,
    item: Item,
    as_of: date,
    log: BoundLogger,
) -> bool:
    """Compute and persist all four signals for one item.

    Returns ``True`` if every signal persisted cleanly (a value=None signal
    counts as clean — we recorded the day for that item); ``False`` if at
    least one signal had to be replaced with a synthesized null row because
    its compute raised.

    Re-raises :class:`StorageError`; it's caught at the run level.
    """
    item_clean = True
    for signal_name, module in _SIGNAL_MODULES:
        sig_log = log.bind(signal_name=signal_name)
        try:
            signal = module.compute(conn, item.item_id, as_of)
        except StorageError:
            raise
        except Exception as e:  # documented signal-loop boundary (CLAUDE.md)
            sig_log.exception("signal compute raised; emitting null", error_type=type(e).__name__)
            signal = Signal(
                item_id=item.item_id,
                computed_for=as_of,
                signal_name=signal_name,
                value=None,
                metadata={
                    "reason": "computation_exception",
                    "error_type": type(e).__name__,
                },
            )
            item_clean = False
        insert_signal(conn, signal)
    return item_clean
