"""Tests for ab.aggregate — AB-05 (Wilson CI math and summary schema)."""

import json
import pathlib
import tempfile

from ab.aggregate import compute_summary, wilson_score_ci


def test_wilson_ci_degenerate():
    assert wilson_score_ci(0, 0) == (0.0, 1.0)


def test_wilson_ci_bounds_always_valid():
    cases = [(0, 5), (1, 5), (2, 5), (3, 5), (4, 5), (5, 5), (0, 1), (1, 1), (1, 10), (10, 10)]
    for k, n in cases:
        lo, hi = wilson_score_ci(k, n)
        assert 0.0 <= lo <= hi <= 1.0, f"bounds violated k={k} n={n}: ({lo},{hi})"


def test_wilson_ci_known_values():
    lo, hi = wilson_score_ci(3, 5)
    # Wilson CI for k=3, n=5, z=1.96: approx (0.231, 0.882)
    assert abs(lo - 0.231) < 0.01 and abs(hi - 0.882) < 0.01, f"k=3,n=5 got ({lo:.3f},{hi:.3f})"

    lo5, hi5 = wilson_score_ci(5, 5)
    assert lo5 > 0.45 and hi5 == 1.0, f"k=5,n=5 expected lo>0.45 hi=1.0, got ({lo5},{hi5})"

    lo0, hi0 = wilson_score_ci(0, 5)
    assert lo0 == 0.0 and hi0 < 0.55, f"k=0,n=5 expected lo=0 hi<0.55, got ({lo0},{hi0})"


def test_wilson_not_normal_approximation():
    """Normal approx gives hi=0.0 for k=0,n=5; Wilson must give hi>0."""
    _, hi = wilson_score_ci(0, 5)
    assert hi > 0.0, "Wilson CI must give non-zero upper bound for k=0,n=5"


def test_compute_summary_schema():
    with tempfile.TemporaryDirectory() as d:
        raw = pathlib.Path(d) / "raw"
        raw.mkdir()
        # taskA: all 3 pass
        for rep in range(1, 4):
            (raw / f"taskA__on__rep{rep}.json").write_text(json.dumps({"grader_is_resolved": True}))
        # taskB: 1 pass, 2 fail
        (raw / "taskB__on__rep1.json").write_text(json.dumps({"grader_is_resolved": True}))
        (raw / "taskB__on__rep2.json").write_text(json.dumps({"grader_is_resolved": False}))
        (raw / "taskB__on__rep3.json").write_text(json.dumps({"grader_is_resolved": False}))

        summary = compute_summary("test-run", raw)

        assert "run_id" in summary and summary["run_id"] == "test-run"
        assert "generated_at" in summary
        assert "cells" in summary

        cell_a = summary["cells"]["taskA__on"]
        assert cell_a["passed"] == 3
        assert cell_a["total"] == 3
        assert "ci_lower" in cell_a and "ci_upper" in cell_a
        assert "p_hat" not in cell_a, "AB-05: p_hat must never be stored"

        cell_b = summary["cells"]["taskB__on"]
        assert cell_b["passed"] == 1
        assert cell_b["total"] == 3


def test_compute_summary_ignores_tmp_files():
    with tempfile.TemporaryDirectory() as d:
        raw = pathlib.Path(d) / "raw"
        raw.mkdir()
        for rep in range(1, 4):
            (raw / f"taskA__on__rep{rep}.json").write_text(json.dumps({"grader_is_resolved": True}))
        # stale partial-write artifact
        (raw / "taskA__on__rep1.json.tmp").write_text(json.dumps({"grader_is_resolved": True}))

        summary = compute_summary("test-run", raw)
        assert "taskA__on" in summary["cells"]
        assert summary["cells"]["taskA__on"]["total"] == 3, "tmp file must not be counted"
