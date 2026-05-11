"""Tests for the SWE-bench harness (offline, mock-only)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from benchmarks.swe import BenchConfig, Mode, load_config, mode_specs
from benchmarks.swe.agent_runner import MockAgent, build_agent
from benchmarks.swe.config import BenchConfig as Cfg
from benchmarks.swe.datasets import _MOCK_TASKS, gold_patch_lookup, load_tasks
from benchmarks.swe.metrics import RunMetrics, aggregate, read_metrics, write_metrics
from benchmarks.swe.modes import get_spec
from benchmarks.swe.patch_export import export_predictions
from benchmarks.swe.prompts import system_prompt, task_prompt
from benchmarks.swe.report import write_combined_report
from benchmarks.swe.run_swe_bench import cli
from benchmarks.swe.swebench_eval import mock_evaluate
from benchmarks.swe.task_runner import run_one

# ----------------------------- config ------------------------------------- #


def test_config_loads_lite_20() -> None:
    cfg = load_config("benchmarks/swe/configs/lite_20.yaml")
    assert isinstance(cfg, BenchConfig)
    assert cfg.task_limit == 20
    assert Mode.VANILLA in cfg.modes


def test_config_rejects_unknown_field(tmp_path: Path) -> None:
    from pydantic import ValidationError

    p = tmp_path / "bad.yaml"
    p.write_text("dataset_name: x\nmystery: 1\n")
    with pytest.raises(ValidationError):
        load_config(p)


def test_config_requires_modes(tmp_path: Path) -> None:
    from pydantic import ValidationError

    p = tmp_path / "empty.yaml"
    p.write_text("modes: []\n")
    with pytest.raises(ValidationError):
        load_config(p)


def test_warm_reasonblocks_requires_path() -> None:
    cfg = Cfg(modes=[Mode.ATELIER_WARM_REASONBLOCKS], dataset_name="mock")
    assert cfg.warm_required_but_missing()
    cfg2 = Cfg(modes=[Mode.ATELIER_WARM_REASONBLOCKS], warm_reasonblocks_path="x.json", dataset_name="mock")
    assert not cfg2.warm_required_but_missing()


# ----------------------------- modes ------------------------------------- #


def test_modes_are_distinct() -> None:
    specs = mode_specs()
    assert len(specs) == 5
    seen = {(s.mcp_available, s.forced_steps, s.enable_run_ledger, s.requires_warm_blocks) for s in specs.values()}
    assert len(seen) == 5  # five truly different mode shapes


def test_vanilla_has_no_atelier_features() -> None:
    s = get_spec(Mode.VANILLA)
    assert s.mcp_available is False
    assert s.forced_steps == ()


# ----------------------------- datasets ---------------------------------- #


def test_mock_dataset_loads_offline() -> None:
    cfg = Cfg(dataset_name="mock", task_limit=2)
    tasks = load_tasks(cfg)
    assert len(tasks) == 2
    assert all(t.instance_id.startswith("mock__") for t in tasks)


def test_agent_payload_strips_gold() -> None:
    cfg = Cfg(dataset_name="mock", task_limit=3)
    for t in load_tasks(cfg):
        payload = t.to_agent_payload()
        for forbidden in ("patch", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS"):
            assert forbidden not in payload


def test_no_gold_leakage_through_mock_dataset() -> None:
    # Mock dataset has 'patch' fields; load_tasks must not surface them via Task fields.
    raw = _MOCK_TASKS[0]
    assert "patch" in raw
    cfg = Cfg(dataset_name="mock", task_limit=1)
    tasks = load_tasks(cfg)
    assert tasks[0].instance_id == raw["instance_id"]
    payload = tasks[0].to_agent_payload()
    assert "patch" not in payload


def test_custom_jsonl_dataset(tmp_path: Path) -> None:
    p = tmp_path / "tasks.jsonl"
    p.write_text(
        json.dumps(
            {
                "instance_id": "custom__1",
                "repo": "x/y",
                "base_commit": "abc",
                "problem_statement": "fix it",
            }
        )
        + "\n"
    )
    cfg = Cfg(dataset_name="custom", custom_tasks_path=str(p), task_limit=10)
    tasks = load_tasks(cfg)
    assert tasks[0].instance_id == "custom__1"


# ----------------------------- agent + runner ---------------------------- #


def test_mock_agent_produces_patch() -> None:
    cfg = Cfg(dataset_name="mock", task_limit=1)
    agent = build_agent(cfg)
    assert isinstance(agent, MockAgent)
    task = load_tasks(cfg)[0]
    spec = get_spec(Mode.ATELIER_FORCED_WORKFLOW)
    res = agent.solve(task, spec, cfg)
    assert res.patch
    assert res.tokens_input > 0
    assert res.estimated_cost_usd > 0


def test_run_one_writes_metrics(tmp_path: Path) -> None:
    cfg = Cfg(dataset_name="mock", task_limit=1, output_dir=str(tmp_path))
    agent = build_agent(cfg)
    task = load_tasks(cfg)[0]
    m = run_one(task=task, mode=Mode.ATELIER_FULL_RUNTIME, attempt=1, cfg=cfg, agent=agent, out_dir=tmp_path)
    assert isinstance(m, RunMetrics)
    assert m.patch_generated
    assert m.tool_calls > 0
    # Atelier-specific counters populated for full runtime.
    assert m.monitor_events >= 0
    assert Path(m.patch_path or "").is_file()


# ----------------------------- metrics + report -------------------------- #


def test_metrics_jsonl_roundtrip(tmp_path: Path) -> None:
    rows = [
        RunMetrics(task_id="a", mode="vanilla", resolved=True, tokens_input=10),
        RunMetrics(task_id="b", mode="vanilla", tokens_input=20),
    ]
    p = tmp_path / "metrics.jsonl"
    write_metrics(rows, p)
    out = read_metrics(p)
    assert [r.task_id for r in out] == ["a", "b"]
    agg = aggregate(out)
    assert agg["vanilla"]["attempts"] == 2
    assert agg["vanilla"]["resolve_rate"] == 0.5


def test_predictions_jsonl_format(tmp_path: Path) -> None:
    p = export_predictions(
        [("foo__1", "claude:c-1:vanilla", "diff text")],
        tmp_path / "predictions.jsonl",
    )
    rec = json.loads(p.read_text().splitlines()[0])
    assert set(rec.keys()) == {"instance_id", "model_name_or_path", "model_patch"}


def test_report_renders_modes(tmp_path: Path) -> None:
    rows = [
        RunMetrics(task_id="a", mode="vanilla", resolved=False, estimated_cost_usd=0.1),
        RunMetrics(
            task_id="a",
            mode="atelier_full_runtime",
            resolved=True,
            estimated_cost_usd=0.05,
            reasonblock_hits=2,
        ),
    ]
    write_metrics([rows[0]], tmp_path / "metrics_vanilla.jsonl")
    write_metrics([rows[1]], tmp_path / "metrics_atelier_full_runtime.jsonl")
    md, js = write_combined_report(tmp_path, {"dataset_name": "mock"})
    text = md.read_text()
    assert "vanilla" in text and "atelier_full_runtime" in text
    payload = json.loads(js.read_text())
    assert "by_mode" in payload


# ----------------------------- prompts ----------------------------------- #


def test_prompt_changes_with_mode() -> None:
    p_van = system_prompt(Mode.VANILLA)
    p_forced = system_prompt(Mode.ATELIER_FORCED_WORKFLOW)
    assert "MUST" in p_forced and "MUST" not in p_van
    assert "Problem" in task_prompt("x")


# ----------------------------- evaluator -------------------------------- #


def test_swebench_eval_skips_cleanly_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Force the "not installed" branch even if the package happens to be present.
    import benchmarks.swe.swebench_eval as ev

    monkeypatch.setattr(ev, "is_available", lambda: False)
    p = tmp_path / "predictions.jsonl"
    p.write_text(json.dumps({"instance_id": "x", "model_name_or_path": "m", "model_patch": ""}) + "\n")
    out = ev.evaluate(p, dataset_name="swe_bench_lite", session_id="t")
    assert out["status"] == "skipped"


def test_mock_evaluator_scores_text_equality(tmp_path: Path) -> None:
    p = tmp_path / "predictions.jsonl"
    p.write_text(
        json.dumps({"instance_id": "x", "model_name_or_path": "m", "model_patch": "PATCH"})
        + "\n"
        + json.dumps({"instance_id": "y", "model_name_or_path": "m", "model_patch": "WRONG"})
        + "\n"
    )
    out = mock_evaluate(p, {"x": "PATCH", "y": "RIGHT"})
    assert out["status"] == "mock_ok"
    assert out["resolved"] == ["x"]
    assert out["failed"] == ["y"]


# ------------------------------- CLI ------------------------------------- #


def test_cli_show_modes() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["swe", "show-modes"])
    assert result.exit_code == 0
    assert "vanilla" in result.output
    assert "atelier_warm_reasonblocks" in result.output


def test_cli_run_end_to_end(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "dataset_name": "mock",
                "task_limit": 2,
                "agent_host": "mock",
                "model": "mock-1",
                "modes": ["vanilla", "atelier_forced_workflow"],
                "attempts_per_task": 1,
                "output_dir": str(tmp_path / "out"),
            }
        )
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["swe", "run", "--config", str(cfg_path)])
    assert result.exit_code == 0, result.output
    runs = list((tmp_path / "out").iterdir())
    assert runs, "expected one timestamped run dir"
    rd = runs[0]
    assert (rd / "metrics_vanilla.jsonl").is_file()
    assert (rd / "metrics_atelier_forced_workflow.jsonl").is_file()
    assert (rd / "predictions_vanilla.jsonl").is_file()
    assert (rd / "predictions_atelier_forced_workflow.jsonl").is_file()
    assert (rd / "report.md").is_file()
    assert (rd / "report.json").is_file()
    # Mode rows are different objects (agent_runner makes them distinct).
    rows_v = read_metrics(rd / "metrics_vanilla.jsonl")
    rows_f = read_metrics(rd / "metrics_atelier_forced_workflow.jsonl")
    assert rows_v[0].tool_calls == 0
    assert rows_f[0].tool_calls > 0


def test_cli_run_blocks_warm_without_path(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "dataset_name": "mock",
                "task_limit": 1,
                "modes": ["atelier_warm_reasonblocks"],
                "output_dir": str(tmp_path / "out"),
            }
        )
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["swe", "run", "--config", str(cfg_path)])
    assert result.exit_code != 0
    assert "warm_reasonblocks_path" in result.output


def test_cli_evaluate_mock(tmp_path: Path) -> None:
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "dataset_name": "mock",
                "task_limit": 1,
                "modes": ["vanilla"],
                "output_dir": str(tmp_path / "out"),
            }
        )
    )
    runner = CliRunner()
    runner.invoke(cli, ["swe", "run", "--config", str(cfg_path)])
    rd = next((tmp_path / "out").iterdir())
    result = runner.invoke(cli, ["swe", "evaluate", "--run-dir", str(rd), "--mock"])
    assert result.exit_code == 0, result.output
    payload = json.loads((rd / "evaluation.json").read_text())
    assert "predictions_vanilla" in payload


def test_no_gold_in_predictions(tmp_path: Path) -> None:
    """Sanity: predictions JSONL must not echo the gold patch back."""
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "dataset_name": "mock",
                "task_limit": 3,
                "modes": ["vanilla"],
                "output_dir": str(tmp_path / "out"),
            }
        )
    )
    runner = CliRunner()
    runner.invoke(cli, ["swe", "run", "--config", str(cfg_path)])
    rd = next((tmp_path / "out").iterdir())
    preds = (rd / "predictions_vanilla.jsonl").read_text().splitlines()
    cfg = Cfg(dataset_name="mock", task_limit=10)
    gold = gold_patch_lookup(cfg)
    # Vanilla mode emits its own patches; for tasks 2/3 those are stub patches
    # not equal to the gold patch.
    for line in preds:
        rec = json.loads(line)
        if rec["instance_id"] not in {"mock__add-1"}:
            assert rec["model_patch"] != gold.get(rec["instance_id"], "__never__")
