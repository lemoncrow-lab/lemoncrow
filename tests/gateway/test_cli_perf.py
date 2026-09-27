"""Tests for `lc perf` (MCP tool latency profiling).

The actual latency measurement (run_profile -> _handle) is a real-system probe and
is not unit-tested; everything around it -- drift detection, the noise floor, the
regression breakdown, history I/O, and the baseline guard -- is, with synthetic
records and a tmp history file so the suite never profiles for real or writes the
tracked history.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from lemoncrow.gateway.cli import cli
from lemoncrow.gateway.cli.commands import _mcp_profile as P
from lemoncrow.gateway.cli.commands import perf as perf_mod


def _tool(
    warm: float, *, cold: float = 5.0, handler: float | None = None, overhead: float | None = None
) -> dict[str, Any]:
    return {
        "cold_ms": cold,
        "warm_ms": warm,
        "warm_p95_ms": warm,
        "runs": 5,
        "handler_ms": handler if handler is not None else round(warm * 0.8, 1),
        "overhead_ms": overhead if overhead is not None else round(warm * 0.2, 1),
    }


def _rec(repo: str, *, sha: str = "abc", tools: dict[str, Any] | None = None, ts: float = 1000.0) -> dict[str, Any]:
    return {"ts": ts, "git_sha": sha, "git_branch": "test", "repo": repo, "tools": tools or {}, "skipped": {}}


# --------------------------------------------------------------------------- #
# render_drift -- pure function over two run records                          #
# --------------------------------------------------------------------------- #


def test_noise_below_abs_floor_is_not_a_regression() -> None:
    # +3ms is +37% (over the 25% threshold) but under the 10ms floor -> not flagged.
    cur = _rec("/r", tools={"search": _tool(11.0)})
    prev = _rec("/r", tools={"search": _tool(8.0)})
    text, regressed = P.render_drift(cur, prev, threshold=25.0, min_abs_ms=10.0)
    assert regressed is False
    assert "REGRESS" not in text


def test_real_regression_is_flagged_with_handler_overhead_breakdown() -> None:
    cur = _rec("/r", tools={"explore": _tool(120.0, handler=110.0, overhead=10.0)})
    prev = _rec("/r", tools={"explore": _tool(20.0, handler=12.0, overhead=8.0)})
    text, regressed = P.render_drift(cur, prev, threshold=25.0, min_abs_ms=10.0)
    assert regressed is True
    assert "REGRESS" in text
    assert "Regression breakdown" in text
    # the breakdown attributes the time to handler vs pipeline overhead
    assert "handler" in text and "pipeline overhead" in text


def test_large_improvement_marked_faster_not_regression() -> None:
    cur = _rec("/r", tools={"read": _tool(30.0)})
    prev = _rec("/r", tools={"read": _tool(100.0)})
    text, regressed = P.render_drift(cur, prev, threshold=25.0, min_abs_ms=10.0)
    assert regressed is False
    assert "faster" in text


def test_new_tool_has_no_prior() -> None:
    text, regressed = P.render_drift(_rec("/r", tools={"graph": _tool(40.0)}), None, threshold=25.0)
    assert regressed is False
    assert "new" in text
    assert "baseline only" in text


def test_different_commit_is_noted() -> None:
    cur = _rec("/r", sha="bbb", tools={"read": _tool(50.0)})
    prev = _rec("/r", sha="aaa", tools={"read": _tool(50.0)})
    text, _ = P.render_drift(cur, prev, threshold=25.0)
    assert "different commit" in text


# --------------------------------------------------------------------------- #
# history I/O                                                                  #
# --------------------------------------------------------------------------- #


def test_append_and_load_last_run_is_repo_scoped(tmp_path: Path) -> None:
    hist = tmp_path / "h.jsonl"
    P.append_history(hist, _rec("/r", sha="a", tools={"read": _tool(50.0)}))
    P.append_history(hist, _rec("/r", sha="b", tools={"read": _tool(60.0)}))
    P.append_history(hist, _rec("/other", sha="x", tools={"read": _tool(99.0)}))
    last = P.load_last_run(hist, "/r")
    assert last is not None
    assert last["git_sha"] == "b"  # newest for THIS repo, not the /other run


def test_summarize_history(tmp_path: Path) -> None:
    hist = tmp_path / "h.jsonl"
    P.append_history(hist, _rec("/r", tools={"read": _tool(50.0)}))
    out = P.summarize_history(hist, "/r", last=10)
    assert "read" in out and "50.0" in out


# --------------------------------------------------------------------------- #
# CLI baseline guard -- mocked run_profile, tmp history (no real profiling)    #
# --------------------------------------------------------------------------- #


def _patch_run_profile(monkeypatch: pytest.MonkeyPatch, warms: list[float]) -> None:
    it = iter(warms)

    def fake(repo: Path, *, warmup: int, runs: int, include_edit: bool = True) -> dict[str, Any]:
        return _rec(str(repo), sha="abc", tools={"search": _tool(next(it))})

    monkeypatch.setattr(perf_mod, "run_profile", fake)


def test_append_skips_a_regressed_run_unless_forced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hist = tmp_path / "h.jsonl"
    _patch_run_profile(monkeypatch, [50.0, 200.0, 200.0])
    runner = CliRunner()
    base = ["perf", "append", "--repo", str(tmp_path), "--history", str(hist), "--no-edit"]

    r1 = runner.invoke(cli, base)  # baseline (no prior) -> recorded
    assert r1.exit_code == 0, r1.output
    assert hist.read_text(encoding="utf-8").count("\n") == 1

    r2 = runner.invoke(cli, base)  # 50 -> 200 regression -> NOT recorded
    assert r2.exit_code == 0, r2.output
    assert "NOT recorded" in r2.output
    assert hist.read_text(encoding="utf-8").count("\n") == 1  # baseline kept

    r3 = runner.invoke(cli, [*base, "--force"])  # forced -> recorded despite regression
    assert r3.exit_code == 0, r3.output
    assert hist.read_text(encoding="utf-8").count("\n") == 2


def test_run_never_writes_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    hist = tmp_path / "h.jsonl"
    _patch_run_profile(monkeypatch, [50.0])
    runner = CliRunner()
    r = runner.invoke(cli, ["perf", "run", "--repo", str(tmp_path), "--history", str(hist), "--no-edit"])
    assert r.exit_code == 0, r.output
    assert not hist.exists()  # `run` is read-only
