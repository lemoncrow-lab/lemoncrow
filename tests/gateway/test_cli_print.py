from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.commands import run as run_cmd


def test_top_level_print_runs_prompt_only(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    def fake_run_print_session(task: str, *, root: Path) -> str:
        seen["task"] = task
        seen["root"] = root
        return "short answer"

    monkeypatch.setattr(run_cmd, "_run_print_session", fake_run_print_session)

    result = CliRunner().invoke(cli, ["--root", str(tmp_path), "-p", "explain this"])

    assert result.exit_code == 0, result.output
    assert result.stdout == "short answer\n"
    assert seen == {"task": "explain this", "root": tmp_path}


def test_top_level_print_includes_piped_stdin(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def fake_run_print_session(task: str, *, root: Path) -> str:
        seen["task"] = task
        return "debugged"

    monkeypatch.setattr(run_cmd, "_run_print_session", fake_run_print_session)

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "--print", "debug this"],
        input="line 1\nline 2\n",
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "debugged\n"
    assert seen["task"] == "debug this\n\n<stdin>\nline 1\nline 2\n</stdin>"


def test_top_level_print_batches_multiple_prompts(monkeypatch, tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def fake_run_print_session(task: str, *, root: Path) -> str:
        seen["task"] = task
        return "first answer\n---\nsecond answer"

    monkeypatch.setattr(run_cmd, "_run_print_session", fake_run_print_session)

    result = CliRunner().invoke(
        cli,
        ["--root", str(tmp_path), "-p", "first", "-p", "second"],
    )

    assert result.exit_code == 0, result.output
    assert result.stdout == "first answer\n---\nsecond answer\n"
    assert '<prompt index="1">\nfirst\n</prompt>' in seen["task"]
    assert '<prompt index="2">\nsecond\n</prompt>' in seen["task"]
