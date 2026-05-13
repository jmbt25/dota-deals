"""Typer entry point.

Sub-commands:

* ``universe refresh`` — refresh the items universe via Steam search.
* ``ingest``           — fetch current prices/listings for the items in a file.
* ``signals compute``  — compute all four signals for every active item on a date.
* ``score``            — compose buy scores from signals for a date.
* ``report``           — render top-N report to stdout or JSON file.
* ``publish``          — build JSON payloads for the static frontend.
* ``db pull`` / ``db push`` — sync the SQLite DB to/from Cloudflare R2.

The :data:`app` object is the Typer application; ``[project.scripts]`` in
``pyproject.toml`` wires the ``dota-deals`` console script to :func:`main`.
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
from dota_deals.publish.builder import (
    build_health,
    build_historical_report,
    build_item_detail,
    build_latest_report,
)
from dota_deals.publish.r2 import R2Client, R2Error
from dota_deals.publish.writer import write_atomic
from dota_deals.scoring.runner import compute_scores_for
from dota_deals.signals.runner import compute_signals_for
from dota_deals.storage.db import connect
from dota_deals.storage.repositories import (
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

db_app = typer.Typer(
    name="db",
    help="Database sync (Cloudflare R2).",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(db_app, name="db")


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
    summary: RunSummary = compute_signals_for(
        as_of=as_of,
        settings=settings,
        run_id=run_id,
        parent_run_id=parent_run_id,
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


def _data_quality_block(settings: Settings, on: date) -> dict[str, object]:
    """Build the run-level data_quality block used by the notifier."""
    conn = connect(settings.db_path)
    try:
        ingest = latest_ingest_run_for_date(conn, on)
        missing = items_missing_observation_for_date(conn, on)
    finally:
        conn.close()
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
    summary: RunSummary = compute_scores_for(
        as_of=as_of, settings=settings, run_id=run_id, parent_run_id=parent_run_id
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

    conn = connect(settings.db_path)
    try:
        scores = latest_scores(conn, as_of, top)
    finally:
        conn.close()

    data_quality = _data_quality_block(settings, as_of)

    if out is None:
        stdout.emit(scores, data_quality, dest=None)
    else:
        json_file.emit(scores, data_quality, dest=out)
    log.info(
        "report emitted",
        as_of=as_of.isoformat(),
        score_count=len(scores),
        out=str(out) if out else "stdout",
    )


@app.command("publish")
def publish_command(
    top: Annotated[int, typer.Option("--top", "-n", help="Items per top-N report.")] = 20,
    out_dir: Annotated[
        Path,
        typer.Option(
            "--out-dir",
            "-o",
            help="Directory to write JSON files into. Created if missing.",
        ),
    ] = Path("public/data"),
    include_items: Annotated[
        bool,
        typer.Option(
            "--include-items",
            help="Also write per-item detail JSON for items in the top-N.",
        ),
    ] = False,
) -> None:
    """Build the JSON payloads for the static frontend.

    Always writes ``latest.json`` and ``health.json``. Writes
    ``history/<date>.json`` for today's UTC date if scores exist for it
    (skipped silently otherwise — today is the most common reason). With
    ``--include-items``, also writes ``items/<item_id>.json`` for every
    item in the top-N.
    """
    if top < 0:
        raise typer.BadParameter(f"--top must be >= 0, got {top}")
    settings = load_settings()
    parent_run_id = str(uuid.uuid4())
    configure_logging(run_id=parent_run_id, log_format=settings.log_format)
    log = get_logger("dota_deals.cli.publish").bind(
        source="cli", parent_run_id=parent_run_id, out_dir=str(out_dir)
    )

    conn = connect(settings.db_path)
    try:
        latest = build_latest_report(conn, top_n=top)
        write_atomic(latest, out_dir / "latest.json")
        log.info("published latest.json", status=latest.status, score_count=len(latest.scores))

        health = build_health(conn)
        write_atomic(health, out_dir / "health.json")
        log.info("published health.json", status=health.status)

        today = datetime.now(UTC).date()
        historical = build_historical_report(conn, today, top_n=top)
        if historical is not None:
            write_atomic(historical, out_dir / "history" / f"{today.isoformat()}.json")
            log.info("published history file", date=today.isoformat())
        else:
            log.info("no scores for today; history file skipped", date=today.isoformat())

        if include_items:
            written = 0
            for score in latest.scores:
                detail = build_item_detail(conn, score.item_id)
                if detail is None:
                    continue
                write_atomic(detail, out_dir / "items" / f"{score.item_id}.json")
                written += 1
            log.info("published item detail files", count=written)
    finally:
        conn.close()


@db_app.command("pull")
def db_pull() -> None:
    """Download the SQLite DB from Cloudflare R2 to ``settings.db_path``."""
    settings = load_settings()
    parent_run_id = str(uuid.uuid4())
    configure_logging(run_id=parent_run_id, log_format=settings.log_format)
    log = get_logger("dota_deals.cli.db_pull").bind(source="cli")
    try:
        client = R2Client(settings)
        client.download_db(settings.db_path)
    except R2Error as e:
        log.error("R2 pull failed", error_type=type(e).__name__, error=str(e))
        raise typer.Exit(code=1) from e


@db_app.command("push")
def db_push() -> None:
    """Upload the SQLite DB at ``settings.db_path`` to Cloudflare R2."""
    settings = load_settings()
    parent_run_id = str(uuid.uuid4())
    configure_logging(run_id=parent_run_id, log_format=settings.log_format)
    log = get_logger("dota_deals.cli.db_push").bind(source="cli")
    try:
        client = R2Client(settings)
        client.upload_db(settings.db_path)
    except R2Error as e:
        log.error("R2 push failed", error_type=type(e).__name__, error=str(e))
        raise typer.Exit(code=1) from e
    except FileNotFoundError as e:
        log.error("local DB not found", error=str(e), path=str(settings.db_path))
        raise typer.Exit(code=1) from e


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
