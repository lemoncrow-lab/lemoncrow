"""Tests for the additive ast-grep rule-mode (G12) `scan` path.

These exercise the real ast-grep binary (skipped if unavailable) to prove the
rule engine evaluates relational matchers (`inside`) and that the legacy
`--pattern` search path is unaffected by the addition.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier.infra.code_intel.astgrep.adapter import AstGrepAdapter
from atelier.infra.code_intel.astgrep.binaries import discover_astgrep_binary

_RESOLUTION = discover_astgrep_binary(Path.cwd(), allow_bootstrap=True)
_HAS_ASTGREP = _RESOLUTION.available
_SKIP_REASON = "ast-grep binary unavailable in this environment"


def _write(tmp_path: Path, name: str, body: str) -> None:
    target = tmp_path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


@pytest.mark.skipif(not _HAS_ASTGREP, reason=_SKIP_REASON)
def test_scan_rule_mode_finds_relational_inside_match(tmp_path: Path) -> None:
    # `eval` called *inside* a function body, plus a bare `eval` at module
    # scope that the relational matcher must NOT flag.
    _write(
        tmp_path,
        "app.py",
        "def outer():\n    eval(user_input)\n\n\neval(top_level)\n",
    )
    adapter = AstGrepAdapter(tmp_path)
    rules = [
        {
            "id": "eval-inside-func",
            "language": "python",
            "severity": "error",
            "message": "eval inside a function",
            "rule": {
                "pattern": "eval($X)",
                "inside": {"kind": "function_definition", "stopBy": "end"},
            },
        }
    ]

    result = adapter.scan(rules=rules)

    assert len(result.matches) == 1, result.matches
    match = result.matches[0]
    assert match.rule_id == "eval-inside-func"
    assert match.severity == "error"
    assert match.message == "eval inside a function"
    assert match.snippet == "eval(user_input)"
    assert match.captures == {"X": "user_input"}
    # The module-scope `eval(top_level)` is outside any function -> not matched.
    assert Path(match.file_path).name == "app.py"


@pytest.mark.skipif(not _HAS_ASTGREP, reason=_SKIP_REASON)
def test_scan_accepts_prerendered_yaml_string(tmp_path: Path) -> None:
    _write(tmp_path, "m.py", "def f():\n    exec(blob)\n")
    adapter = AstGrepAdapter(tmp_path)
    yaml_rule = (
        "id: exec-call\n"
        "language: python\n"
        "severity: warning\n"
        "message: exec call\n"
        "rule:\n"
        "  pattern: exec($X)\n"
    )

    result = adapter.scan(rules=yaml_rule)

    assert [m.rule_id for m in result.matches] == ["exec-call"]
    assert result.matches[0].severity == "warning"


@pytest.mark.skipif(not _HAS_ASTGREP, reason=_SKIP_REASON)
def test_legacy_pattern_search_still_works(tmp_path: Path) -> None:
    # The additive scan() path must not disturb the existing --pattern search.
    _write(tmp_path, "req.py", "import requests\n\nrequests.get(url)\n")
    adapter = AstGrepAdapter(tmp_path)

    result = adapter.search(pattern="requests.get($URL)", language="python")

    assert len(result.matches) == 1
    assert result.matches[0].captures == {"URL": "url"}
    assert result.matches[0].snippet == "requests.get(url)"
