"""The kit's ast-grep adapter: results in path order, rules without YAML, no download."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import pytest
from lemoncrow_client.kit.astgrep import (
    AstGrepAdapter,
    AstGrepToolUnavailable,
    bound_rewrite_diff,
    discover_astgrep,
    one_based,
    render_rules,
)

_HAS_ASTGREP = discover_astgrep(Path.cwd()).available


def _match(file: str, line: int, column: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "file": file,
        "text": f"hit {file}:{line}",
        "range": {"start": {"line": line, "column": column}, "end": {"line": line, "column": column + 3}},
        **extra,
    }


def _printing(adapter: AstGrepAdapter, monkeypatch: pytest.MonkeyPatch, matches: list[dict[str, Any]]) -> None:
    """ast-grep printing ``matches`` in exactly this order, as its parallel walk may."""
    monkeypatch.setattr(
        adapter,
        "_run",
        lambda args: CompletedProcess(args=args, returncode=0, stdout=json.dumps(matches), stderr=""),
    )


def test_a_limit_keeps_the_first_matches_in_path_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = AstGrepAdapter(tmp_path, binary_path=tmp_path / "ast-grep")
    _printing(adapter, monkeypatch, [_match("z.py", 1), _match("a.py", 9), _match("a.py", 2, 4), _match("a.py", 2, 1)])
    result = adapter.search(pattern="f($X)", limit=3)
    assert [(m.file_path, m.line, m.column) for m in result.matches] == [("a.py", 2, 1), ("a.py", 2, 4), ("a.py", 9, 0)]
    assert result.truncated is True


def test_scan_findings_come_back_in_path_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = AstGrepAdapter(tmp_path, binary_path=tmp_path / "ast-grep")
    _printing(
        adapter,
        monkeypatch,
        [_match("b.py", 3, ruleId="r2"), _match("a.py", 3, ruleId="r2"), _match("a.py", 3, ruleId="r1")],
    )
    result = adapter.scan(rules=[{"id": "r1"}], limit=200)
    assert [(m.file_path, m.rule_id) for m in result.matches] == [("a.py", "r1"), ("a.py", "r2"), ("b.py", "r2")]


def test_a_rewrite_lists_files_in_path_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("b.py", "a.py"):
        (tmp_path / name).write_text("x == None\n", encoding="utf-8")
    adapter = AstGrepAdapter(tmp_path, binary_path=tmp_path / "ast-grep")
    edit = {"replacement": "x is None", "replacementOffsets": {"start": 0, "end": 9}}
    _printing(adapter, monkeypatch, [{"file": "b.py", **edit}, {"file": "a.py", **edit}])
    result = adapter.rewrite(pattern="$X == None", rewrite="$X is None", dry_run=True)
    assert result.files_changed == ["a.py", "b.py"]
    assert result.diff.index("--- a/a.py") < result.diff.index("--- a/b.py")
    assert (tmp_path / "a.py").read_text(encoding="utf-8") == "x == None\n"


def test_matches_read_one_based_like_an_editor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = AstGrepAdapter(tmp_path, binary_path=tmp_path / "ast-grep")
    _printing(adapter, monkeypatch, [_match("a.py", 0, 4)])
    [match] = one_based(adapter.search(pattern="f($X)")).matches
    assert (match.line, match.column, match.end_line, match.end_column) == (1, 5, 1, 8)


def test_rules_render_as_json_documents() -> None:
    rules = [{"id": "a", "language": "python", "rule": {"pattern": "eval($X)"}}, {"id": "b"}]
    rendered = render_rules(rules)
    assert [json.loads(document) for document in rendered.split("\n---\n")] == rules


def test_discovery_honours_the_override_and_refuses_util_linux_sg(tmp_path: Path) -> None:
    fake = tmp_path / "ast-grep"
    fake.write_text("#!/bin/sh\n", encoding="utf-8")
    fake.chmod(0o755)
    found = discover_astgrep(tmp_path, env={"LEMONCROW_AST_GREP_BIN": str(fake)})
    assert (found.available, found.path, found.source) == (True, fake.resolve(), "env")
    refused = discover_astgrep(tmp_path, env={"LEMONCROW_AST_GREP_BIN": "/usr/bin/sg"})
    assert refused.available is False
    assert "group-switch" in str(refused.reason)


def test_without_a_binary_the_adapter_refuses_and_downloads_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.delenv("LEMONCROW_AST_GREP_BIN", raising=False)
    with pytest.raises(AstGrepToolUnavailable) as refused:
        AstGrepAdapter(tmp_path).search(pattern="f($X)")
    assert refused.value.payload["error"] == "tool_unavailable"
    assert list(tmp_path.iterdir()) == []


def test_a_long_diff_keeps_its_head_and_tail() -> None:
    diff = "".join(f"+line {index}\n" for index in range(500))
    bounded = bound_rewrite_diff(diff)
    assert bounded.startswith("+line 0\n") and bounded.endswith("+line 499\n")
    assert "(300 more diff lines elided; see files_changed)" in bounded
    assert bound_rewrite_diff("+short\n") == "+short\n"


@pytest.mark.skipif(not _HAS_ASTGREP, reason="ast-grep is not installed")
def test_json_rules_and_an_applied_rewrite_on_the_real_binary(tmp_path: Path) -> None:
    script = tmp_path / "run.py"
    script.write_text("if x == None:\n    eval(x)\n", encoding="utf-8")
    script.chmod(0o755)
    adapter = AstGrepAdapter(tmp_path)
    rule = {"id": "no-eval", "language": "python", "severity": "error", "message": "m", "rule": {"pattern": "eval($X)"}}
    found = adapter.scan(rules=[rule])
    assert [(m.rule_id, m.line) for m in found.matches] == [("no-eval", 1)]
    applied = adapter.rewrite(pattern="$X == None", rewrite="$X is None", language="python", dry_run=False)
    assert applied.files_changed == ["run.py"]
    assert script.read_text(encoding="utf-8") == "if x is None:\n    eval(x)\n"
    assert script.stat().st_mode & 0o777 == 0o755
    assert shutil.which("ast-grep") is not None


def test_a_truncated_search_reports_how_many_matched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = AstGrepAdapter(tmp_path, binary_path=tmp_path / "ast-grep")
    _printing(adapter, monkeypatch, [_match("a.py", line) for line in range(5)])
    result = adapter.search(pattern="f($X)", limit=2)
    assert (len(result.matches), result.truncated, result.total_matches) == (2, True, 5)


def test_a_scan_without_ast_grep_says_the_rule_pack_was_skipped(tmp_path: Path) -> None:
    from lemoncrow_client.kit.security_scan import run_scan_tool

    (tmp_path / "view.py").write_text(
        "import os\nfrom flask import request\n\n\ndef v():\n    os.system(request.args.get('q'))\n",
        encoding="utf-8",
    )

    def missing(_root: Path) -> Path:
        raise AstGrepToolUnavailable({"message": "ast-grep could not be resolved"})

    payload = run_scan_tool(tmp_path, adapter_factory=lambda root: AstGrepAdapter(root, resolve_binary=missing))
    assert payload["summary"]["rules_skipped"] == "ast-grep unavailable: ast-grep could not be resolved"
    assert {finding["source"] for finding in payload["findings"]} == {"taint"}
