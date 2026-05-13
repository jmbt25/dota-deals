"""Smoke test: verify :mod:`dota_deals.cli.main` imports."""

from __future__ import annotations

import typer

from dota_deals.cli import main as cli_main


def test_module_imports() -> None:
    assert isinstance(cli_main.app, typer.Typer)
    assert callable(cli_main.main)
