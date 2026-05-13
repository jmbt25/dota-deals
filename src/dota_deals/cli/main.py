"""Typer entry point.

Sub-commands:

* ``ingest``    — fetch current prices/listings for the items in a file.
* (Phase 4+) ``universe``, ``signals``, ``score``, ``report``.

The :data:`app` object is the Typer application; ``[project.scripts]`` in
``pyproject.toml`` wires the ``dota-deals`` console script to :func:`main`.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from typing import Annotated

import typer

from dota_deals.config import Settings, load_settings
from dota_deals.ingest.runner import run_ingestion
from dota_deals.logging import configure_logging, get_logger
from dota_deals.models.domain import RunSummary

app = typer.Typer(
    name="dota-deals",
    help="Buy-signal analytics for the Steam Community Market (Dota 2).",
    no_args_is_help=True,
    add_completion=False,
)


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
