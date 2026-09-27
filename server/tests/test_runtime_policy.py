from __future__ import annotations

import json
from types import SimpleNamespace

from lemoncrow_server_core.index import runtime_policy
from lemoncrow_server_core.index.query import QueryAnswer, QueryHit


def _ambiguous_answer() -> QueryAnswer:
    return QueryAnswer(
        kind="code_search",
        hits=(
            QueryHit(path="src/a.py", score=1.0, detail={"content_indexed": True}),
            QueryHit(path="src/b.py", score=0.92, detail={"content_indexed": True}),
            QueryHit(path="src/c.py", score=0.70, detail={"content_indexed": True}),
        ),
        at_view_revision=3,
        searched_paths=20,
    )


def test_native_evidence_projects_server_hits_without_source_content() -> None:
    state = runtime_policy.native_evidence_state(
        _ambiguous_answer(),
        query="authorization middleware",
        source_paths=("src/a.py", "src/b.py", "src/c.py"),
    )

    assert state.status == "ambiguous"
    assert state.reason_codes == ("low_rank_margin", "source_available")
    assert state.source_file_count == 3
    assert state.source_section_count == 3
    assert state.relationship_count == 0
    assert state.top_paths[:2] == ("src/a.py", "src/b.py")


def test_native_shadow_proposes_but_never_executes(monkeypatch) -> None:
    monkeypatch.setenv("LEMONCROW_EVIDENCE_RESOLUTION_MODE", "shadow")

    class MustNotRun:
        def __init__(self, *args, **kwargs) -> None:
            raise AssertionError("shadow mode executed a relation expansion")

    monkeypatch.setattr(runtime_policy, "ServerGraphAnalytics", MustNotRun)
    answer = _ambiguous_answer()
    resolved, diagnostic = runtime_policy.apply_native_search_policy(
        SimpleNamespace(),
        org_id="org",
        view_id="view",
        query="authorization middleware",
        answer=answer,
        source_paths=("src/a.py", "src/b.py", "src/c.py"),
        limit=3,
    )

    assert resolved is answer
    assert diagnostic["mode"] == "shadow"
    assert diagnostic["proposed_action"] == "EXPAND_RELATIONS"
    assert diagnostic["actual_action"] == "STOP"
    assert diagnostic["rounds"] == 0


def test_native_candidate_runs_one_bounded_relation_round(monkeypatch) -> None:
    monkeypatch.setenv("LEMONCROW_EVIDENCE_RESOLUTION_MODE", "experiment")
    monkeypatch.setenv("LEMONCROW_EVIDENCE_RESOLUTION_EXPERIMENT", "benchmark")

    class FakeGraph:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def blast_radius(self, path: str, *, max_transitive_depth: int = 3):
            assert path == "src/a.py"
            assert max_transitive_depth == 2
            return {
                "direct_importers": ["src/neighbor.py"],
                "transitive_importers": [],
                "affected_tests": [],
            }

    monkeypatch.setattr(runtime_policy, "ServerGraphAnalytics", FakeGraph)
    monkeypatch.setattr(
        runtime_policy,
        "_relation_hit",
        lambda *args, **kwargs: QueryHit(
            path="src/neighbor.py",
            score=0.3,
            detail={"content_indexed": True, "runtime_expansion": "relation"},
        ),
    )

    answer = _ambiguous_answer()
    resolved, diagnostic = runtime_policy.apply_native_search_policy(
        SimpleNamespace(content_size_cap=1024),
        org_id="org-secret",
        view_id="view-secret",
        query="authorization middleware secret-query",
        answer=answer,
        source_paths=("src/a.py", "src/b.py", "src/c.py"),
        limit=3,
    )

    assert [hit.path for hit in resolved.hits] == ["src/a.py", "src/b.py", "src/neighbor.py"]
    assert diagnostic["mode"] == "experiment"
    assert diagnostic["actual_action"] == "EXPAND_RELATIONS"
    assert diagnostic["rounds"] == 1
    assert diagnostic["candidate_count_before"] == 3
    assert diagnostic["candidate_count_after"] == 3
    encoded = json.dumps(diagnostic)
    assert "secret-query" not in encoded
    assert "src/a.py" not in encoded
    assert "org-secret" not in encoded
    assert "view-secret" not in encoded


def test_merge_relation_candidates_never_displaces_top_two() -> None:
    answer = _ambiguous_answer()
    merged = runtime_policy.merge_relation_candidates(
        answer,
        (
            QueryHit(path="src/d.py", score=0.3),
            QueryHit(path="src/e.py", score=0.2),
        ),
        limit=3,
    )

    assert [hit.path for hit in merged.hits[:2]] == ["src/a.py", "src/b.py"]
    assert len(merged.hits) == 3
    assert merged.hits[-1].path == "src/d.py"
