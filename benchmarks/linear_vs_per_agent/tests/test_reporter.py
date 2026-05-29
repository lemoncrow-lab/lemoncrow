"""Tests for benchmarks.linear_vs_per_agent.reporter (13-04-02).

Covers:
* Report schema (LINEAR-05, TBEVAL-01, D-15).
* Threshold pass/fail at the D-16 boundary (>=30% cost, >=25% wall).
* Cache-savings vs minify-savings decomposition (D-17).
"""

from __future__ import annotations

import json
import pathlib
import tempfile


def _write_cell(raw: pathlib.Path, scenario: str, mode: str, rep: int, **fields) -> None:
    payload = {
        "scenario_id": scenario,
        "mode": mode,
        "rep": rep,
        "cost_usd": 0.0,
        "wall_time_ms": 0.0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "cache_hit_ratio": 0.0,
        "minify_delta_tokens": 0,
        "task_success": True,
    }
    payload.update(fields)
    (raw / f"{scenario}__{mode}__rep{rep}.json").write_text(json.dumps(payload))


def test_compute_report_schema() -> None:
    from benchmarks.linear_vs_per_agent.reporter import compute_report

    with tempfile.TemporaryDirectory() as d:
        raw = pathlib.Path(d) / "raw"
        raw.mkdir()
        _write_cell(raw, "s1", "linear", 1, cost_usd=0.5, wall_time_ms=1000)
        _write_cell(raw, "s1", "per_agent", 1, cost_usd=1.0, wall_time_ms=2000)
        report = compute_report("test-run", raw)

    for key in ("run_id", "cells", "deltas", "cache_savings", "minify_savings", "thresholds"):
        assert key in report, f"missing top-level key {key!r}"
    assert report["run_id"] == "test-run"
    for k in ("cost_pass", "wall_time_pass", "success_at_least_equal"):
        assert k in report["thresholds"], f"missing threshold flag {k!r}"


def test_threshold_check_passes_at_target() -> None:
    from benchmarks.linear_vs_per_agent.reporter import compute_report

    with tempfile.TemporaryDirectory() as d:
        raw = pathlib.Path(d) / "raw"
        raw.mkdir()
        scenarios_meta = {"s1": "linear"}
        for sid, _ in scenarios_meta.items():
            # Linear: 30% cheaper, 25% faster, equal success.
            _write_cell(
                raw,
                sid,
                "linear",
                1,
                cost_usd=0.70,
                wall_time_ms=750,
                cache_read_tokens=900,
                minify_delta_tokens=100,
                task_success=True,
            )
            _write_cell(
                raw,
                sid,
                "per_agent",
                1,
                cost_usd=1.00,
                wall_time_ms=1000,
                cache_read_tokens=0,
                minify_delta_tokens=0,
                task_success=True,
            )
        report = compute_report(
            "test-run",
            raw,
            scenarios_meta=scenarios_meta,
        )

    assert report["thresholds"]["cost_pass"] is True
    assert report["thresholds"]["wall_time_pass"] is True
    assert report["thresholds"]["success_at_least_equal"] is True


def test_threshold_check_fails_below_target() -> None:
    from benchmarks.linear_vs_per_agent.reporter import compute_report

    with tempfile.TemporaryDirectory() as d:
        raw = pathlib.Path(d) / "raw"
        raw.mkdir()
        scenarios_meta = {"s1": "linear"}
        _write_cell(
            raw,
            "s1",
            "linear",
            1,
            cost_usd=0.90,
            wall_time_ms=950,
            task_success=True,
        )
        _write_cell(
            raw,
            "s1",
            "per_agent",
            1,
            cost_usd=1.00,
            wall_time_ms=1000,
            task_success=True,
        )
        report = compute_report(
            "test-run",
            raw,
            scenarios_meta=scenarios_meta,
        )

    assert report["thresholds"]["cost_pass"] is False
    assert report["thresholds"]["wall_time_pass"] is False


def test_savings_decomposition() -> None:
    from benchmarks.linear_vs_per_agent.reporter import compute_report

    with tempfile.TemporaryDirectory() as d:
        raw = pathlib.Path(d) / "raw"
        raw.mkdir()
        scenarios_meta = {"s1": "linear", "s2": "linear"}
        # s1 — no minify; pure cache reuse.
        _write_cell(
            raw,
            "s1",
            "linear",
            1,
            cost_usd=0.6,
            wall_time_ms=800,
            cache_read_tokens=1000,
            minify_delta_tokens=0,
        )
        _write_cell(
            raw,
            "s1",
            "per_agent",
            1,
            cost_usd=1.0,
            wall_time_ms=1000,
        )
        # s2 — heavy minify savings.
        _write_cell(
            raw,
            "s2",
            "linear",
            1,
            cost_usd=0.7,
            wall_time_ms=900,
            cache_read_tokens=200,
            minify_delta_tokens=2000,
        )
        _write_cell(
            raw,
            "s2",
            "per_agent",
            1,
            cost_usd=1.0,
            wall_time_ms=1000,
        )

        report = compute_report("test-run", raw, scenarios_meta=scenarios_meta)

    cs = report["cache_savings"]
    ms = report["minify_savings"]
    assert isinstance(cs, dict) and "tokens" in cs
    assert isinstance(ms, dict) and "tokens" in ms
    assert cs["tokens"] == 1200, cs
    assert ms["tokens"] == 2000, ms
    # Sum of token savings equals total token savings (D-17 decomposition).
    assert cs["tokens"] + ms["tokens"] == report["total_savings"]["tokens"]
