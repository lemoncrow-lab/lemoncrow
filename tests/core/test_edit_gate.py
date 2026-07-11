"""Tests for the WS1 edit-loop correctness gate (parse gate + verifier driver)."""

from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.verification.edit_gate import (
    run_edit_gate,
    treesitter_parse_errors,
)


def test_parse_gate_flags_broken_typescript(tmp_path: Path) -> None:
    bad = tmp_path / "broken.ts"
    bad.write_text("export const y: number = ;;;{\n", encoding="utf-8")
    ces = treesitter_parse_errors([bad])
    assert ces, "expected a parse counterexample for broken TS"
    assert all(c.check == "parse" and c.severity == "error" for c in ces)
    assert ces[0].file_path == str(bad)


def test_parse_gate_passes_clean_typescript(tmp_path: Path) -> None:
    good = tmp_path / "ok.ts"
    good.write_text("export const x: number = 1;\n", encoding="utf-8")
    assert treesitter_parse_errors([good]) == []


def test_parse_gate_flags_broken_python(tmp_path: Path) -> None:
    bad = tmp_path / "broken.py"
    bad.write_text("def f(:\n    return\n", encoding="utf-8")
    ces = treesitter_parse_errors([bad])
    assert len(ces) == 1
    assert ces[0].check == "parse"
    assert "syntax error" in ces[0].diagnostic


def test_parse_gate_passes_clean_python(tmp_path: Path) -> None:
    good = tmp_path / "ok.py"
    good.write_text("def f() -> int:\n    return 1\n", encoding="utf-8")
    assert treesitter_parse_errors([good]) == []


def test_parse_gate_ignores_missing_and_unknown(tmp_path: Path) -> None:
    missing = tmp_path / "nope.ts"
    unknown = tmp_path / "data.txt"
    unknown.write_text("not code at all := ;;;\n", encoding="utf-8")
    assert treesitter_parse_errors([missing, unknown]) == []


def test_run_edit_gate_short_circuits_on_parse_failure(tmp_path: Path) -> None:
    # A broken non-Python file must be reported without invoking mypy/pytest.
    bad = tmp_path / "broken.ts"
    bad.write_text("function (\n", encoding="utf-8")
    ces = run_edit_gate([bad], repo_root=tmp_path)
    assert ces and ces[0].check == "parse"


def test_run_edit_gate_empty_when_no_python_or_failures(tmp_path: Path) -> None:
    good = tmp_path / "ok.ts"
    good.write_text("export const x = 1;\n", encoding="utf-8")
    # No .py files -> no mypy/pytest scope -> empty (clean) result.
    assert run_edit_gate([good], repo_root=tmp_path, run_parse_gate=True) == []
