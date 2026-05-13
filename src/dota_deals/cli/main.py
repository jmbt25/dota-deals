"""Typer entry point.

Sub-commands (wired up in Phase 3):

* ``universe``  — refresh the items universe.
* ``ingest``    — fetch current prices/listings for the universe.
* ``signals``   — compute signals for a given date.
* ``score``     — compose buy scores from the latest signals.
* ``report``    — emit stdout + JSON output.

The :data:`app` object is the Typer application; ``[project.scripts]`` in
``pyproject.toml`` will reference it once Phase 3 lands.
"""

from __future__ import annotations

import typer

app = typer.Typer(
    name="dota-deals",
    help="Buy-signal analytics for the Steam Community Market (Dota 2).",
    no_args_is_help=True,
    add_completion=False,
)


def main() -> None:
    """Synchronous CLI entry point. Delegates to the Typer app.

    Phase-2 scaffold: this is importable but commands are not yet registered.
    """
    raise NotImplementedError
