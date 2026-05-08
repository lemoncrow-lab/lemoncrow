"""CLI tests for V2 commands: ledger, compress, env, failure, eval, read, savings."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner, Result

from atelier.gateway.adapters.cli import cli
from atelier.infra.runtime.run_ledger import RunLedger


def _invoke(root: Path, *args: str, input: str | None = None) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), *args], input=input)


def _seed_ledger(root: Path, run_id: str = "run1") -> Path:
    led = RunLedger(run_id=run_id, agent="codex", task="t", domain="d", root=root)
    led.record_command("pytest", ok=False, error_signature="sig1")
    led.record_command("pytest", ok=False, error_signature="sig1")
    led.record_alert("repeated_command_failure", "high", "pytest x2")
    path: Path = led.persist()
    return path


def test_ledger_show_and_summarize(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    _seed_ledger(root)
    res = _invoke(root, "ledger", "show", "--json")
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["run_id"] == "run1"

    res2 = _invoke(root, "ledger", "summarize")
    assert res2.exit_code == 0
    assert "Atelier compact state" in res2.output


def test_compress_context_cli(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    _seed_ledger(root)
    res = _invoke(root, "compress-context", "--json")
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert "preserved" in payload
    assert payload["error_fingerprints"]


def test_env_list_and_show(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    res = _invoke(root, "env", "list", "--json")
    assert res.exit_code == 0
    ids = {e["id"] for e in json.loads(res.output)}
    assert "env_state_change_safety" in ids

    res2 = _invoke(root, "env", "show", "env_state_change_safety")
    assert res2.exit_code == 0
    assert "rubric_id: rubric_state_change_safety" in res2.output


def test_env_context_emits_rubric_and_blocks(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    res = _invoke(root, "env", "context", "env_state_change_safety", "--json")
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["environment"]["id"] == "env_state_change_safety"
    assert payload["rubric"] is not None


def test_failure_list_accept_reject(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    _seed_ledger(root)
    _seed_ledger(root, run_id="run2")

    res = _invoke(root, "failure", "list", "--json")
    assert res.exit_code == 0
    clusters = json.loads(res.output)
    assert clusters
    cid = clusters[0]["id"]

    res2 = _invoke(root, "failure", "accept", cid)
    assert res2.exit_code == 0
    res3 = _invoke(root, "failure", "list", "--json")
    payload = json.loads(res3.output)
    assert any(c["status"] == "accepted" for c in payload)

    res4 = _invoke(root, "failure", "reject", cid)
    assert res4.exit_code == 0


def test_analyze_failures_cli(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    _seed_ledger(root)
    res = _invoke(root, "analyze-failures", "--json")
    assert res.exit_code == 0
    assert json.loads(res.output)


def test_eval_lifecycle(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    eval_dir = root / "evals"
    eval_dir.mkdir(parents=True, exist_ok=True)
    case = {
        "id": "case1",
        "domain": "state.change",
        "description": "blocks slug-only identity plan",
        "task": "Fix external state",
        "plan": ["Resolve target from URL slug alone"],
        "expected_status": "blocked",
        "status": "draft",
    }
    (eval_dir / "case1.json").write_text(json.dumps(case), encoding="utf-8")

    res = _invoke(root, "eval", "list", "--json")
    assert res.exit_code == 0
    assert json.loads(res.output)

    res2 = _invoke(root, "eval", "run", "--case", "case1", "--json")
    assert res2.exit_code == 0
    results = json.loads(res2.output)
    assert results[0]["passed"] is True

    res3 = _invoke(root, "eval", "promote", "case1")
    assert res3.exit_code == 0
    promoted = json.loads((eval_dir / "case1.json").read_text(encoding="utf-8"))
    assert promoted["status"] == "active"


def test_tool_mode_show_set(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    res = _invoke(root, "tool-mode", "show")
    assert res.exit_code == 0
    assert res.output.strip() == "shadow"
    res2 = _invoke(root, "tool-mode", "set", "suggest")
    assert res2.exit_code == 0
    res3 = _invoke(root, "tool-mode", "show")
    assert res3.output.strip() == "suggest"


def test_read_returns_summary_and_related(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    f = tmp_path / "x.py"
    f.write_text("\n".join(f"line {i}" for i in range(200)), encoding="utf-8")
    res = _invoke(root, "read", str(f), "--max-lines", "50")
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert payload["lines_total"] == 200
    assert "summary" in payload


def test_savings_reports_counters(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    _seed_ledger(root)
    res = _invoke(root, "savings", "--json")
    assert res.exit_code == 0
    payload = json.loads(res.output)
    assert "rescue_events" in payload
    assert payload["rescue_events"] >= 1


def test_benchmark_dry_run(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    res = _invoke(
        root,
        "benchmark",
        "--prompt",
        "Fix Shopify publish",
        "--prompt",
        "Refactor catalog",
        "--rounds",
        "2",
        "--json",
    )
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert "tasks" in payload
    assert len(payload["tasks"]) == 2
    assert payload["aggregate"]["total_calls"] >= 4
    # Round 2 should cost <= round 1 because lessons reduce input tokens.
    for task in payload["tasks"]:
        assert task["final_cost_usd"] <= task["baseline_cost_usd"]
