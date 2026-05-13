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
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime

from structlog.stdlib import BoundLogger

from dota_deals.config import Settings
from dota_deals.logging import get_logger
from dota_deals.models.domain import BuyScore, Item, RunStatus, RunSummary
from dota_deals.scoring.buy_score import compute_buy_score
from dota_deals.storage.db import StorageError, bootstrap_schema, connect
from dota_deals.storage.repositories import (
    active_items,
    insert_run,
    insert_score,
    items_missing_observation_for_date,
    latest_ingest_run_for_date,
    signals_for,
    update_run,
)


def compute_scores_for(
    as_of: date,
    settings: Settings,
    *,
    run_id: str,
    parent_run_id: str | None = None,
    now: datetime | None = None,
) -> RunSummary:
    """Compose buy scores for every active item with ≥ 2 signal values on ``as_of``.

    Items where :func:`compute_buy_score` returns ``None`` (3+ null signals)
    are counted in ``items_failed`` and no row is written. The runs row is
    tagged ``kind='scoring'`` and linked to ``parent_run_id``.

    :raises StorageError: on any DB-level failure (the run row is marked
        ``failed`` before re-raise).
    """
    started_at = now if now is not None else datetime.now(UTC)
    log = get_logger("dota_deals.scoring.runner").bind(
        source="scoring",
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

        # Read ingest health for the date once; reuse for every score.
        ingest_info = latest_ingest_run_for_date(conn, as_of)
        ingest_status: str = ingest_info[1] if ingest_info is not None else "missing"
        missing_set = set(items_missing_observation_for_date(conn, as_of))

        items_ok = 0
        items_failed = 0

        try:
            for item in active_items(conn):
                if _score_one(conn, item, as_of, ingest_status, missing_set, log):
                    items_ok += 1
                else:
                    items_failed += 1
        except StorageError as e:
            log.error("DB error aborting scoring run", error=str(e))
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
    finally:
        conn.close()


def _score_one(
    conn: sqlite3.Connection,
    item: Item,
    as_of: date,
    ingest_status: str,
    missing_set: set[str],
    log: BoundLogger,
) -> bool:
    """Compute and persist one item's score. Returns ``True`` on success."""
    item_log = log.bind(item_id=item.item_id)
    signals = signals_for(conn, item.item_id, as_of)
    if not signals:
        item_log.info("no signals for item on this date; skipping")
        return False

    score: BuyScore | None
    try:
        score = compute_buy_score(signals)
    except ValueError as e:
        # compute_buy_score raises ValueError for contract violations
        # (empty list, mixed item_ids, unknown signal name). The runner
        # treats this as a per-item failure rather than aborting.
        item_log.warning("compute_buy_score rejected input", error=str(e))
        return False

    if score is None:
        item_log.info("3+ null signals; no score for this item")
        return False

    enriched = score.model_copy(
        update={
            "data_quality": {
                **score.data_quality,
                "ingest_status": ingest_status,
                "item_missing_from_ingest": item.market_hash in missing_set,
            }
        }
    )

    insert_score(conn, enriched)
    return True
