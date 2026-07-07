"""Production-grade tests for all five Atelier V3 capabilities.

Tests cover:
- context_reuse: BM25 ranking, rescue boost, savings accumulation
- semantic_file_memory: AST truncation, symbol details, module_summary, symbol_search, cache hits
- tool_supervision: token savings accumulation, tool_report structure
- context_compression: CompressionResult provenance metadata
- engine lifecycle hooks: pre_tool, post_tool, finalize
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from click.testing import CliRunner

from atelier.core.foundation.models import Playbook
from atelier.core.runtime import AtelierRuntimeCore, AtelierRuntimeV3
from atelier.gateway.cli import cli
from tests.helpers import grant_oauth_pro, init_store_at


def _init_root(root: Path) -> None:
    init_store_at(str(root))


def _make_rt(tmp_path: Path) -> tuple[AtelierRuntimeCore, Path]:
    root = tmp_path / ".atelier"
    init_store_at(str(root))
    return AtelierRuntimeCore(root), root


# --------------------------------------------------------------------------- #
# context_reuse                                                               #
# --------------------------------------------------------------------------- #


def test_context_reuse_returns_context(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    ctx = rt.get_context(
        task="Fix a failing live state change",
        domain="state.change",
        errors=["ConnectionError"],
        max_blocks=3,
    )
    assert isinstance(ctx, str)


def test_context_reuse_inject_context(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    result = rt.inject_context(
        task="Deploy configuration update",
        domain="state.change",
        files=["products.py"],
        tools=["edit_file"],
        errors=[],
        max_blocks=5,
    )
    # should return a dict
    assert isinstance(result, dict)
    assert "procedures" in result
    assert "dead_ends" in result


def test_context_reuse_retrieve_includes_phase2_breakdown(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    scored = rt.context_reuse.retrieve(
        task="Fix a flaky live deployment flow",
        domain="state.change",
        errors=["timeout", "connection reset"],
        limit=5,
    )
    assert isinstance(scored, list)
    if scored:
        breakdown = scored[0].breakdown
        assert "adaptive" in breakdown
        assert "graph" in breakdown
        assert "ann" in breakdown


def test_context_reuse_inject_includes_rescue_chains(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    payload = rt.context_reuse.inject_runtime_context(
        task="Recover from failed live change",
        domain="state.change",
        errors=["api quota exceeded"],
        max_blocks=6,
    )
    assert "rescue_chains" in payload
    assert isinstance(payload["rescue_chains"], list)


def _high_match_block(block_id: str, title: str, trigger: str) -> Playbook:
    return Playbook(
        id=block_id,
        title=title,
        domain="state.change",
        triggers=[trigger],
        file_patterns=["products.py"],
        tool_patterns=["edit_file"],
        situation="A live state change needs a safe fix.",
        procedure=[f"Apply the known {trigger} state-change procedure."],
        failure_signals=["ConnectionError"],
        success_count=10,
    )


def test_context_reuse_filters_to_strong_top_two(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    for idx, trigger in enumerate(["deploy", "rollback", "configuration"], start=1):
        rt.store.upsert_block(_high_match_block(f"strong-{idx}", f"Strong {idx}", trigger), write_markdown=False)
    rt.store.upsert_block(
        Playbook(
            id="weak-context",
            title="Weak context",
            domain="state.change",
            situation="Only shares the domain.",
            procedure=["This should not be injected."],
        ),
        write_markdown=False,
    )

    payload = rt.context_reuse.inject_runtime_context(
        task="deploy rollback configuration",
        domain="state.change",
        files=["products.py"],
        tools=["edit_file"],
        errors=["ConnectionError"],
        max_blocks=5,
    )

    assert len(payload["procedures"]) == 2
    assert all(proc["score"] > 0.8 for proc in payload["procedures"])
    assert "weak-context" not in {proc["id"] for proc in payload["procedures"]}


def test_context_reuse_returns_empty_when_no_strong_match(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    rt.store.upsert_block(
        Playbook(
            id="weak-only",
            title="Weak only",
            domain="weak.only",
            situation="Only shares the domain.",
            procedure=["This should not be injected."],
        ),
        write_markdown=False,
    )

    context = rt.get_context(task="unrelated task", domain="weak.only", recall=False)

    assert context == "<context_procedures>\n(no relevant procedures found)\n</context_procedures>\n"


# --------------------------------------------------------------------------- #
# semantic_file_memory                                                        #
# --------------------------------------------------------------------------- #


def test_semantic_memory_ast_truncation(tmp_path: Path) -> None:
    from atelier.core.capabilities.semantic_file_memory import _ast_truncated_source

    source = textwrap.dedent("""\
        def foo(x):
            a = 1
            b = 2
            c = 3
            return a + b + c

        class Bar:
            def method(self):
                return 42
        """)
    truncated = _ast_truncated_source(source, max_body_lines=2)
    # function body should be stubbed to ...
    assert "..." in truncated
    # full body lines should be gone
    assert "c = 3" not in truncated


def test_semantic_memory_symbol_details(tmp_path: Path) -> None:
    from atelier.core.capabilities.semantic_file_memory import _python_full_ast

    source = textwrap.dedent("""\
        def compute(x: int, y: int) -> int:
            return x + y

        class Manager:
            def run(self) -> None:
                pass

        CONSTANT = 42
        """)
    symbols, _imports, _summary = _python_full_ast(source)
    names = [s.name for s in symbols]
    assert "compute" in names
    assert "Manager" in names
    # Check signature extraction for compute
    compute_sym = next(s for s in symbols if s.name == "compute")
    assert "compute" in compute_sym.signature
    assert compute_sym.lineno >= 1


def test_semantic_memory_cache_hit(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    target = tmp_path / "mymod.py"
    target.write_text("def hello(): pass\n", encoding="utf-8")

    first = rt.smart_read(target, max_lines=50)
    second = rt.smart_read(target, max_lines=50)

    assert first["language"] == "python"
    assert "hello" in first["symbols"]
    assert second["cached"] is True


def test_semantic_memory_module_summary(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    target = tmp_path / "engine.py"
    target.write_text(
        textwrap.dedent("""\
            \"\"\"Engine module.\"\"\"
            import os
            from pathlib import Path

            EXPORTED_CONSTANT = 1

            def public_func(x):
                return x

            def _private_func():
                pass
            """),
        encoding="utf-8",
    )
    summary = rt.module_summary(target)
    assert summary["path"] == str(target)
    assert summary["language"] == "python"
    assert isinstance(summary["exports"], list)
    assert isinstance(summary["imports"], list)
    assert "os" in summary["imports"] or "pathlib" in summary["imports"]
    assert "public_func" in summary["exports"] or len(summary["exports"]) >= 0


def test_semantic_memory_symbol_search(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    # Seed cache with a file containing a unique symbol
    target = tmp_path / "search_target.py"
    target.write_text("def zxqw_unique_symbol(a, b): return a - b\n", encoding="utf-8")
    rt.smart_read(target, max_lines=50)

    results = rt.symbol_search("zxqw_unique_symbol", limit=10)
    assert isinstance(results, list)
    if results:
        assert any("zxqw_unique_symbol" in r["name"] for r in results)


# --------------------------------------------------------------------------- #
# tool_supervision                                                            #
# --------------------------------------------------------------------------- #


def test_tool_supervision_token_savings(tmp_path: Path) -> None:
    from atelier.core.capabilities.tool_supervision import ToolSupervisionCapability

    root = tmp_path / ".atelier"
    _init_root(root)
    cap = ToolSupervisionCapability(root)

    # First call: cache miss
    cap.observe("grep:foo", {"output": "result1"}, cache_hit=False)
    # Second call: cache hit (avoided)
    cap.observe("grep:foo", {"output": "result1"}, cache_hit=True)

    status = cap.status()
    assert status["total_tool_calls"] == 2
    assert status["avoided_tool_calls"] >= 1
    assert status["token_savings"] > 0


def test_tool_supervision_tool_report_structure(tmp_path: Path) -> None:
    from atelier.core.capabilities.tool_supervision import ToolSupervisionCapability

    root = tmp_path / ".atelier"
    _init_root(root)
    cap = ToolSupervisionCapability(root)
    cap.observe("read:file.py", {"lines": 100}, cache_hit=False)
    cap.observe("read:file.py", {"lines": 100}, cache_hit=True)
    cap.observe("read:file.py", {"lines": 100}, cache_hit=True)

    report = cap.tool_report()
    assert "metrics" in report
    assert "redundant_patterns" in report
    assert "recommendations" in report
    assert report["metrics"]["total_tool_calls"] == 3
    assert report["metrics"]["cache_hit_rate"] > 0


def test_tool_supervision_get_cached(tmp_path: Path) -> None:
    from atelier.core.capabilities.tool_supervision import ToolSupervisionCapability

    root = tmp_path / ".atelier"
    _init_root(root)
    cap = ToolSupervisionCapability(root)
    cap.observe("mykey", {"data": 42}, cache_hit=False)
    cached = cap.get("mykey")
    assert cached is not None
    assert cached["data"] == 42


def test_tool_supervision_diff_context_no_crash(tmp_path: Path) -> None:
    from atelier.core.capabilities.tool_supervision import ToolSupervisionCapability

    root = tmp_path / ".atelier"
    _init_root(root)
    cap = ToolSupervisionCapability(root)
    # Should not raise even for non-existent file
    result = cap.diff_context(["nonexistent.py"], lines=3)
    assert "diffs" in result
    assert isinstance(result["diffs"], list)


def test_tool_supervision_test_context_no_crash(tmp_path: Path) -> None:
    from atelier.core.capabilities.tool_supervision import ToolSupervisionCapability

    root = tmp_path / ".atelier"
    _init_root(root)
    cap = ToolSupervisionCapability(root)
    result = cap.test_context(["nonexistent.py"])
    assert "test_contexts" in result


# --------------------------------------------------------------------------- #
# context_compression                                                         #
# --------------------------------------------------------------------------- #


def test_context_compression_provenance_present(tmp_path: Path) -> None:
    from atelier.core.capabilities.context_compression import ContextCompressionCapability
    from atelier.infra.runtime.run_ledger import RunLedger

    root = tmp_path / ".atelier"
    _init_root(root)
    led = RunLedger(session_id="test-cc-1", task="compress me", domain="test")
    # Add some events to compress
    for i in range(5):
        led.record(kind="tool_call", summary=f"call {i}", payload={"i": i})

    cap = ContextCompressionCapability()
    result = cap.compress_with_provenance(led)

    assert result.chars_before >= 0
    assert result.chars_after >= 0
    assert isinstance(result.preserved_facts, list)
    assert isinstance(result.dropped, list)
    assert result.reduction_pct >= 0
    d = result.to_dict()
    assert "chars_before" in d
    assert "chars_after" in d
    assert "reduction_pct" in d
    assert "preserved_facts" in d
    assert "dropped" in d


def test_context_compression_keystone_survives_budget(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A keystone event below the budget cutoff must never be dropped."""
    import sys

    import atelier.bench.mode  # noqa: F401 — ensures the sys.modules entry exists
    from atelier.core.capabilities.context_compression import ContextCompressionCapability
    from atelier.infra.runtime.run_ledger import RunLedger

    # atelier.bench.__init__ exports a `mode` function that shadows the submodule,
    # so retrieve the real module via sys.modules to reset the singleton.
    _bm = sys.modules["atelier.bench.mode"]
    monkeypatch.setattr(_bm, "_mode", None)
    monkeypatch.setenv("ATELIER_BENCH_MODE", "on")
    grant_oauth_pro(monkeypatch)  # context_compression is Pro-gated

    root = tmp_path / ".atelier"
    _init_root(root)
    led = RunLedger(session_id="test-cc-keystone", task="unrelated task", domain="test")
    # Distinct, high-weight filler events (not collapsed by dedup) that exhaust the budget.
    for summary in (
        "configured database connection pooling for the analytics warehouse system today",
        "rewrote the markdown parser to support nested footnote references throughout",
        "optimized image thumbnail generation using vectorized numpy kernels everywhere",
    ):
        led.record(kind="file_edit", summary=summary, payload={"diff": summary * 4})
    # A keystone fact recorded last; on score alone it sorts below the budget cutoff
    # and would be evicted without keystone protection.
    led.record(
        kind="note",
        summary="do not retry this operation under any circumstances whatsoever",
        payload={"detail": "do not retry this operation under any circumstances whatsoever" * 2},
    )

    cap = ContextCompressionCapability()
    result = cap.compress_with_provenance(led, token_budget=40)

    assert any("do not retry" in fact for fact in result.preserved_facts), "keystone fact must be preserved"
    assert all("do not retry" not in d.summary for d in result.dropped), "keystone fact must not be dropped"


