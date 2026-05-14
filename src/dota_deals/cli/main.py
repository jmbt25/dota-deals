"""Typer entry point.

Sub-commands:

* ``universe refresh`` — refresh the items universe via Steam search.
* ``items list-active`` — print active ``market_hash_name``s, one per line.
* ``ingest``           — fetch current prices/listings for the items in a file.
* ``signals compute``  — compute all four signals for every active item on a date.
* ``score``            — compose buy scores from signals for a date.
* ``report``           — render top-N report to stdout or JSON file.

The :data:`app` object is the Typer application; ``[project.scripts]`` in
``pyproject.toml`` wires the ``dota-deals`` console script to :func:`main`.

Phase 13 removed the ``publish`` and ``db pull`` / ``db push`` commands
along with their backing modules. Static-JSON publishing is now the
Worker API's job (``functions/api/*``) and the database lives in D1
rather than a SQLite file that needed R2 syncing.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import typer

from dota_deals.config import Settings, load_settings
from dota_deals.ingest.runner import run_ingestion
from dota_deals.ingest.universe import refresh_universe
from dota_deals.logging import configure_logging, get_logger
from dota_deals.models.domain import RunSummary
from dota_deals.notifier import json_file, stdout
from dota_deals.scoring.runner import compute_scores_for
from dota_deals.signals.runner import compute_signals_for
from dota_deals.storage.db import D1Connection, connect
from dota_deals.storage.repositories import (
    active_items,
    items_missing_observation_for_date,
    latest_ingest_run_for_date,
    latest_scores,
)

app = typer.Typer(
    name="dota-deals",
    help="Buy-signal analytics for the Steam Community Market (Dota 2).",
    no_args_is_help=True,
    add_completion=False,
)

universe_app = typer.Typer(
    name="universe",
    help="Manage the universe of tracked items.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(universe_app, name="universe")

signals_app = typer.Typer(
    name="signals",
    help="Compute and inspect signals.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(signals_app, name="signals")

items_app = typer.Typer(
    name="items",
    help="Inspect the items table.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(items_app, name="items")


def _read_items_file(path: Path) -> list[str]:
    """Read newline-separated market_hash_names. Strip blank lines and ``#`` comments."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise typer.BadParameter(f"cannot read items file {path}: {e}") from e
    items: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        items.append(stripped)
    if not items:
        raise typer.BadParameter(f"no items found in {path}")
    return items


@app.command()
def ingest(
    items_file: Annotated[
        Path,
        typer.Option(
            "--items",
            "-i",
            help="Path to newline-separated market_hash_name file.",
            exists=True,
            file_okay=True,
            dir_okay=False,
            readable=True,
        ),
    ],
) -> None:
    """Fetch current prices and listing counts for every item in --items."""
    settings = load_settings()
    parent_run_id = str(uuid.uuid4())
    configure_logging(run_id=parent_run_id, log_format=settings.log_format)
    log = get_logger("dota_deals.cli.ingest").bind(
        source="cli",
        parent_run_id=parent_run_id,
    )

    items = _read_items_file(items_file)
    log.info("starting ingest", item_count=len(items))

    run_id = str(uuid.uuid4())
    summary: RunSummary = asyncio.run(
        run_ingestion(
            items=items,
            settings=settings,
            run_id=run_id,
            parent_run_id=parent_run_id,
        )
    )

    log.info(
        "ingest complete",
        status=summary.status,
        items_ok=summary.items_ok,
        items_quarantined=summary.items_quarantined,
        items_failed=summary.items_failed,
    )

    if summary.status == "failed":
        raise typer.Exit(code=1)


@universe_app.command("refresh")
def universe_refresh() -> None:
    """Refresh the items table from Steam's market search.

    Discovers every arcana and immortal currently listed for Dota 2 and
    upserts each into ``items``. Previously deactivated items that reappear
    are reactivated.
    """
    settings = load_settings()
    parent_run_id = str(uuid.uuid4())
    configure_logging(run_id=parent_run_id, log_format=settings.log_format)
    log = get_logger("dota_deals.cli.universe").bind(
        source="cli",
        parent_run_id=parent_run_id,
    )

    run_id = str(uuid.uuid4())
    log.info("starting universe refresh")
    summary: RunSummary = asyncio.run(
        refresh_universe(
            settings=settings,
            run_id=run_id,
            parent_run_id=parent_run_id,
        )
    )

    log.info(
        "universe refresh complete",
        status=summary.status,
        items_ok=summary.items_ok,
        items_quarantined=summary.items_quarantined,
        items_failed=summary.items_failed,
    )

    if summary.status == "failed":
        raise typer.Exit(code=1)


@signals_app.command("compute")
def signals_compute(
    date_str: Annotated[
        str | None,
        typer.Option(
            "--date",
            "-d",
            help="UTC date YYYY-MM-DD; defaults to today UTC.",
        ),
    ] = None,
) -> None:
    """Compute all four signals for every active item on the given UTC date."""
    if date_str is None:
        as_of = datetime.now(UTC).date()
    else:
        try:
            as_of = date.fromisoformat(date_str)
        except ValueError as e:
            raise typer.BadParameter(f"invalid date {date_str!r}: {e}") from e

    settings = load_settings()
    parent_run_id = str(uuid.uuid4())
    configure_logging(run_id=parent_run_id, log_format=settings.log_format)
    log = get_logger("dota_deals.cli.signals").bind(
        source="cli",
        parent_run_id=parent_run_id,
    )

    run_id = str(uuid.uuid4())
    log.info("starting signals compute", as_of=as_of.isoformat())
    summary: RunSummary = asyncio.run(
        compute_signals_for(
            as_of=as_of,
            settings=settings,
            run_id=run_id,
            parent_run_id=parent_run_id,
        )
    )

    log.info(
        "signals compute complete",
        status=summary.status,
        items_ok=summary.items_ok,
        items_failed=summary.items_failed,
    )

    if summary.status == "failed":
        raise typer.Exit(code=1)


