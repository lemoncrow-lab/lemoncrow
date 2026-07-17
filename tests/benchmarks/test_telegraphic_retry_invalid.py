"""Tests for benchmarks/telegraphic/retry_invalid.py.

`lc benchmark telegraphic` has no --resume, and codebench's own --retry-failed
only covers ok=false rows, not content-invalid (valid=false) ones -- this
script fills that gap by patching just the invalid (task, arm, rep) rows out
of the right batch's results.jsonl and re-invoking codebench.run --resume /
run_extra_arm for exactly those. All subprocess/network calls are stubbed
here -- these tests spend no tokens and touch no real batch runner.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(REPO_ROOT / "benchmarks") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "benchmarks"))

from benchmarks.telegraphic import extra_arms as extra_arms_mod  # noqa: E402
from benchmarks.telegraphic import retry_invalid as ri  # noqa: E402


def _row(task: str, arm: str, rep: int, *, valid: bool, output_tokens: int, reason: str = "") -> dict[str, Any]:
    return {
        "task": task,
        "arm": arm,
        "rep": rep,
        "ok": True,
        "is_error": False,
        "timed_out": False,
        "valid": valid,
        "validity_reason": reason,
        "output_tokens": output_tokens,
        "cost_usd": 0.01,
    }


def test_find_invalid_filters_valid_false_only() -> None:
    rows = [
        _row("local1", "baseline", 0, valid=True, output_tokens=10),
        _row("local1", "lemoncrow", 0, valid=False, output_tokens=5, reason="no task keyword overlap"),
        {**_row("local2", "baseline", 0, valid=True, output_tokens=10), "ok": False},  # different bucket, not ours
    ]
    invalid = ri.find_invalid(rows)
    assert [(r["task"], r["arm"]) for r in invalid] == [("local1", "lemoncrow")]


@pytest.fixture()
def run_dir(tmp_path: Path) -> Path:
    """A finished 2-prompt/1-batch telegraphic run: local1 all valid, local2
    invalid on lemoncrow (codebench arm) and caveman (extra arm).
    """
    d = tmp_path / "run"
    batch0 = d / "batch0"
    batch0.mkdir(parents=True)

    codebench_rows = [
        _row("local1", "baseline", 0, valid=True, output_tokens=100),
        _row("local1", "lemoncrow", 0, valid=True, output_tokens=50),
        _row("local2", "baseline", 0, valid=True, output_tokens=200),
        _row("local2", "lemoncrow", 0, valid=False, output_tokens=10, reason="no task keyword overlap"),
    ]
    (batch0 / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in codebench_rows), encoding="utf-8")

    merged_rows = [
        *codebench_rows,
        _row("local1", "caveman", 0, valid=True, output_tokens=60),
        _row("local2", "caveman", 0, valid=False, output_tokens=5, reason="off-task capability/list response"),
    ]
    (d / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in merged_rows), encoding="utf-8")
    return d


def test_dry_run_reports_invalid_and_spends_nothing(
    run_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: pytest.fail("must not spawn under --dry-run"))
    monkeypatch.setattr(
        extra_arms_mod, "run_extra_arm", lambda **kw: pytest.fail("must not call run_extra_arm under --dry-run")
    )
    monkeypatch.setattr(sys, "argv", ["retry_invalid.py", "--run-dir", str(run_dir), "--dry-run"])

    rc = ri.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "local2" in out and "lemoncrow" in out and "rep0" in out
    # results.jsonl on disk must be byte-identical -- a dry run spends and mutates nothing.
    assert len(ri._load_jsonl(run_dir / "results.jsonl")) == 6


def test_rerun_patches_only_invalid_rows_and_remerges(run_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end (subprocess + run_extra_arm stubbed, zero spend): the
    codebench-arm invalid row must be stripped from batch0/results.jsonl
    before the (stubbed) rerun, the rerun command must target exactly the
    arm(s) that were invalid in that batch, and the final merged
    results.jsonl/report must reflect both fresh rows with every other row
    untouched.
    """
    calls: list[list[str]] = []
    call_kwargs: list[dict[str, Any]] = []

    class _FakeCompleted:
        returncode = 0

    def fake_run(cmd: list[str], **kw: Any) -> _FakeCompleted:
        calls.append(cmd)
        call_kwargs.append(kw)
        # Emulate what a real `codebench.run --resume` call would append for
        # the row we just stripped out.
        batch0 = run_dir / "batch0"
        rows = ri._load_jsonl(batch0 / "results.jsonl")
        rows.append(_row("local2", "lemoncrow", 0, valid=True, output_tokens=42))
        ri._write_jsonl(batch0 / "results.jsonl", rows)
        return _FakeCompleted()

    extra_calls: list[dict[str, Any]] = []

    def fake_run_extra_arm(**kw: Any) -> dict[str, Any]:
        extra_calls.append(kw)
        return _row(kw["task_id"], kw["arm"], kw["rep"], valid=True, output_tokens=33)

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(extra_arms_mod, "run_extra_arm", fake_run_extra_arm)
    monkeypatch.setattr(sys, "argv", ["retry_invalid.py", "--run-dir", str(run_dir), "--no-capture", "-y"])

    rc = ri.main()
    assert rc == 0

    # Exactly one codebench.run subprocess call, scoped to the arm(s) that were
    # actually invalid in batch0 (lemoncrow only -- baseline was fine).
    assert len(calls) == 1
    cmd = calls[0]
    assert "lemoncrow" in cmd
    assert "baseline" not in cmd
    assert cmd.count("--prompt") == 2  # full batch (local1 + local2), not just the invalid one
    # Regression: cwd MUST be the repo root, not REPO_ROOT/"benchmarks" -- the
    # latter breaks `python -m benchmarks.codebench.run`'s package resolution
    # (ModuleNotFoundError), silently no-ops the rerun, and the invalid rows
    # just vanish from results.jsonl instead of being replaced.
    assert call_kwargs[0]["cwd"] == str(ri.REPO_ROOT)

    # Exactly one run_extra_arm call, for the invalid caveman row.
    assert len(extra_calls) == 1
    assert extra_calls[0]["task_id"] == "local2"
    assert extra_calls[0]["arm"] == "caveman"
    assert extra_calls[0]["rep"] == 0

    merged = {(r["task"], r["arm"], r["rep"]): r for r in ri._load_jsonl(run_dir / "results.jsonl")}
    assert merged[("local2", "lemoncrow", 0)]["output_tokens"] == 42
    assert merged[("local2", "lemoncrow", 0)]["valid"] is True
    assert merged[("local2", "caveman", 0)]["output_tokens"] == 33
    # Untouched rows survive as-is.
    assert merged[("local1", "baseline", 0)]["output_tokens"] == 100
    assert merged[("local2", "baseline", 0)]["output_tokens"] == 200

    assert not ri.find_invalid(list(merged.values()))
    assert (run_dir / "telegraphic_report.md").exists()