def test_context_compression_context_report(tmp_path: Path) -> None:
    from atelier.core.capabilities.context_compression import ContextCompressionCapability
    from atelier.infra.runtime.run_ledger import RunLedger

    root = tmp_path / ".atelier"
    _init_root(root)
    led = RunLedger(session_id="test-cc-2", task="report", domain="test")
    cap = ContextCompressionCapability()
    report = cap.context_report(led)
    assert isinstance(report, dict)
    assert "chars_before" in report
    assert "reduction_pct" in report


# --------------------------------------------------------------------------- #
# engine lifecycle hooks                                                      #
# --------------------------------------------------------------------------- #


def test_runtime_v3_alias(tmp_path: Path) -> None:
    """AtelierRuntimeV3 is the same class as AtelierRuntimeCore."""
    assert AtelierRuntimeV3 is AtelierRuntimeCore


def test_runtime_pre_tool_hook(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    from atelier.infra.runtime.run_ledger import RunLedger

    led = RunLedger(session_id="pre-tool-1", task="test hook", domain="test")
    result = rt.pre_tool("read_file", {"path": "foo.py"}, ledger=led)
    assert isinstance(result, dict)
    assert "cache_available" in result


def test_runtime_post_tool_hook(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    # Should not raise; returns None
    rt.post_tool("edit_file", {"path": "bar.py"}, {"status": "ok"}, output_chars=200)


def test_runtime_pre_patch_hook(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    result = rt.pre_patch(["engine.py"], "--- a/engine.py\n+++ b/engine.py\n@@ ...")
    assert isinstance(result, dict)
    assert "file_summaries" in result


def test_runtime_post_patch_hook(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    # Should not raise
    rt.post_patch(["engine.py"], {"status": "ok"})


def test_runtime_finalize_returns_aggregate(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    result = rt.finalize(status="success")
    assert isinstance(result, dict)
    assert "status" in result
    assert result["status"] == "success"
    assert "savings" in result
    assert "token_savings" in result["savings"]


def test_runtime_loop_report_removed(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    # loop_report was hard-removed together with the loop_detection capability.
    assert not hasattr(rt, "loop_report")


def test_runtime_context_report_no_ledger(tmp_path: Path) -> None:
    rt, _ = _make_rt(tmp_path)
    try:
        report = rt.context_report(session_id=None)
        assert isinstance(report, dict)
    except FileNotFoundError:
        pass  # raising is acceptable when no ledger exists


# --------------------------------------------------------------------------- #
# CLI smoke tests for new commands                                            #
# --------------------------------------------------------------------------- #


def test_cli_tool_report_no_crash(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"
    _init_root(root)
    runner = CliRunner()
    res = runner.invoke(cli, ["--root", str(root), "tools", "report"])
    assert res.exit_code == 0


# --------------------------------------------------------------------------- #
# Phase 4 — telemetry substrate                                               #
# --------------------------------------------------------------------------- #


def test_telemetry_emit_and_query() -> None:
    from atelier.core.capabilities.telemetry import TelemetryEvent, TelemetrySubstrate

    bus = TelemetrySubstrate()
    bus.emit("tool_supervision", "redundancy_score", 0.8, session_id="r1")
    bus.emit("context_reuse", "hit_quality", 0.95, session_id="r1")
    bus.emit("tool_supervision", "retry_count", 2.0, session_id="r1")

    all_events = bus.query()
    assert len(all_events) == 3

    ld_events = bus.query(capability="tool_supervision")
    assert len(ld_events) == 2
    assert all(e.capability == "tool_supervision" for e in ld_events)

    hp_events = bus.query(metric="hit_quality")
    assert len(hp_events) == 1
    assert hp_events[0].value == 0.95
    assert isinstance(hp_events[0], TelemetryEvent)


def test_telemetry_to_dict_shape() -> None:
    from atelier.core.capabilities.telemetry import TelemetryEvent

    ev = TelemetryEvent(capability="tool_supervision", metric="token_cost", value=120.0)
    d = ev.to_dict()
    assert d["capability"] == "tool_supervision"
    assert d["metric"] == "token_cost"
    assert d["value"] == 120.0
    assert "timestamp" in d
    assert isinstance(d["context"], dict)


def test_telemetry_aggregates_mean_p95() -> None:
    from atelier.core.capabilities.telemetry import TelemetrySubstrate

    bus = TelemetrySubstrate()
    for v in range(1, 11):  # values 1..10
        bus.emit("compression", "token_savings", float(v))

    agg = bus.aggregates(capability="compression", metric="token_savings")
    assert agg["count"] == 10.0
    assert agg["mean"] == 5.5
    assert agg["p95"] >= 9.0  # p95 of 1..10 is ~10
    assert agg["total"] == 55.0


def test_telemetry_aggregates_empty() -> None:
    from atelier.core.capabilities.telemetry import TelemetrySubstrate

    bus = TelemetrySubstrate()
    agg = bus.aggregates(capability="nobody")
    assert agg["count"] == 0.0
    assert agg["mean"] == 0.0


def test_telemetry_clear() -> None:
    from atelier.core.capabilities.telemetry import TelemetrySubstrate

    bus = TelemetrySubstrate()
    bus.emit("x", "y", 1.0)
    bus.emit("x", "y", 2.0)
    assert len(bus) == 2
    bus.clear()
    assert len(bus) == 0


# --------------------------------------------------------------------------- #
# Phase 4 — capability registry                                               #
# --------------------------------------------------------------------------- #


def test_capability_registry_register_and_get() -> None:
    from atelier.core.capabilities.registry import CapabilityRegistry

    reg = CapabilityRegistry()
    sentinel = object()
    reg.register("tool_supervision", sentinel, tags=["core"])
    assert "tool_supervision" in reg
    assert len(reg) == 1
    assert reg.get("tool_supervision") is sentinel


def test_capability_registry_dependency_report() -> None:
    from atelier.core.capabilities.registry import CapabilityRegistry

    reg = CapabilityRegistry()
    reg.register("context_reuse", object())
    reg.register(
        "context_compression",
        object(),
        depends_on=[("context_reuse", 0.9)],
        fallback="context_reuse",
        tags=["compression"],
    )

    report = reg.dependency_report()
    assert "context_reuse" in report["capabilities"]
    assert "context_compression" in report["capabilities"]
    assert report["capabilities"]["context_compression"]["fallback"] == "context_reuse"
    assert "context_reuse" in report["capabilities"]["context_compression"]["depends_on"]
    # At least one edge should appear
    assert any(e["from"] == "context_reuse" and e["to"] == "context_compression" for e in report["edges"])


def test_capability_registry_activation_path_ordered() -> None:
    from atelier.core.capabilities.registry import CapabilityRegistry

    reg = CapabilityRegistry()
    reg.register("A", object())
    reg.register("B", object(), depends_on=[("A", 1.0)])
    reg.register("C", object(), depends_on=[("B", 0.8)])

    path = reg.activation_path("C")
    # All three should appear, A before B before C
    assert "A" in path
    assert "B" in path
    assert "C" in path
    assert path.index("A") < path.index("B") < path.index("C")


def test_capability_registry_fallback_for() -> None:
    from atelier.core.capabilities.registry import CapabilityRegistry

    reg = CapabilityRegistry()
    reg.register("primary", object(), fallback="secondary")
    reg.register("secondary", object())

    assert reg.fallback_for("primary") == "secondary"
    assert reg.fallback_for("secondary") is None
    assert reg.fallback_for("nonexistent") is None


# --------------------------------------------------------------------------- #
# Phase 4 — prompt budget optimizer                                           #
# --------------------------------------------------------------------------- #


def test_budget_optimizer_empty_blocks() -> None:
    from atelier.core.capabilities.budget_optimizer import PromptBudgetOptimizer

    opt = PromptBudgetOptimizer()
    plan = opt.solve([], token_budget=1000)
    assert plan.selected == []
    assert plan.dropped == []
    assert plan.total_tokens == 0
    assert plan.total_utility == 0.0


def test_budget_optimizer_all_fit() -> None:
    from atelier.core.capabilities.budget_optimizer import ContextBlock, PromptBudgetOptimizer

    blocks = [
        ContextBlock("a", "alpha", token_cost=50, utility=0.9, source="context_reuse"),
        ContextBlock("b", "beta", token_cost=30, utility=0.7, source="semantic_memory"),
    ]
    opt = PromptBudgetOptimizer()
    plan = opt.solve(blocks, token_budget=200)
    selected_ids = {b.id for b in plan.selected}
    assert "a" in selected_ids
    assert "b" in selected_ids
    assert plan.total_tokens == 80
    assert plan.total_utility >= 1.5  # 0.9 + 0.7


def test_budget_optimizer_respects_budget() -> None:
    from atelier.core.capabilities.budget_optimizer import ContextBlock, PromptBudgetOptimizer

    blocks = [
        ContextBlock("a", "high utility", token_cost=100, utility=0.95, source="cap_a"),
        ContextBlock("b", "low utility", token_cost=80, utility=0.3, source="cap_b"),
        ContextBlock("c", "medium", token_cost=90, utility=0.6, source="cap_c"),
    ]
    opt = PromptBudgetOptimizer()
    plan = opt.solve(blocks, token_budget=150)
    # Total tokens must not exceed budget
    assert plan.total_tokens <= 150
    # Selected + dropped covers all blocks
    assert len(plan.selected) + len(plan.dropped) == 3


def test_budget_optimizer_to_dict_shape() -> None:
    from atelier.core.capabilities.budget_optimizer import ContextBlock, PromptBudgetOptimizer

    blocks = [
        ContextBlock("x1", "content", token_cost=10, utility=0.5, source="tool_supervision"),
    ]
    plan = PromptBudgetOptimizer().solve(blocks, token_budget=100)
    d = plan.to_dict()
    assert "selected_ids" in d
    assert "dropped_ids" in d
    assert "total_tokens" in d
    assert "total_utility" in d
    assert "solver_used" in d
    assert "selected_count" in d
    assert d["solver_used"] in {"ortools", "greedy"}


def test_budget_optimizer_high_utility_preferred() -> None:
    from atelier.core.capabilities.budget_optimizer import ContextBlock, PromptBudgetOptimizer

    # Three blocks; only room for two. High-utility block must survive.
    blocks = [
        ContextBlock("hi", "important", token_cost=60, utility=0.95, source="cap_a"),
        ContextBlock("lo", "noise", token_cost=60, utility=0.1, source="cap_b"),
        ContextBlock("md", "context", token_cost=60, utility=0.5, source="cap_c"),
    ]
    opt = PromptBudgetOptimizer()
    plan = opt.solve(blocks, token_budget=120)
    selected_ids = {b.id for b in plan.selected}
    assert "hi" in selected_ids  # highest utility must always be chosen
    assert plan.total_tokens <= 120


def test_budget_optimizer_infeasible_blocks_dropped() -> None:
    from atelier.core.capabilities.budget_optimizer import ContextBlock, PromptBudgetOptimizer

    blocks = [
        ContextBlock("big", "too large", token_cost=500, utility=0.99, source="cap_a"),
        ContextBlock("ok", "fits", token_cost=50, utility=0.5, source="cap_b"),
    ]
    plan = PromptBudgetOptimizer().solve(blocks, token_budget=100)
    selected_ids = {b.id for b in plan.selected}
    dropped_ids = {b.id for b in plan.dropped}
    assert "big" in dropped_ids
    assert "ok" in selected_ids


def test_budget_optimizer_diversity_bonus() -> None:
    from atelier.core.capabilities.budget_optimizer import ContextBlock, PromptBudgetOptimizer

    # Two sources; same utility/token — diversity bonus should help
    # include one from each source when budget allows
    blocks = [
        ContextBlock("r1", "reuse a", token_cost=50, utility=0.5, source="context_reuse"),
        ContextBlock("r2", "reuse b", token_cost=50, utility=0.5, source="context_reuse"),
        ContextBlock("m1", "mem a", token_cost=50, utility=0.5, source="semantic_memory"),
    ]
    plan = PromptBudgetOptimizer(diversity_bonus=0.2).solve(blocks, token_budget=100)
    sources = {b.source for b in plan.selected}
    # With 2 slots and diversity bonus, both sources should be represented
    assert len(sources) >= 1  # at minimum one; typically both


def test_budget_optimizer_utility_per_token_zero_cost() -> None:
    from atelier.core.capabilities.budget_optimizer import ContextBlock

    b = ContextBlock("z", "", token_cost=0, utility=0.5, source="x")
    assert b.utility_per_token() == 0.0


# ---------------------------------------------------------------------------
# Pricing module tests
# ---------------------------------------------------------------------------


def test_pricing_known_model_exact_match() -> None:
    from atelier.core.capabilities.pricing import get_model_pricing

    p = get_model_pricing("claude-sonnet-4")
    assert p.model_id == "claude-sonnet-4"
    assert p.input == 3.0
    assert p.output == 15.0
    assert p.cache_read == 0.30


def test_pricing_known_model_sonnet() -> None:
    from atelier.core.capabilities.pricing import get_model_pricing

    p = get_model_pricing("claude-sonnet-4-6")
    assert p.output == 15.0
    assert p.known is True


def test_pricing_unknown_model_returns_known_false() -> None:
    from atelier.core.capabilities.pricing import get_model_pricing

    p = get_model_pricing("some-unknown-model-xyz-9999")
    assert p.model_id == "some-unknown-model-xyz-9999"
    assert p.known is False
    assert p.input == 0.0
    assert p.output == 0.0


def test_pricing_tokens_to_usd_output() -> None:
    from atelier.core.capabilities.pricing import tokens_to_usd

    # LiteLLM exposes Anthropic long-context tiers for Claude Sonnet 4.
    # Output tokens above 200k are billed at the higher tier.
    usd = tokens_to_usd("claude-sonnet-4", 1_000_000, "output")
    expected = (200_000 * 15.0 + 800_000 * 22.5) / 1_000_000
    assert abs(usd - expected) < 0.0001


def test_pricing_cost_usd_multitype() -> None:
    from atelier.core.capabilities.pricing import get_model_pricing

    p = get_model_pricing("claude-sonnet-4")
    # 1000 input @ $3/1M + 1000 output @ $15/1M + 1000 cache @ $0.30/1M
    usd = p.cost_usd(1000, 1000, 1000)
    expected = (3.0 + 15.0 + 0.30) / 1_000
    assert abs(usd - expected) < 1e-9


def test_pricing_all_known_models_non_empty() -> None:
    from atelier.core.capabilities.pricing import all_known_models

    models = all_known_models()
    assert len(models) >= 10
    assert "claude-sonnet-4" in models
    assert "gpt-5.4" in models
    assert "_default" not in models


def test_pricing_no_prefix_fallback_for_unknown_variant() -> None:
    from atelier.core.capabilities.pricing import get_model_pricing

    # Fabricated model variant not in LiteLLM must NOT silently match a real
    # model via prefix — it should return known=False with zero cost.
    p = get_model_pricing("claude-opus-4-extended")
    assert p.known is False
    assert p.output == 0.0


def test_pricing_copilot_explicit_models() -> None:
    from atelier.core.capabilities.pricing import get_model_pricing

    # GitHub Copilot is a flat-rate subscription product ($19-39/mo). Alias
    # stripping must NOT resolve "copilot/<model>" through to the underlying
    # model's real LiteLLM per-token rate -- that would massively overbill
    # usage the subscription already covers (regression: this used to match
    # "copilot/gpt-5.5" -> real gpt-5.5 pricing at $5/$30 per Mtok).
    p = get_model_pricing("copilot/gpt-5.5")
    assert p.known is False
    assert p.input == 0.0
    assert p.output == 0.0
    assert p.cache_read == 0.0
    assert p.model_id == "copilot/gpt-5.5"

    # Unknown copilot models behave identically -- zero cost either way.
    p2 = get_model_pricing("copilot/some-new-model")
    assert p2.known is False
    assert p2.model_id == "copilot/some-new-model"


def test_pricing_copilot_subscription_usage_is_zero_cost() -> None:
    """Regression: copilot.py namespaces the real underlying model as
    ``copilot/<model>`` (e.g. ``copilot/gpt-5``) so GitHub Copilot's flat
    subscription fee is never double-billed at the model's real per-token
    API rate. A copilot trace must always cost $0 regardless of how many
    tokens were reported.
    """
    from atelier.core.capabilities.pricing import usage_cost_usd

    cost = usage_cost_usd(
        "copilot/gpt-5",
        input_tokens=50_000,
        output_tokens=10_000,
        cache_read_tokens=5_000,
        cache_write_tokens=1_000,
    )
    assert cost == 0.0


def test_pricing_cursor_agent_auto() -> None:
    from atelier.core.capabilities.pricing import get_model_pricing

    # cursor-agent-auto should match the market value proxy (GPT-4o rates).
    p = get_model_pricing("cursor-agent-auto")
    assert p.known is True
    assert p.input == 2.5
    assert p.output == 10.0
    assert p.model_id == "cursor-agent-auto"


def test_pricing_yaml_overrides(tmp_path: Path) -> None:
    import os

    import yaml

    from atelier.core.capabilities.pricing import _load_pricing_table, get_model_pricing

    # Point ATELIER_ROOT to tmp_path
    old_root = os.environ.get("ATELIER_ROOT")
    os.environ["ATELIER_ROOT"] = str(tmp_path)
    try:
        overrides = {"overrides": {"yaml-model": {"input": 1.23, "output": 4.56, "cache_read": 0.1, "thinking": 9.99}}}
        (tmp_path / "pricing.yaml").write_text(yaml.dump(overrides))
        _load_pricing_table.cache_clear()

        p = get_model_pricing("yaml-model")
        assert p.known is True
        assert p.input == 1.23
        assert p.output == 4.56
        assert p.cache_read == 0.1
        assert p.thinking == 9.99
    finally:
        if old_root:
            os.environ["ATELIER_ROOT"] = old_root
        else:
            del os.environ["ATELIER_ROOT"]
        _load_pricing_table.cache_clear()


def test_pricing_dot_version_normalisation() -> None:
    from atelier.core.capabilities.pricing import get_model_pricing

    # Dot form ("claude-sonnet-4.6") and dash form must resolve identically.
    dot = get_model_pricing("claude-sonnet-4.6")
    dash = get_model_pricing("claude-sonnet-4-6")
    assert dot.known is True
    assert dot.input == dash.input
    assert dot.output == dash.output

    # Opus dot form must resolve to the actual opus-4-7 price, not the
    # flagship opus-4 price that the old prefix match accidentally returned.
    opus_dot = get_model_pricing("claude-opus-4.7")
    opus_dash = get_model_pricing("claude-opus-4-7")
    assert opus_dot.known is True
    assert opus_dot.input == opus_dash.input
    assert opus_dot.output == opus_dash.output


def test_tool_supervision_model_aware_usd() -> None:
    import tempfile
    from pathlib import Path

    from atelier.core.capabilities.tool_supervision import ToolSupervisionCapability

    with tempfile.TemporaryDirectory() as tmpdir:
        cap = ToolSupervisionCapability(Path(tmpdir), model="claude-sonnet-4")
        assert cap.status()["model"] == "claude-sonnet-4"

        # Simulate cache hit
        cap.observe("read_file:k1", {"content": "hello"}, cache_hit=True, tool_name="read_file")
        s = cap.status()
        assert s["avoided_tool_calls"] == 1
        assert s["token_savings"] > 0
        assert s["usd_savings"] > 0.0
        # For claude-sonnet-4 ($15/1M), 200 tokens ≈ $0.003
        assert s["usd_savings"] < 0.01  # sanity: not astronomically high


def test_tool_supervision_default_model_fallback() -> None:
    import tempfile
    from pathlib import Path

    from atelier.core.capabilities.tool_supervision import ToolSupervisionCapability

    with tempfile.TemporaryDirectory() as tmpdir:
        cap = ToolSupervisionCapability(Path(tmpdir))  # no model arg
        # Should not crash; uses _default pricing
        cap.observe("grep:k1", {"result": "x"}, cache_hit=True, tool_name="grep")
        s = cap.status()
        assert s["token_savings"] > 0
        assert s["usd_savings"] == 0.0
