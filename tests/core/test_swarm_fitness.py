from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.swarm.capability import build_swarm_spec_payload
from lemoncrow.pro.capabilities.swarm.fitness import (
    FitnessSpec,
    beats_baseline,
    build_fitness_spec,
    evaluate_candidate,
    improvement,
    measure_baseline,
    parse_metric,
)
from lemoncrow.pro.capabilities.swarm.models import SwarmChildState, SwarmRunState, SwarmWaveState
from lemoncrow.pro.capabilities.swarm.reducers import WaveContext, get_reducer


@pytest.mark.parametrize(
    ("stdout", "exit_code", "spec", "expected"),
    [
        ('{"savings_pct": 53.5, "correct": true}', 0, "json:savings_pct", 53.5),
        ('logs...\n{"a": {"b": [1, 2, 7]}}', 0, "json:a.b.2", 7.0),
        ("final cost = 3.14 usd", 0, "stdout_float", 3.14),
        ("savings=42% of base", 0, r"regex:savings=(\d+)", 42.0),
        ("", 5, "exit_code", 5.0),
    ],
)
def test_parse_metric_modes(stdout: str, exit_code: int, spec: str, expected: float) -> None:
    value, err = parse_metric(stdout, exit_code, spec)
    assert err == ""
    assert value == expected


def test_parse_metric_reports_errors() -> None:
    value, err = parse_metric("not json", 0, "json:missing.key")
    assert value is None
    assert err
    value, err = parse_metric("no numbers here", 0, "stdout_float")
    assert value is None and err
    value, err = parse_metric("x", 0, "bogus_mode")
    assert value is None and "unknown metric_parse" in err


def test_improvement_and_margin_honor_direction() -> None:
    lo = FitnessSpec(metric_command="x", direction="min", baseline=10.0, improve_margin=1.0)
    assert improvement(lo, 4.0, 10.0) == 6.0
    assert beats_baseline(lo, 9.0, 10.0) is True  # 1.0 gain == margin
    assert beats_baseline(lo, 9.5, 10.0) is False  # 0.5 gain < margin
    hi = FitnessSpec(metric_command="x", direction="max", baseline=10.0)
    assert improvement(hi, 12.0, 10.0) == 2.0
    assert beats_baseline(hi, 8.0, 10.0) is False


def test_gate_blocks_metric(tmp_path: Path) -> None:
    (tmp_path / "value").write_text("3\n")
    spec = FitnessSpec(metric_command="cat value", gate_command="exit 1", direction="min", baseline=10.0)
    result = evaluate_candidate(spec, tmp_path)
    assert result.gate_passed is False
    assert result.metric is None


def test_measure_baseline(tmp_path: Path) -> None:
    (tmp_path / "value").write_text("7.5\n")
    spec = FitnessSpec(metric_command="cat value", direction="min")
    assert measure_baseline(spec, tmp_path) == 7.5


def _make_measured_child(tmp_path: Path, child_id: str, metric_value: str) -> SwarmChildState:
    worktree = tmp_path / child_id
    worktree.mkdir(parents=True, exist_ok=True)
    (worktree / "value").write_text(f"{metric_value}\n")
    run_dir = tmp_path / f"{child_id}-run"
    run_dir.mkdir(parents=True, exist_ok=True)
    return SwarmChildState(
        child_id=child_id,
        label=child_id,
        wave_index=1,
        status="success",
        worktree_path=str(worktree),
        lemoncrow_root=str(run_dir / "lemoncrow-root"),
        run_dir=str(run_dir),
        spec_path=str(run_dir / "program.md"),
        result_path=str(run_dir / "result.json"),
        stdout_path=str(run_dir / "stdout.log"),
        stderr_path=str(run_dir / "stderr.log"),
        metadata_path=str(run_dir / "meta.json"),
        patch_path=str(run_dir / "candidate.patch"),
        files_changed=[" M value"],
    )


def _measured_state(tmp_path: Path) -> SwarmRunState:
    return SwarmRunState(
        run_id="swarm-fit",
        repo_root=str(tmp_path),
        base_worktree=str(tmp_path),
        base_ref="HEAD",
        worktree_pool=str(tmp_path / "pool"),
        copied_spec_path=str(tmp_path / "program.md"),
        child_command=["true"],
        reducer_name="best",
        job_kind="optimize",
        fitness_spec=FitnessSpec(
            objective="minimize value",
            metric_command="cat value",
            metric_parse="stdout_float",
            direction="min",
            baseline=10.0,
        ),
    )


def test_best_reducer_selects_lowest_metric(tmp_path: Path) -> None:
    state = _measured_state(tmp_path)
    children = [
        _make_measured_child(tmp_path, "wave-01-run-01", "8"),
        _make_measured_child(tmp_path, "wave-01-run-02", "3"),  # best for direction=min
        _make_measured_child(tmp_path, "wave-01-run-03", "12"),  # worse than baseline
    ]
    wave = SwarmWaveState(wave_index=1)
    evaluation = get_reducer("best").reduce(children, WaveContext(state=state, wave=wave))

    assert evaluation.accepted_child_ids == ["wave-01-run-02"]
    assert evaluation.candidate_order[0] == "wave-01-run-02"  # best ranked first
    by_id = {c.child_id: c for c in children}
    assert by_id["wave-01-run-02"].metric == 3.0
    assert by_id["wave-01-run-02"].gate_passed is True
    # the candidate worse than baseline is rejected
    assert "wave-01-run-03" in evaluation.rejected_child_ids


def test_best_reducer_converges_when_nothing_beats_baseline(tmp_path: Path) -> None:
    state = _measured_state(tmp_path)
    children = [
        _make_measured_child(tmp_path, "wave-01-run-01", "15"),
        _make_measured_child(tmp_path, "wave-01-run-02", "11"),
    ]
    wave = SwarmWaveState(wave_index=1)
    evaluation = get_reducer("best").reduce(children, WaveContext(state=state, wave=wave))
    assert evaluation.accepted_child_ids == []
    assert evaluation.verdict == "converged"


def test_build_fitness_spec_from_loose_inputs() -> None:
    assert build_fitness_spec(metric_command="  ") is None  # no metric => not an optimize job
    auto = build_fitness_spec(metric_command="cat v", baseline="auto")
    assert auto is not None and auto.baseline == "auto"
    numeric = build_fitness_spec(
        metric_command="cat v", metric_parse="json:cost", direction="max", baseline="42.5", gate_command="true"
    )
    assert numeric is not None
    assert numeric.baseline == 42.5
    assert numeric.direction == "max"
    assert numeric.gate_command == "true"


def test_build_swarm_spec_payload_surfaces_knobs(tmp_path: Path) -> None:
    state = _measured_state(tmp_path)
    state.exec_mode = "edit"
    state.search_space = ["src/**"]
    payload = build_swarm_spec_payload(state)
    assert payload["reducer"] == "best"
    assert payload["exec_mode"] == "edit"
    assert payload["job_kind"] == "optimize"
    assert payload["search_space"] == ["src/**"]
    assert payload["fitness"]["metric_command"] == "cat value"


def test_best_reducer_falls_back_to_heuristic_without_fitness(tmp_path: Path) -> None:
    state = _measured_state(tmp_path)
    state.fitness_spec = None  # no measured fitness -> heuristic deterministic selection
    children = [_make_measured_child(tmp_path, "wave-01-run-01", "8")]
    wave = SwarmWaveState(wave_index=1)
    evaluation = get_reducer("best").reduce(children, WaveContext(state=state, wave=wave))
    # heuristic fallback accepts a successful, changed, validated-or-structural candidate
    assert evaluation.status == "fallback"
