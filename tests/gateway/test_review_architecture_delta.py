from __future__ import annotations

from pathlib import Path
from typing import Any

from lemoncrow.pro.capabilities.review.architecture_delta import (
    architecture_explain_context,
    build_architecture_delta,
    run_architecture_explanation,
    unavailable_architecture_delta,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import (
    ModelCallBudget,
    ModelCallUsage,
    StructuredExecution,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id


def _edge(source: str, target: str, *, weight: int = 1, kind: str = "references") -> dict[str, Any]:
    return {
        "source": source,
        "target": target,
        "kind": kind,
        "weight": weight,
        "symbols": [{"name": f"{source}->{target}", "count": weight}],
        "evidence": [{"kind": "reference", "path": source, "line": 3, "symbol": "ref", "uncertainty": ""}],
    }


def _context_node(path: str, *, exactness: str = "snapshot", status: str = "changed") -> dict[str, Any]:
    return {
        "id": _file_id(path),
        "label": Path(path).name,
        "kind": "file",
        "component": str(Path(path).parent),
        "path": path,
        "line": 0,
        "end_line": 0,
        "status": status,
        "exactness": exactness,
        "target_ids": [],
        "important_public_contract": False,
        "boundaries": [],
        "selection": {},
    }


def _file_id(path: str) -> str:
    import hashlib

    return f"file:{hashlib.sha256(path.encode()).hexdigest()[:20]}"


def _build(
    *,
    base_nodes: list[str],
    snapshot_nodes: list[str],
    base_edges: list[dict[str, Any]],
    snapshot_edges: list[dict[str, Any]],
    files: list[dict[str, Any]],
    context_graph: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return build_architecture_delta(
        review_id="review-1",
        revision_id="revision-1",
        packet={"files": files},
        base_architecture={"nodes": base_nodes, "edges": base_edges},
        snapshot_architecture={"nodes": snapshot_nodes, "edges": snapshot_edges},
        context_graph=context_graph or {"nodes": [], "edges": []},
        context=context
        or {
            "source": "current_index",
            "snapshot_head_matches_checkout": True,
            "generation_matches_snapshot_head": True,
            "readiness": {"graph": {"state": "ready"}},
        },
        base_identity="base-aaa",
        snapshot_identity="snapshot-bbb",
    )


def _edge_by(delta: dict[str, Any], source: str, target: str) -> dict[str, Any]:
    return next(
        edge
        for edge in delta["graph"]["edges"]
        if edge.get("source_path") == source and edge.get("target_path") == target
    )


def test_identical_semantic_input_has_stable_architecture_delta() -> None:
    kwargs = dict(
        base_nodes=["src/a.py", "src/b.py"],
        snapshot_nodes=["src/a.py", "src/b.py"],
        base_edges=[_edge("src/a.py", "src/b.py")],
        snapshot_edges=[_edge("src/a.py", "src/b.py")],
        files=[{"path": "src/a.py", "status": "modified"}],
    )
    first = _build(**kwargs)
    second = _build(**kwargs)
    assert first == second
    assert first["digest"] == second["digest"]
    assert not first["summary"]["material"]


def test_base_to_snapshot_node_and_edge_changes_are_exact() -> None:
    delta = _build(
        base_nodes=["src/a.py", "src/b.py", "src/old.py"],
        snapshot_nodes=["src/a.py", "src/b.py", "src/new.py"],
        base_edges=[_edge("src/a.py", "src/b.py", weight=1), _edge("src/old.py", "src/a.py")],
        snapshot_edges=[_edge("src/a.py", "src/b.py", weight=2), _edge("src/new.py", "src/a.py")],
        files=[
            {"path": "src/a.py", "status": "modified"},
            {"path": "src/new.py", "status": "added"},
            {"path": "src/old.py", "status": "deleted"},
        ],
    )

    nodes = {node["path"]: node for node in delta["graph"]["nodes"]}
    assert nodes["src/new.py"]["delta_status"] == "added"
    assert nodes["src/new.py"]["exactness"] == "snapshot"
    assert nodes["src/old.py"]["delta_status"] == "removed"
    assert nodes["src/old.py"]["selection"]["side"] == "old"
    assert nodes["src/a.py"]["delta_status"] == "changed"

    modified = _edge_by(delta, "src/a.py", "src/b.py")
    added = _edge_by(delta, "src/new.py", "src/a.py")
    removed = _edge_by(delta, "src/old.py", "src/a.py")
    assert (modified["delta_status"], modified["base_weight"], modified["snapshot_weight"]) == ("changed", 1, 2)
    assert (added["delta_status"], added["base_weight"], added["snapshot_weight"]) == ("added", 0, 1)
    assert (removed["delta_status"], removed["base_weight"], removed["snapshot_weight"]) == ("removed", 1, 0)
    for edge in (modified, added, removed):
        assert edge["exactness"] == "snapshot"
        assert edge["architecture_provenance"]["source"] == "snapshot_comparison"
        assert edge["architecture_provenance"]["comparison"] == "base_vs_snapshot"
    assert removed["selection"]["side"] == "old"
    assert modified["selection"]["path"] == "src/a.py"
    assert added["selection"]["path"] == "src/new.py"
    assert removed["selection"]["path"] == "src/old.py"


def test_current_index_only_relationship_never_becomes_snapshot_delta() -> None:
    a = _context_node("src/a.py")
    live = _context_node("src/live.py", exactness="current_index", status="context")
    context_graph = {
        "nodes": [a, live],
        "edges": [
            {
                "id": "live-edge",
                "source": live["id"],
                "target": a["id"],
                "kind": "calls",
                "exactness": "current_index",
                "weight": 1,
                "uncertain": False,
                "selection": {},
            }
        ],
    }
    delta = _build(
        base_nodes=["src/a.py"],
        snapshot_nodes=["src/a.py"],
        base_edges=[],
        snapshot_edges=[],
        files=[{"path": "src/a.py", "status": "modified"}],
        context_graph=context_graph,
    )
    edge = _edge_by(delta, "src/live.py", "src/a.py")
    assert edge["exactness"] == "current_index"
    assert edge["delta_status"] == "context_only"
    assert edge["architecture_provenance"]["comparison"] == "none"
    assert edge["selection"]["architecture_source"] == "current_index"
    assert edge["selection"]["path"] == ""
    explain = architecture_explain_context(delta)
    assert explain["changed_edges"] == []
    assert explain["excluded_context_only_edges"] == 1


def test_architecture_explanation_excludes_edges_touching_denied_paths_and_rebuilds_summary() -> None:
    delta = _build(
        base_nodes=["src/public.py", "src/secret.py", "src/shared.py"],
        snapshot_nodes=["src/public.py", "src/secret.py", "src/shared.py"],
        base_edges=[_edge("src/public.py", "src/shared.py"), _edge("src/secret.py", "src/shared.py")],
        snapshot_edges=[
            _edge("src/public.py", "src/shared.py", weight=2),
            _edge("src/secret.py", "src/shared.py", weight=2),
        ],
        files=[
            {"path": "src/public.py", "status": "modified"},
            {"path": "src/secret.py", "status": "modified"},
        ],
    )
    context = architecture_explain_context(delta, allowed_paths={"src/public.py", "src/shared.py"})
    assert len(context["changed_edges"]) == 1
    assert context["changed_edges"][0]["source"] == "src/public.py"
    rendered = str(context)
    assert "src/secret.py" not in rendered
    assert context["summary"]["changed_edges"] == 1


def test_snapshot_fact_without_base_comparison_stays_unknown() -> None:
    a = _context_node("src/a.py")
    b = _context_node("src/b.py", status="impacted")
    context_graph = {
        "nodes": [a, b],
        "edges": [
            {
                "id": "impact",
                "source": a["id"],
                "target": b["id"],
                "kind": "impacts",
                "exactness": "snapshot",
                "weight": 1,
                "uncertain": False,
                "selection": {},
            }
        ],
    }
    delta = _build(
        base_nodes=["src/a.py"],
        snapshot_nodes=["src/a.py"],
        base_edges=[],
        snapshot_edges=[],
        files=[{"path": "src/a.py", "status": "modified"}],
        context_graph=context_graph,
    )
    edge = _edge_by(delta, "src/a.py", "src/b.py")
    assert edge["exactness"] == "snapshot"
    assert edge["delta_status"] == "unknown"
    assert edge["architecture_provenance"]["source"] == "snapshot_fact"
    assert edge["architecture_provenance"]["comparison"] == "none"
    assert edge["selection"]["path"] == "src/a.py"


def test_missing_or_stale_graph_state_degrades_visibly() -> None:
    delta = _build(
        base_nodes=["src/a.py"],
        snapshot_nodes=["src/a.py"],
        base_edges=[],
        snapshot_edges=[],
        files=[{"path": "src/a.py", "status": "modified"}],
        context={
            "source": "current_index",
            "snapshot_head_matches_checkout": False,
            "generation_matches_snapshot_head": False,
            "readiness": {"graph": {"state": "building"}},
        },
    )
    assert delta["status"] == "degraded"
    assert set(delta["summary"]["degraded"]) == {
        "current_index_building",
        "current_index_checkout_differs_from_snapshot",
        "current_index_generation_differs_from_snapshot",
    }

    unavailable = unavailable_architecture_delta(
        review_id="review-1",
        revision_id="revision-1",
        context_graph={"nodes": [], "edges": []},
        context={"source": "unavailable"},
        reason="Snapshot comparison missing",
    )
    assert unavailable["status"] == "unavailable"
    assert unavailable["summary"]["exactness"] == "unknown"
    assert "snapshot_architecture_comparison_unavailable" in unavailable["summary"]["degraded"]


def test_trivial_relationship_change_does_not_force_sequence_flow() -> None:
    delta = _build(
        base_nodes=["src/a.py", "src/b.py"],
        snapshot_nodes=["src/a.py", "src/b.py"],
        base_edges=[],
        snapshot_edges=[_edge("src/a.py", "src/b.py")],
        files=[{"path": "src/a.py", "status": "modified"}],
    )
    assert delta["summary"]["material"]
    assert delta["summary"]["changed_edges"] == 1
    assert not delta["flow"]["applicable"]
    assert delta["flow"]["steps"] == []


def test_reference_architecture_ties_files_by_what_they_export_not_by_shared_names(tmp_path: Path) -> None:
    # The 2026-11-01 replay: a new test file's locals and its vitest import tied every other test file to it.
    from lemoncrow.pro.capabilities.review.architecture_delta import _reference_architecture

    files = {
        "src/token.ts": (
            "const internal = 1;\n"
            "export async function exchange(code: string): Promise<string> {\n"
            "  const actual = code.trim();\n"
            "  return actual + internal;\n"
            "}\n"
            "export function isOk(value: { ok: boolean }): boolean { return value.ok; }\n"
        ),
        "src/service.ts": (
            "import { exchange } from './token';\n"
            "export async function signIn(code: string, actual: string) {\n"
            "  return [await exchange(code), actual, internal];\n"
            "}\n"
        ),
        "src/token.test.ts": (
            "import { afterEach, expect, it } from 'vitest';\n"
            "import { exchange } from './token';\n"
            "const calls: string[] = [];\n"
            "function json(data: object) { return data; }\n"
            "afterEach(() => calls.splice(0));\n"
            "it('exchanges', async () => { const body = json({}); expect(await exchange('x')).toBe(body); });\n"
        ),
        "src/other.test.ts": (
            "import { afterEach, it } from 'vitest';\n"
            "function json(data: object) { return data; }\n"
            "afterEach(() => undefined);\n"
            "it('reads', () => { const spy = { mock: { calls: [] } }; json(spy.mock.calls); });\n"
        ),
    }
    for path, text in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")

    edges = {(edge["source"], edge["target"]): edge for edge in _reference_architecture(tmp_path)["edges"]}

    assert set(edges) == {("src/service.ts", "src/token.ts"), ("src/token.test.ts", "src/token.ts")}
    assert [row["name"] for row in edges[("src/service.ts", "src/token.ts")]["symbols"]] == ["exchange"]
    assert (
        edges[("src/token.test.ts", "src/token.ts")]["weight"]
        == edges[("src/token.test.ts", "src/token.ts")]["symbols"][0]["count"]
    )


def _store(tmp_path: Path) -> tuple[ReviewStore, ReviewSession, ReviewRevision]:
    store = ReviewStore(tmp_path)
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root="/immutable/test",
            range_mode="working_tree",
            title="architecture delta",
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="tree-arch",
            source_fingerprint="source-arch",
            packet_schema_version=1,
        ),
        [],
    )
    return store, session, revision


def _explain_delta() -> dict[str, Any]:
    return _build(
        base_nodes=["src/a.py", "src/b.py"],
        snapshot_nodes=["src/a.py", "src/b.py"],
        base_edges=[],
        snapshot_edges=[_edge("src/a.py", "src/b.py")],
        files=[{"path": "src/a.py", "status": "modified"}],
    )


def _explain_execution(edge_id: str) -> StructuredExecution:
    return StructuredExecution(
        result={
            "summary": "A now references B.",
            "why_it_matters": ["The dependency shape changed."],
            "grounded_edge_ids": [edge_id, "invented-edge"],
            "flow": [
                {"edge_id": edge_id, "explanation": "Grounded explanation."},
                {"edge_id": "invented-edge", "explanation": "Must be dropped."},
            ],
            "limitations": ["A reference edge does not prove runtime ordering."],
        },
        driver="openai",
        model="test-model",
        usage=ModelCallUsage(calls=1, input_tokens=8, output_tokens=4, request_id="req-arch"),
    )


def test_architecture_explain_uses_shared_cache_and_filters_ungrounded_edges(tmp_path: Path) -> None:
    store, session, revision = _store(tmp_path)
    delta = _explain_delta()
    edge_id = next(edge["id"] for edge in delta["graph"]["edges"] if edge["delta_status"] == "added")
    calls = 0

    def execute() -> StructuredExecution:
        nonlocal calls
        calls += 1
        return _explain_execution(edge_id)

    kwargs = dict(
        store=store,
        review_id=session.id,
        revision_id=revision.id,
        snapshot_identity="source-arch",
        delta=delta,
        driver="openai",
        model="test-model",
        budget=ModelCallBudget(max_calls=1, max_review_calls=5),
        policy_context={"effective_review": {"behavior": {"effort": {"effective_value": "auto"}}}},
        executor=execute,
    )
    first = run_architecture_explanation(**kwargs)
    second = run_architecture_explanation(**kwargs)

    assert first.status == "ready" and first.provider_calls_this_request == 1
    assert second.status == "ready" and second.cache_hit and second.provider_calls_this_request == 0
    assert calls == 1
    assert first.result["grounded_edge_ids"] == [edge_id]
    assert first.result["flow"] == [{"edge_id": edge_id, "explanation": "Grounded explanation."}]
    assert first.result["provenance"] == "inferred_explanation"

    changed_policy = run_architecture_explanation(
        **{
            **kwargs,
            "policy_context": {"effective_review": {"behavior": {"effort": {"effective_value": "deep"}}}},
        }
    )
    assert changed_policy.status == "ready" and changed_policy.provider_calls_this_request == 1
    assert not changed_policy.cache_hit
    assert calls == 2
    assert store.list_intelligence_runs(session.id, revision_id=revision.id, capability="architecture_explain")


def test_architecture_explain_budget_exhaustion_and_provider_failure_do_not_promote_facts(tmp_path: Path) -> None:
    store, session, revision = _store(tmp_path)
    delta = _explain_delta()
    invoked = False

    def should_not_run() -> StructuredExecution:
        nonlocal invoked
        invoked = True
        raise AssertionError("provider must not be invoked")

    exhausted = run_architecture_explanation(
        store=store,
        review_id=session.id,
        revision_id=revision.id,
        snapshot_identity="source-arch",
        delta=delta,
        driver="openai",
        model="budget-model",
        budget=ModelCallBudget(max_calls=0, max_review_calls=5),
        executor=should_not_run,
    )
    assert exhausted.status == "failed"
    assert "budget exhausted" in exhausted.error.lower()
    assert not invoked

    def unavailable() -> StructuredExecution:
        raise RuntimeError("provider unavailable")

    failed = run_architecture_explanation(
        store=store,
        review_id=session.id,
        revision_id=revision.id,
        snapshot_identity="source-arch",
        delta=delta,
        driver="openai",
        model="unavailable-model",
        budget=ModelCallBudget(max_calls=1, max_review_calls=5),
        executor=unavailable,
    )
    assert failed.status == "failed"
    assert "provider unavailable" in failed.error
    # The deterministic graph remains the authority regardless of explanation failure.
    assert all(
        edge["architecture_provenance"]["source"] == "snapshot_comparison"
        for edge in delta["graph"]["edges"]
        if edge["delta_status"] in {"added", "removed", "changed"}
    )
