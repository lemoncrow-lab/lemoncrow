"""Tests for the G11 security-scan capability (rule pack + bounded taint).

The taint check is pure-Python (no binary). The rule-pack assertions exercise
the real ast-grep binary and skip if it is unavailable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.capabilities.security import (
    BUNDLED_RULES,
    SecurityScanner,
    analyze_python_source,
    scan_repository,
)
from atelier.infra.code_intel.astgrep.binaries import discover_astgrep_binary

_HAS_ASTGREP = discover_astgrep_binary(Path.cwd(), allow_bootstrap=True).available
_SKIP_REASON = "ast-grep binary unavailable in this environment"

_VULN_SOURCE = """\
import subprocess


def handle(request):
    name = request.args.get("name")
    subprocess.run("echo " + name, shell=True)
    cmd = "ls " + name
    eval(cmd)


def run_sql(db, request):
    q = "SELECT * FROM users WHERE n = \'" + request.args["n"] + "\'"
    db.execute(q)


PASSWORD = "hunter2supersecret"
"""

_SAFE_SOURCE = """\
import subprocess


def handle(request):
    name = request.args.get("name")
    subprocess.run(["echo", name])


def run_sql(db, request):
    db.execute("SELECT * FROM users WHERE n = ?", (request.args["n"],))


def compute(x):
    return int(x) + 1
"""


def _write_pair(tmp_path: Path) -> None:
    (tmp_path / "vuln.py").write_text(_VULN_SOURCE, encoding="utf-8")
    (tmp_path / "safe.py").write_text(_SAFE_SOURCE, encoding="utf-8")


# --- bundled rule metadata -------------------------------------------------


def test_bundled_rules_have_unique_ids_and_metadata() -> None:
    ids = [rule.rule_id for rule in BUNDLED_RULES]
    assert len(ids) == len(set(ids)), "duplicate rule ids"
    for rule in BUNDLED_RULES:
        assert rule.cwe.startswith("CWE-")
        assert rule.severity in {"error", "warning", "info"}
        assert rule.confidence in {"high", "medium", "low"}
        ast_rule = rule.to_astgrep_rule()
        assert ast_rule["id"] == rule.rule_id
        assert "rule" in ast_rule


# --- bounded taint analysis (no binary required) ---------------------------


def test_taint_links_request_source_to_subprocess_sink() -> None:
    findings = analyze_python_source(_VULN_SOURCE, file_path="vuln.py")
    rule_ids = {f.rule_id for f in findings}
    assert "taint-subprocess" in rule_ids
    assert "taint-eval" in rule_ids
    assert "taint-sql-execute" in rule_ids
    for finding in findings:
        assert finding.heuristic is True
        assert finding.confidence in {"high", "medium", "low"}


def test_taint_does_not_flag_safe_source() -> None:
    findings = analyze_python_source(_SAFE_SOURCE, file_path="safe.py")
    assert findings == []


def test_taint_requires_a_real_source() -> None:
    # A local literal feeding eval is not tainted -> no source-to-sink link.
    src = "def f():\n    cmd = 'ls'\n    eval(cmd)\n"
    assert analyze_python_source(src, file_path="x.py") == []


def test_taint_fail_open_on_syntax_error() -> None:
    assert analyze_python_source("def (:", file_path="bad.py") == []


# --- full scanner (rule pack + taint, needs binary) ------------------------


@pytest.mark.skipif(not _HAS_ASTGREP, reason=_SKIP_REASON)
def test_scanner_flags_vulnerable_file(tmp_path: Path) -> None:
    _write_pair(tmp_path)
    findings = scan_repository(tmp_path, paths=["vuln.py"])
    rule_ids = {f["rule_id"] for f in findings}
    # rule-pack hits
    assert "py-subprocess-shell-true" in rule_ids
    assert "py-eval-exec" in rule_ids
    assert "py-hardcoded-secret" in rule_ids
    # taint hits
    assert "taint-subprocess" in rule_ids
    assert "taint-sql-execute" in rule_ids
    for finding in findings:
        assert finding["path"] == "vuln.py"
        assert finding["line"] >= 1
        assert "severity" in finding and "confidence" in finding


@pytest.mark.skipif(not _HAS_ASTGREP, reason=_SKIP_REASON)
def test_scanner_does_not_flag_safe_file(tmp_path: Path) -> None:
    _write_pair(tmp_path)
    findings = scan_repository(tmp_path, paths=["safe.py"])
    assert findings == [], findings


@pytest.mark.skipif(not _HAS_ASTGREP, reason=_SKIP_REASON)
def test_scanner_rule_only_pass_excludes_taint(tmp_path: Path) -> None:
    _write_pair(tmp_path)
    scanner = SecurityScanner(tmp_path)
    findings = scanner.scan(paths=["vuln.py"], include_taint=False)
    assert all(f.source == "rule" for f in findings)
    assert any(f.rule_id == "py-eval-exec" for f in findings)
