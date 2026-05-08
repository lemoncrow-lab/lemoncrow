"""Tests for FailureAnalyzer clustering and proposal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atelier.core.improvement.failure_analyzer import FailureAnalyzer, analyze_failures


def _snap(run_id: str, env: str, error_sig: str, status: str = "failed") -> dict[str, Any]:
    return {
        "run_id": run_id,
        "environment_id": env,
        "status": status,
        "events": [
            {
                "kind": "command_result",
                "at": "2026-01-01T00:00:00+00:00",
                "summary": "pytest",
                "payload": {"ok": False, "error_signature": error_sig},
            }
        ],
    }


def test_analyze_clusters_by_env_and_fingerprint() -> None:
    snaps = [
        _snap("r1", "env_debugging_loop", "errA"),
        _snap("r2", "env_debugging_loop", "errA"),
        _snap("r3", "env_debugging_loop", "errB"),
    ]
    clusters = analyze_failures(snaps)
    assert any(len(c.trace_ids) == 2 for c in clusters)
    assert any(c.fingerprint == "errA" for c in clusters)
    assert any(c.fingerprint == "errB" for c in clusters)


def test_analyzer_proposes_concrete_fields() -> None:
    snaps = [_snap("r1", "env_debugging_loop", "errA")]
    clusters = analyze_failures(snaps)
    assert clusters
    c = clusters[0]
    assert c.suggested_block_title
    assert c.suggested_rubric_check
    assert c.suggested_eval_case


def test_analyzer_loads_from_runs_dir(tmp_path: Path) -> None:
    runs = tmp_path / "runs"
    runs.mkdir()
    (runs / "r1.json").write_text(json.dumps(_snap("r1", "x", "sig")), encoding="utf-8")
    fa = FailureAnalyzer(runs)
    clusters = fa.analyze()
    assert clusters and clusters[0].fingerprint == "sig"
