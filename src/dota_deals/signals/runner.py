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

Phase 9c-ii: rewritten to async. The HTTP round-trip count per run is now
``O(pages_of_each_bulk_fetch)`` rather than ``O(items * signals)`` —
typically 6-8 fetches total + one batched signal insert, regardless of
universe size. See :mod:`dota_deals.signals.dataset` for the
fetch-once / dispatch-many shape.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime

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
from dota_deals.signals.dataset import DataLookup, build_data_lookup
from dota_deals.storage.db import StorageError
from dota_deals.storage.db_async import D1Backend, connect
from dota_deals.storage.repositories_async import (
    insert_run,
    insert_signals,
    update_run,
)

# A "signal computer" is a pure callable that returns a populated
# :class:`Signal` (with value or None) for a given (item, date, data).
# Insufficient-data cases are encoded in the returned Signal's metadata,
# not raised.
SignalComputeFn = Callable[[int, date, DataLookup], Signal]


# Attribute resolution at call time (via module reference) rather than
# direct function references so a future instrumentation layer can wrap
# ``module.compute`` once and have every dispatch pick up the wrapper.
# Caching ``module.compute`` at import would defeat that.
_SIGNAL_DISPATCH: tuple[tuple[SignalName, SignalComputeFn], ...] = (
    ("price_zscore", lambda i, d, dl: price_zscore.compute(i, d, dl)),
    ("supply_velocity", lambda i, d, dl: supply_velocity.compute(i, d, dl)),
    ("event_proximity", lambda i, d, dl: event_proximity.compute(i, d, dl)),
    ("comparables_delta", lambda i, d, dl: comparables.compute(i, d, dl)),
)


async def compute_signals_for(
    as_of: date,
    settings: Settings,
    *,
    run_id: str,
    parent_run_id: str | None = None,
    now: datetime | None = None,
    backend: D1Backend | None = None,
) -> RunSummary:
    """Compute all four signals for every active item on ``as_of`` (UTC).

    :param as_of: UTC date the signals pertain to.
    :param settings: process settings (D1 credentials, budget warn).
    :param run_id: UUID4 identifying this signals run.
    :param parent_run_id: optional UUID4 grouping this run with sibling stage
        runs from the same CLI invocation.
    :param now: optional override for the run's wall-clock time.
    :param backend: test seam. When ``None`` (CLI path), the runner opens
        a real :class:`D1Client` from ``settings``. Tests pass a
        :class:`D1FakeClient` instance to keep storage in-memory.

    :raises StorageError: on any DB-level failure (the run is marked
        ``failed`` in ``runs`` before re-raise so observability stays clean).
    """
    started_at = now if now is not None else datetime.now(UTC)
    log = get_logger("dota_deals.signals.runner").bind(
        source="signals",
        run_id=run_id,
        as_of=as_of.isoformat(),
    )

    async with connect(settings, backend=backend) as conn:
        await insert_run(
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
            data = await build_data_lookup(conn, as_of)
            log.info(
                "data lookup built",
                items=len(data.items_by_id),
                next_event_id=data.next_event.event_id if data.next_event else None,
            )

            # Collect all signals across all items, then batch-write once.
            # Per-item bookkeeping (items_ok / items_failed) tracks whether
            # any of the item's four signals had to be replaced by a
            # synthesized null row from the exception boundary.
            all_signals: list[Signal] = []
            for item in sorted(data.items_by_id.values(), key=lambda i: i.item_id):
                item_log = log.bind(item_id=item.item_id)
                item_signals, item_clean = _compute_all_for_item(item, as_of, data, item_log)
                all_signals.extend(item_signals)
                if item_clean:
                    items_ok += 1
                else:
                    items_failed += 1

            await insert_signals(conn, all_signals)
        except StorageError as e:
            log.error("DB error aborting signals run", error=str(e))
            await update_run(
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
        await update_run(
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


def _compute_all_for_item(
    item: Item,
    as_of: date,
    data: DataLookup,
    log: BoundLogger,
) -> tuple[list[Signal], bool]:
    """Compute all four signals for one item.

    Returns ``(signals, item_clean)`` where ``signals`` is the list of four
    Signal rows to persist and ``item_clean`` is ``True`` iff none of them
    had to be replaced with a synthesized null row because its compute raised.
    A null Signal from data insufficiency counts as clean — we recorded the
    day for that item.
    """
    signals: list[Signal] = []
    item_clean = True
    for signal_name, compute_fn in _SIGNAL_DISPATCH:
        sig_log = log.bind(signal_name=signal_name)
        try:
            signal = compute_fn(item.item_id, as_of, data)
        except Exception as e:  # documented signal-loop boundary (CLAUDE.md)
            sig_log.exception(
                "signal compute raised; emitting null",
                error_type=type(e).__name__,
            )
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
        signals.append(signal)
    return signals, item_clean
