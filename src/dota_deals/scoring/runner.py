"""Scoring orchestrator.

For each active item, reads the four signal rows written by
``signals.runner`` and composes them into a :class:`BuyScore` via
:func:`compute_buy_score`. Idempotent at the row level on
``(item_id, computed_for)`` via ``INSERT OR IGNORE``.

Data-quality propagation
------------------------
Per the architecture's "honesty in failure" criterion, every score row
carries a ``data_quality_json`` payload that surfaces:

* ``null_signals`` — which of the four signals were null for this item
  (already populated by :func:`compute_buy_score`).
* ``ingest_status`` — the status of the most recent ingest run whose
  ``started_at`` falls on the score's UTC date. ``"missing"`` if no
  ingest ran that day.
* ``item_missing_from_ingest`` — true if this item has no ``price_history``
  row on the score's date (regardless of why).

Run-level data quality (e.g. the universe of items with no observations)
is the notifier's responsibility, not the scorer's.

Phase 9c-iii: storage moves to async D1. Signals for all active items
on the date are bulk-read in a single call via
:func:`signals_for_items_on_date`, then composed and batch-written via
:func:`insert_scores`. HTTP round-trips per run drop from O(items) to
~5 (active_items, latest_ingest_run_for_date,
items_missing_observation_for_date, signals_for_items_on_date,
insert_scores).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from structlog.stdlib import BoundLogger

from dota_deals.config import Settings
from dota_deals.logging import get_logger
from dota_deals.models.domain import BuyScore, Item, RunStatus, RunSummary, Signal
from dota_deals.scoring.buy_score import compute_buy_score
from dota_deals.storage.db import StorageError
from dota_deals.storage.db_async import D1Backend, connect
from dota_deals.storage.repositories_async import (
    active_items,
    insert_run,
    insert_scores,
    items_missing_observation_for_date,
    latest_ingest_run_for_date,
    signals_for_items_on_date,
    update_run,
)


async def compute_scores_for(
    as_of: date,
    settings: Settings,
    *,
    run_id: str,
    parent_run_id: str | None = None,
    now: datetime | None = None,
    backend: D1Backend | None = None,
) -> RunSummary:
    """Compose buy scores for every active item with ≥ 2 signal values on ``as_of``.

    Items where :func:`compute_buy_score` returns ``None`` (3+ null signals)
    are counted in ``items_failed`` and no row is written. The runs row is
    tagged ``kind='scoring'`` and linked to ``parent_run_id``.

    :param backend: test seam. ``None`` (CLI path) opens a real D1Client;
        tests pass a D1FakeClient.

    :raises StorageError: on any DB-level failure (the run row is marked
        ``failed`` before re-raise).
    """
    started_at = now if now is not None else datetime.now(UTC)
    log = get_logger("dota_deals.scoring.runner").bind(
        source="scoring",
        run_id=run_id,
        as_of=as_of.isoformat(),
    )

    async with connect(settings, backend=backend) as conn:
        await insert_run(
            conn,
            RunSummary(
                run_id=run_id,
                parent_run_id=parent_run_id,
                kind="scoring",
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
            ingest_info = await latest_ingest_run_for_date(conn, as_of)
            ingest_status: str = ingest_info[1] if ingest_info is not None else "missing"
            missing_set = set(await items_missing_observation_for_date(conn, as_of))

            items = await active_items(conn)
            signals_by_item = await signals_for_items_on_date(
                conn, [i.item_id for i in items], as_of
            )

            # Compose all scores in-process, then batch-write once.
            enriched: list[BuyScore] = []
            for item in items:
                item_log = log.bind(item_id=item.item_id)
                score = _score_one(
                    item,
                    as_of,
                    signals_by_item.get(item.item_id, []),
                    ingest_status,
                    missing_set,
                    item_log,
                )
                if score is None:
                    items_failed += 1
                    continue
                enriched.append(score)
                items_ok += 1

            await insert_scores(conn, enriched)
        except StorageError as e:
            log.error("DB error aborting scoring run", error=str(e))
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
            "scoring run finished",
            status=final_status,
            items_ok=items_ok,
            items_failed=items_failed,
        )

        return RunSummary(
            run_id=run_id,
            parent_run_id=parent_run_id,
            kind="scoring",
            started_at=started_at,
            finished_at=finished_at,
            status=final_status,
            items_ok=items_ok,
            items_quarantined=0,
            items_failed=items_failed,
            notes=None,
        )


def _score_one(
    item: Item,
    as_of: date,
    signals: list[Signal],
    ingest_status: str,
    missing_set: set[str],
    log: BoundLogger,
) -> BuyScore | None:
    """Compose one item's score; ``None`` means "no row should be written".

    Pure function over the pre-fetched signals list. ``None`` is returned
    when either the item has no signals at all on this date, the signals
    list violates :func:`compute_buy_score`'s preconditions, or 3+ of the
    four signals are null.
    """
    if not signals:
        log.info("no signals for item on this date; skipping")
        return None

    try:
        score = compute_buy_score(signals)
    except ValueError as e:
        # compute_buy_score raises ValueError for contract violations
        # (empty list, mixed item_ids, unknown signal name). The runner
        # treats this as a per-item failure rather than aborting.
        log.warning("compute_buy_score rejected input", error=str(e))
        return None

    if score is None:
        log.info("3+ null signals; no score for this item")
        return None

    return score.model_copy(
        update={
            "data_quality": {
                **score.data_quality,
                "ingest_status": ingest_status,
                "item_missing_from_ingest": item.market_hash in missing_set,
            }
        }
    )
