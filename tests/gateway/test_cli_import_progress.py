"""Import-progress logging routes to stderr, never stdout (QBL-LOG-02/03).

Phase 24 converted the session-parser import-progress ``print()`` calls to
module loggers. The CLI import path attaches an idempotent INFO-level stderr
``StreamHandler`` so users still see progress — on stderr, with stdout reserved
for user result lines (``imported N ... sessions``).
"""

from __future__ import annotations

import logging
from pathlib import Path

from click.testing import CliRunner, Result

from atelier.gateway.cli import cli
from atelier.gateway.cli.app import (
    _IMPORT_PROGRESS_LOGGER,
    _ensure_import_progress_logging,
)


def _make_claude_fixture(tmp_path: Path) -> Path:
    """Create a minimal ~/.claude/projects-style dir with one session file."""
    projects = tmp_path / "projects"
    project_dir = projects / "-home-user-Projects-demo"
    project_dir.mkdir(parents=True)
    # A single (intentionally trivial) jsonl session — enough to trigger the
    # "discovering sessions (found N)" progress line on import_all.
    (project_dir / "00000000-0000-0000-0000-000000000001.jsonl").write_text(
        '{"type": "user", "message": {"role": "user", "content": "hi"}}\n',
        encoding="utf-8",
    )
    return projects


def _invoke(root: Path, *args: str) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), *args])


def test_import_progress_lands_on_stderr_not_stdout(tmp_path: Path) -> None:
    root = tmp_path / "store"
    _invoke(root, "init", "--no-index")
    sessions = _make_claude_fixture(tmp_path)

    result = _invoke(root, "claude", "import", "--path", str(sessions))

    assert result.exit_code == 0, result.output

    # Progress diagnostic appears on stderr.
    assert "[atelier] claude: discovering sessions" in result.stderr

    # Import progress text must NOT leak onto stdout.
    assert "[atelier]" not in result.stdout
    assert "discovering sessions" not in result.stdout

    # User-facing result line stays on stdout.
    assert "imported" in result.stdout


def test_import_progress_handler_is_idempotent() -> None:
    progress_logger = logging.getLogger(_IMPORT_PROGRESS_LOGGER)

    def _flagged_handler_count() -> int:
        return sum(1 for h in progress_logger.handlers if getattr(h, "_atelier_import_progress_handler", False))

    _ensure_import_progress_logging()
    after_first = _flagged_handler_count()
    assert after_first == 1

    # Repeat invocations must not accumulate duplicate handlers.
    _ensure_import_progress_logging()
    _ensure_import_progress_logging()
    assert _flagged_handler_count() == 1
