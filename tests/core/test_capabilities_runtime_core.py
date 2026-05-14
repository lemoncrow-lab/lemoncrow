from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from atelier.core.foundation.models import Trace
from atelier.core.runtime import AtelierRuntimeCore
from atelier.gateway.adapters.cli import cli
from atelier.infra.runtime.run_ledger import RunLedger


def _init_root(root: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["--root", str(root), "init"])
    assert res.exit_code == 0, res.output


def test_context_reuse_returns_ranked_procedures(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _init_root(root)
    rt = AtelierRuntimeCore(root)

    context = rt.get_context(
        task="Apply a live state change safely",
        domain="state.change",
        errors=["wrong target updated"],
        max_blocks=3,
    )
    assert isinstance(context, str)
    assert context.strip()


def test_semantic_memory_read_and_search(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _init_root(root)
    rt = AtelierRuntimeCore(root)

    target = tmp_path / "sample.py"
    target.write_text(
        "def alpha():\n    return 1\n\nclass Beta:\n    pass\n",
        encoding="utf-8",
    )

    first = rt.smart_read(target, max_lines=20)
    second = rt.smart_read(target, max_lines=20)

    assert first["language"] == "python"
    assert "alpha" in first["symbols"]
    assert "token_metrics" in first
    assert "saved_tokens" in first["token_metrics"]
    assert second["cached"] is True

    matches = rt.smart_search("alpha", limit=5)
    assert "semantic" in matches
    assert "glob" in matches
    assert "snippets" in matches
    assert "ranked" in matches


def test_loop_detection_and_context_compression(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _init_root(root)
    rt = AtelierRuntimeCore(root)

    ledger = RunLedger(root=root, agent="test", task="t", domain="d")
    ledger.record_command("pytest", ok=False, error_signature="same")
    ledger.record_command("pytest", ok=False, error_signature="same")
    ledger.record_alert("repeated_command_failure", "high", "pytest repeated")
    ledger.persist(root)

    summary = rt.summarize_memory(session_id=ledger.session_id)
    assert summary["session_id"] == ledger.session_id
    assert "loop_alerts" in summary


def test_failure_analysis_clusters_and_matches(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _init_root(root)
    rt = AtelierRuntimeCore(root)

    rt.store.record_trace(
        Trace(
            id=Trace.make_id("Fix auth", "codex"),
            agent="codex",
            domain="coding",
            task="Fix auth",
            status="failed",
            errors_seen=["AssertionError: expected 200 got 500"],
            commands_run=["pytest -q"],
        )
    )
    rt.store.record_trace(
        Trace(
            id=Trace.make_id("Fix auth retry", "codex"),
            agent="codex",
            domain="coding",
            task="Fix auth retry",
            status="failed",
            errors_seen=["AssertionError: expected 201 got 500"],
            commands_run=["pytest -q"],
        )
    )

    report = rt.analyze_failures(domain="coding", lookback=50, min_cluster_size=1)
    assert report["total_failed_traces"] >= 2
    assert report["cluster_count"] >= 1

    matched = rt.analyze_failure_for_error(
        task="Fix auth endpoint",
        error="AssertionError: expected 204 got 500",
        domain="coding",
        lookback=50,
    )
    assert "matched" in matched
    assert "suggested_fixes" in matched.get("incident", {}) or "suggested_fixes" in matched


def test_tool_supervision_and_smart_edit(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _init_root(root)
    rt = AtelierRuntimeCore(root)

    rt.smart_search("shopify", limit=3)
    rt.smart_search("shopify", limit=3)

    status = rt.capability_status()["tool_supervision"]
    assert status["total_tool_calls"] >= 2
    assert status["avoided_tool_calls"] >= 1

    file_path = tmp_path / "edit.txt"
    file_path.write_text("hello world", encoding="utf-8")
    result = rt.smart_edit(
        [
            {"path": str(file_path), "find": "world", "replace": "atelier"},
        ]
    )
    assert result["applied"] == 1
    assert file_path.read_text(encoding="utf-8") == "hello atelier"


def test_smart_edit_fuzzy_and_bash_intercept(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _init_root(root)
    rt = AtelierRuntimeCore(root)

    file_path = tmp_path / "fuzzy.txt"
    file_path.write_text("publish_product_by_gid\n", encoding="utf-8")

    # trailing whitespace variant matches through whitespace-flex regex path
    result = rt.smart_edit(
        [
            {
                "path": str(file_path),
                "find": "publish_product_by_gid ",
                "replace": "publish_product_by_id",
            }
        ]
    )
    assert result["applied"] == 1
    assert "publish_product_by_id" in file_path.read_text(encoding="utf-8")

    intercept = rt.bash_intercept("rg publish")
    assert intercept["intercepted"] is True
    assert intercept["suggestion"]["tool"] == "search"


def test_sql_inspect_and_runtime_benchmark_metrics(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _init_root(root)
    rt = AtelierRuntimeCore(root)

    sql = "SELECT * FROM catalog.products JOIN sales.orders ON products.id = orders.product_id"
    inspected = rt.sql_inspect(sql=sql)
    assert "catalog.products" in inspected["tables"]
    assert inspected["query_profile"]["join_count"] == 1

    metrics = rt.benchmark_runtime_metrics()
    for key in [
        "total_tool_calls",
        "avoided_tool_calls",
        "token_savings",
        "retries_prevented",
        "loops_prevented",
        "successful_rescues",
        "validation_catches",
        "context_reduction",
        "task_success_rate",
    ]:
        assert key in metrics

    out = tmp_path / "runtime_metrics.json"
    rt.export_benchmark_runtime(out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert "total_tool_calls" in payload
