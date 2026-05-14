"""CLI tests for V2 commands: ledger, compress, env, failure, eval, read, savings."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from atelier.core.foundation.models import Trace
from atelier.core.foundation.store import ContextStore
from atelier.gateway.adapters.cli import cli
from atelier.infra.runtime.run_ledger import RunLedger


def _invoke(root: Path, *args: str, input: str | None = None) -> Result:
    runner = CliRunner()
    return runner.invoke(cli, ["--root", str(root), *args], input=input)


def _seed_ledger(root: Path, session_id: str = "run1") -> Path:
    led = RunLedger(session_id=session_id, agent="codex", task="t", domain="d", root=root)
    led.record_command("pytest", ok=False, error_signature="sig1")
    led.record_command("pytest", ok=False, error_signature="sig1")
    led.record_alert("repeated_command_failure", "high", "pytest x2")
    path: Path = led.persist()
    return path


def _seed_optimizer_traces(root: Path) -> None:
    store = ContextStore(root)
    store.init()
    created_at = datetime.now(UTC)
    for trace in (
        Trace(
            id="peer-low",
            agent="codex",
            host="codex",
            domain="optimizer-test",
            task="small run",
            status="success",
            input_tokens=80_000,
            output_tokens=4_000,
            model="gpt-5.5-pro",
            files_touched=["a.py"],
            created_at=created_at,
        ),
        Trace(
            id="outlier",
            agent="codex",
            host="codex",
            domain="optimizer-test",
            task="large run",
            status="success",
            input_tokens=1_000_000,
            output_tokens=10_000,
            model="gpt-5.5-pro",
            created_at=created_at,
        ),
    ):
        store.record_trace(trace)


def test_ledger_show_and_summarize(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    _seed_ledger(root)
    res = _invoke(root, "ledger", "show", "--json")
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["session_id"] == "run1"

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


def test_failure_list_accept_reject(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    _seed_ledger(root)
    _seed_ledger(root, session_id="run2")

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
    assert "optimization" in payload


def test_optimize_reports_trace_recommendations(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _seed_optimizer_traces(root)

    res = _invoke(root, "optimize", "--host", "codex", "--json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    recommendation_ids = {item["id"] for item in payload["recommendations"]}
    assert payload["host"] == "codex"
    assert "high-cost-session-outliers" in recommendation_ids
    assert "low-worth-expensive-sessions" in recommendation_ids


def test_optimize_accepts_new_registry_host_choice(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _seed_optimizer_traces(root)

    res = _invoke(root, "optimize", "--host", "qwen", "--json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["host"] == "qwen"
    assert payload["trace_count"] == 0


def test_external_status_cli_reports_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")

    monkeypatch.setattr(
        "atelier.gateway.integrations.external_analytics.external_status",
        lambda cwd=None: [
            {
                "tool": "tokscale",
                "display_name": "Tokscale",
                "available": True,
                "license": "MIT",
                "execution_mode": "installed_cli",
                "path": "/usr/bin/tokscale",
                "update_strategy": "pin",
                "install_hint": "install",
                "notes": ["reporting"],
                "recommended_integration": "pinned_sidecar_cli",
            }
        ],
    )

    res = _invoke(root, "external-status", "--json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["tools"][0]["tool"] == "tokscale"


def test_external_report_cli_returns_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")

    monkeypatch.setattr(
        "atelier.gateway.integrations.external_analytics.run_external_reports",
        lambda tool="all", period="week", cwd=None, include_optimize=False: {
            "tool": tool,
            "period": period,
            "reports": [
                {
                    "tool": "codeburn",
                    "ok": True,
                    "command_display": "codeburn report --format json -p week",
                    "payload": {"overview": {"cost": 12.5, "calls": 8, "sessions": 3}},
                }
            ],
        },
    )

    res = _invoke(root, "external-report", "--tool", "codeburn", "--json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["reports"][0]["tool"] == "codeburn"


def test_external_report_cli_persists_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")

    monkeypatch.setattr(
        "atelier.gateway.integrations.external_analytics.run_external_reports",
        lambda tool="all", period="today", cwd=None, include_optimize=False: {
            "generated_at": "2026-05-14T09:00:00+00:00",
            "tool": tool,
            "period": period,
            "reports": [
                {
                    "tool": "codeburn",
                    "period": period,
                    "ok": True,
                    "returncode": 0,
                    "command_display": "codeburn report --format json -p today",
                    "payload": {"overview": {"cost": 12.5, "calls": 48, "sessions": 3}},
                }
            ],
        },
    )

    res = _invoke(root, "external-report", "--tool", "all", "--period", "today", "--persist", "--json")

    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["persisted"][0]["tool"] == "codeburn"

    runs = ContextStore(root).list_external_analytics_runs(tool="codeburn", period="today", limit=10)
    assert len(runs) == 1
    assert runs[0]["payload"]["overview"]["calls"] == 48


def test_external_report_cli_streams_tool_progress(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    calls: list[str] = []

    def fake_run_external_report(tool: str, *, period: str = "week", cwd: Path | None = None) -> dict[str, object]:
        _ = cwd
        calls.append(tool)
        return {
            "tool": tool,
            "period": period,
            "ok": True,
            "returncode": 0,
            "command_display": f"{tool} report -p {period}",
            "payload": {"overview": {"cost": 1.0, "calls": len(calls), "sessions": 1}},
        }

    monkeypatch.setattr(
        "atelier.gateway.integrations.external_analytics.run_external_report",
        fake_run_external_report,
    )

    res = _invoke(root, "external-report", "--tool", "all", "--period", "today")

    assert res.exit_code == 0, res.output
    assert calls == ["tokscale", "codeburn", "codeburn:optimize"]
    assert "[external-report] running tokscale period=today..." in res.output
    assert "[external-report] done tokscale status=ok" in res.output
    assert "[external-report] running codeburn period=today..." in res.output
    assert "[external-report] done codeburn:optimize status=ok" in res.output


def test_benchmark_dry_run(tmp_path: Path) -> None:
    root = tmp_path / "a"
    _invoke(root, "init")
    res = _invoke(
        root,
        "benchmark",
        "run",
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
