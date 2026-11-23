"""scripts/mirror.py publishes one commit per release; a per-commit replay must be asked for."""

from __future__ import annotations

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_MIRROR = Path(__file__).resolve().parents[1] / "scripts" / "mirror.py"
_spec = importlib.util.spec_from_file_location("_mirror_publication_under_test", _MIRROR)
assert _spec and _spec.loader
mirror = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mirror)


def _args(**overrides: object) -> argparse.Namespace:
    values: dict[str, object] = {"export_dir": None, "resync": False, "per_commit": False, "dry_run": False}
    values.update(overrides)
    return argparse.Namespace(**values)


def _run(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(_MIRROR), *argv], capture_output=True, text=True, timeout=60)


def test_a_replay_is_refused_unless_per_commit_is_asked_for() -> None:
    assert mirror.replay_refusal(_args()) is not None
    assert mirror.replay_refusal(_args(resync=True)) is None
    assert mirror.replay_refusal(_args(per_commit=True)) is None
    assert mirror.replay_refusal(_args(export_dir=Path("out"))) is None
    assert mirror.replay_refusal(_args(dry_run=True)) is None
    # Refused before any git command, push or watermark read: a plain run and a forced full replay alike.
    for argv in ((), ("--force",), ("--since", "a:b")):
        result = _run(*argv)
        assert result.returncode == 2, (argv, result.stderr)
        assert "refused: a per-commit replay publishes the message of every private commit" in result.stderr


def test_message_file_goes_with_resync_only_and_resync_excludes_per_commit(tmp_path: Path) -> None:
    message = tmp_path / "message"
    message.write_text("Release v1.2.3\n", encoding="utf-8")
    assert _run("--message-file", str(message)).returncode == 2
    assert "--message-file goes with --resync" in _run("--message-file", str(message)).stderr
    assert _run("--resync", "--per-commit").returncode == 2


def test_resync_message_is_the_file_text_or_the_generic_message(tmp_path: Path) -> None:
    assert mirror.resync_message(None) == mirror.DEFAULT_RESYNC_MESSAGE
    message = tmp_path / "message"
    message.write_text("\nRelease v1.2.3\n\n### Added\n- A thing.\n\n", encoding="utf-8")
    assert mirror.resync_message(message) == "Release v1.2.3\n\n### Added\n- A thing.\n"
    message.write_text(" \n", encoding="utf-8")
    with pytest.raises(SystemExit, match="--message-file is empty"):
        mirror.resync_message(message)
