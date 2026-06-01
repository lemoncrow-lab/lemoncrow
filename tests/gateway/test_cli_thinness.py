"""Regression guard for the final Phase 25 CLI thinning pass."""

from __future__ import annotations

from pathlib import Path

import click

from atelier.gateway.cli import cli, main

APP_PATH = Path(__file__).resolve().parents[2] / "src" / "atelier" / "gateway" / "cli" / "app.py"
APP_LOC_BUDGET = 350


def _non_comment_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.lstrip().startswith("#")]


def test_cli_app_stays_thin_and_behavior_free() -> None:
    """QBL-CLI-01: keep app.py below the final budget and free of process/db logic.

    Phase 25's M4 goal was to shrink app.py to roughly <500 LOC. The file now sits
    materially below that target, so this tighter <350 LOC guard leaves modest headroom
    for registration glue while still catching any business-logic regression quickly.
    """

    source = APP_PATH.read_text(encoding="utf-8")
    non_comment_source = "\n".join(_non_comment_lines(source))

    assert len(source.splitlines()) < APP_LOC_BUDGET
    assert "subprocess" not in non_comment_source
    assert "sqlite3" not in non_comment_source


def test_cli_public_import_surface_remains_stable() -> None:
    assert isinstance(cli, click.Group)
    assert callable(main)
