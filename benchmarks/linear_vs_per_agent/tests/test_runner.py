"""Tests for benchmarks.linear_vs_per_agent.runner (13-04-01).

Covers:
* Resumable cells (skip-if-exists, no re-trial side-effects).
* Per-arm ATELIER_ROOT isolation (T-13-05).
* Per-cell JSON payload contains the required field set
  (LINEAR-05, TBEVAL-01).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


def _load_scenarios() -> list[dict]:
    import yaml

    src = Path(__file__).resolve().parents[1] / "scenarios.yaml"
    with src.open() as fh:
        return yaml.safe_load(fh)["scenarios"]


def test_cell_skip_on_existing_output(tmp_path: Path) -> None:
    from benchmarks.linear_vs_per_agent import runner as bench_runner

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    dest = raw_dir / "survey_plan_implement_small__linear__rep1.json"
    dest.write_text(json.dumps({"task_success": True, "mode": "linear"}))

    counter = {"calls": 0}

    def factory(scenario: dict):
        counter["calls"] += 1

        class _P:
            def complete(self, messages, *, cache_read=0, cache_write=0):
                return ("ok", 100, 50, 0, 0)

        return _P()

    result = bench_runner.run_cell(
        scenario_id="survey_plan_implement_small",
        mode="linear",
        rep=1,
        raw_dir=raw_dir,
        scenarios=_load_scenarios(),
        provider_factory=factory,
    )
    assert result is True
    assert counter["calls"] == 0, "skip-if-exists must not invoke the provider"


def test_arm_isolation_via_atelier_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from benchmarks.linear_vs_per_agent import runner as bench_runner

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    seen_roots: list[str] = []

    original = os.environ.get("ATELIER_ROOT")
    monkeypatch.delenv("ATELIER_ROOT", raising=False)

    def factory(scenario: dict):
        # Capture ATELIER_ROOT at the moment the runner builds the trial.
        seen_roots.append(os.environ.get("ATELIER_ROOT", ""))

        class _P:
            def complete(self, messages, *, cache_read=0, cache_write=0):
                return ("ok", 100, 50, 0, 0)

        return _P()

    for mode in ("linear", "per_agent"):
        bench_runner.run_cell(
            scenario_id="doc_only_task",
            mode=mode,
            rep=1,
            raw_dir=raw_dir,
            scenarios=_load_scenarios(),
            provider_factory=factory,
        )

    assert len(seen_roots) == 2
    assert seen_roots[0] != seen_roots[1], "each arm must run under a distinct ATELIER_ROOT"
    assert all(r for r in seen_roots), "ATELIER_ROOT must be set per arm"
    # Env var restored to original (None or prior value) after the trial.
    assert os.environ.get("ATELIER_ROOT") == original


def test_runner_records_required_fields(tmp_path: Path) -> None:
    from benchmarks.linear_vs_per_agent import runner as bench_runner

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    def factory(scenario: dict):
        class _P:
            def complete(self, messages, *, cache_read=0, cache_write=0):
                # First per-phase call (len==2): "cold" — no cache reuse.
                # Subsequent calls in linear arm (len>2): warm cache.
                if len(messages) <= 2:
                    return ("ok <done>", 1000, 200, 0, 100)
                return ("ok <done>", 200, 200, 800, 0)

        return _P()

    ok = bench_runner.run_cell(
        scenario_id="survey_plan_implement_small",
        mode="linear",
        rep=1,
        raw_dir=raw_dir,
        scenarios=_load_scenarios(),
        provider_factory=factory,
    )
    assert ok is True

    dest = raw_dir / "survey_plan_implement_small__linear__rep1.json"
    payload = json.loads(dest.read_text())
    required = {
        "cost_usd",
        "wall_time_ms",
        "cache_read_tokens",
        "cache_write_tokens",
        "cache_hit_ratio",
        "minify_delta_tokens",
        "task_success",
        "mode",
    }
    assert required.issubset(payload), f"missing fields: {required - set(payload)}"
    assert isinstance(payload["cost_usd"], int | float)
    assert isinstance(payload["wall_time_ms"], int | float)
    assert isinstance(payload["cache_read_tokens"], int)
    assert isinstance(payload["cache_write_tokens"], int)
    assert isinstance(payload["cache_hit_ratio"], int | float)
    assert isinstance(payload["minify_delta_tokens"], int)
    assert isinstance(payload["task_success"], bool)
    assert payload["mode"] == "linear"