def _parse_date_or_today(raw: str | None) -> date:
    if raw is None:
        return datetime.now(UTC).date()
    try:
        return date.fromisoformat(raw)
    except ValueError as e:
        raise typer.BadParameter(f"invalid date {raw!r}: {e}") from e


async def _data_quality_block(conn: D1Connection, on: date) -> dict[str, object]:
    """Build the run-level data_quality block used by the notifier."""
    ingest = await latest_ingest_run_for_date(conn, on)
    missing = await items_missing_observation_for_date(conn, on)
    block: dict[str, object] = {}
    if ingest is None:
        block["ingest_status"] = "missing"
    else:
        block["ingest_run_id"] = ingest[0]
        block["ingest_status"] = ingest[1]
    block["missing_items"] = missing
    return block


@app.command("score")
def score_command(
    date_str: Annotated[
        str | None,
        typer.Option("--date", "-d", help="UTC date YYYY-MM-DD; defaults to today UTC."),
    ] = None,
) -> None:
    """Compose buy scores from signals for the given UTC date."""
    as_of = _parse_date_or_today(date_str)
    settings = load_settings()
    parent_run_id = str(uuid.uuid4())
    configure_logging(run_id=parent_run_id, log_format=settings.log_format)
    log = get_logger("dota_deals.cli.score").bind(source="cli", parent_run_id=parent_run_id)

    run_id = str(uuid.uuid4())
    log.info("starting scoring", as_of=as_of.isoformat())
    summary: RunSummary = asyncio.run(
        compute_scores_for(
            as_of=as_of, settings=settings, run_id=run_id, parent_run_id=parent_run_id
        )
    )
    log.info(
        "scoring complete",
        status=summary.status,
        items_ok=summary.items_ok,
        items_failed=summary.items_failed,
    )
    if summary.status == "failed":
        raise typer.Exit(code=1)


@app.command("report")
def report_command(
    date_str: Annotated[
        str | None,
        typer.Option("--date", "-d", help="UTC date YYYY-MM-DD; defaults to today UTC."),
    ] = None,
    top: Annotated[
        int, typer.Option("--top", "-n", help="Number of top candidates to report.")
    ] = 20,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            "-o",
            help="Write JSON to this path. If omitted, the report goes to stdout.",
        ),
    ] = None,
) -> None:
    """Render the top-N buy candidates for ``--date`` to stdout or JSON file."""
    if top < 0:
        raise typer.BadParameter(f"--top must be >= 0, got {top}")
    as_of = _parse_date_or_today(date_str)
    settings = load_settings()
    parent_run_id = str(uuid.uuid4())
    configure_logging(run_id=parent_run_id, log_format=settings.log_format)
    log = get_logger("dota_deals.cli.report").bind(source="cli", parent_run_id=parent_run_id)

    score_count = asyncio.run(_report_async(settings, as_of, top, out))
    log.info(
        "report emitted",
        as_of=as_of.isoformat(),
        score_count=score_count,
        out=str(out) if out else "stdout",
    )


async def _report_async(settings: Settings, as_of: date, top: int, out: Path | None) -> int:
    """Async body of ``dota-deals report``.

    Returns the number of scores emitted so the sync entry can log it
    without re-opening the connection.
    """
    async with connect(settings) as conn:
        scores = await latest_scores(conn, as_of, top)
        data_quality = await _data_quality_block(conn, as_of)

    if out is None:
        stdout.emit(scores, data_quality, dest=None)
    else:
        json_file.emit(scores, data_quality, dest=out)
    return len(scores)


@items_app.command("list-active")
def items_list_active() -> None:
    """Print every active item's ``market_hash_name``, one per line.

    Designed to be piped into ``dota-deals ingest --items <file>`` so the
    scheduled workflow can derive the ingest list from the universe stage's
    output without committing a static ``items.txt``. Output has no header
    and no trailing whitespace; sorted by ``item_id`` for stable diffs.
    """
    settings = load_settings()
    asyncio.run(_items_list_active_async(settings))


async def _items_list_active_async(settings: Settings) -> None:
    """Async body of ``dota-deals items list-active``.

    Separated so tests can invoke the helper directly (typer's CliRunner
    can't host a nested ``asyncio.run`` inside pytest-asyncio's loop).
    """
    async with connect(settings) as conn:
        for item in await active_items(conn):
            typer.echo(item.market_hash)


def main() -> None:
    """Console-script entry point.

    Wraps Typer with a top-level handler so we exit cleanly on Ctrl-C
    (architecture says SIGINT marks the run failed and exits 130).
    """
    try:
        app()
    except KeyboardInterrupt:
        # Reach this only if asyncio.run was interrupted before run_ingestion
        # could update its own run row; that's acceptable — the row was
        # inserted with status='running' and will be visible as such.
        sys.exit(130)


# Type-check helper so this module can be imported without side effects in
# tests that only assert on the Typer app's existence.
__all__ = ["Settings", "app", "main"]
