"""CLI plumbing tests for `lemon optimize compress-context` (LLM monkeypatched)."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner, Result

import lemoncrow.infra.internal_llm as internal_llm
from lemoncrow.gateway.cli import cli
from lemoncrow.infra.internal_llm import InternalLLMError

ORIGINAL = "# Rules\n\n" + ("Please always make sure that you run the full test suite before you commit.\n" * 20)
COMPRESSED = "# Rules\n\n- Run the full test suite before committing."


def _invoke(tmp_path: Path, target: Path, *extra: str) -> Result:
    root = tmp_path / ".lemoncrow"
    return CliRunner().invoke(cli, ["--root", str(root), "optimize", "compress-context", str(target), *extra])


@pytest.fixture()
def context_file(tmp_path: Path) -> Path:
    target = tmp_path / "CLAUDE.md"
    target.write_text(ORIGINAL, encoding="utf-8")
    return target


def test_dry_run_prints_compression_and_leaves_file_untouched(
    tmp_path: Path, context_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(internal_llm, "summarize", lambda text, **kw: COMPRESSED)
    result = _invoke(tmp_path, context_file)
    assert result.exit_code == 0, result.output
    assert "Run the full test suite before committing." in result.output
    assert "tokens:" in result.output
    assert "dry-run" in result.output
    assert context_file.read_text(encoding="utf-8") == ORIGINAL
    assert not context_file.with_name("CLAUDE.md.bak").exists()


def test_write_creates_backup_and_rewrites_file(
    tmp_path: Path, context_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(internal_llm, "summarize", lambda text, **kw: COMPRESSED)
    result = _invoke(tmp_path, context_file, "--write")
    assert result.exit_code == 0, result.output
    backup = context_file.with_name("CLAUDE.md.bak")
    assert backup.read_text(encoding="utf-8") == ORIGINAL
    assert context_file.read_text(encoding="utf-8") == COMPRESSED + "\n"


def test_not_smaller_result_is_refused(tmp_path: Path, context_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(internal_llm, "summarize", lambda text, **kw: ORIGINAL)
    result = _invoke(tmp_path, context_file, "--write")
    assert result.exit_code != 0
    assert "not smaller" in result.output
    assert context_file.read_text(encoding="utf-8") == ORIGINAL
    assert not context_file.with_name("CLAUDE.md.bak").exists()


def test_internal_llm_unavailable_fails_without_truncation(
    tmp_path: Path, context_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _raise(text: str, **kw: object) -> str:
        raise InternalLLMError("backend disabled")

    monkeypatch.setattr(internal_llm, "summarize", _raise)
    result = _invoke(tmp_path, context_file, "--write")
    assert result.exit_code != 0
    assert "internal LLM unavailable" in result.output
    assert context_file.read_text(encoding="utf-8") == ORIGINAL
