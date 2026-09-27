from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import lemoncrow.pro.capabilities.review.review_map as review_map
from lemoncrow.pro.capabilities.review.review_map import build_review_map


def _phase3_paths(root: Path) -> list[str]:
    output = subprocess.check_output(
        ["git", "-C", str(root), "diff-tree", "--no-commit-id", "--name-only", "-r", "eed5d1c9d"],
        text=True,
    )
    return [line for line in output.splitlines() if line]


def _revision() -> SimpleNamespace:
    return SimpleNamespace(
        id="rrv-map",
        revision_number=4,
        tree_fingerprint="tree-exact",
        source_fingerprint="source-exact",
        base_sha="a" * 40,
        head_sha="b" * 40,
        degraded=(),
    )


def _target(target_id: str, path: str, start: int, end: int) -> SimpleNamespace:
    return SimpleNamespace(
        target_id=target_id,
        path=path,
        start_line=start,
        end_line=end,
        symbol="",
        state="unreviewed",
    )


def test_real_phase3_change_projects_to_bounded_file_relationship_graph(monkeypatch: pytest.MonkeyPatch) -> None:
    root = Path(__file__).resolve().parents[2]
    paths = _phase3_paths(root)
    assert len(paths) >= 10
    assert "frontend/src/review/ReviewOverviewSheet.tsx" in paths
    assert "src/lemoncrow/pro/capabilities/review/chapters.py" in paths

    packet = {
        "files": [{"path": path, "status": "modified"} for path in paths],
        "symbols": [
            {
                "symbol_name": "build_chapter_lenses",
                "qualified_name": "lemoncrow.pro.capabilities.review.chapters.build_chapter_lenses",
                "kind": "function",
                "file_path": "src/lemoncrow/pro/capabilities/review/chapters.py",
                "start_line": 540,
                "end_line": 570,
                "change": "modified",
                "caller_count": 3,
                "usage_count": 5,
                "centrality_rank": 22,
                "centrality_percentile": 0.86,
            },
            {
                "symbol_name": "ReviewOverviewSheet",
                "qualified_name": "ReviewOverviewSheet",
                "kind": "function",
                "file_path": "frontend/src/review/ReviewOverviewSheet.tsx",
                "start_line": 17,
                "end_line": 280,
                "change": "modified",
                "caller_count": 1,
                "usage_count": 1,
                "centrality_rank": None,
                "centrality_percentile": None,
            },
        ],
        "impact": [
            {
                "kind": "callsite",
                "path": "frontend/src/review/ReviewReader.tsx:L3393",
                "old": "",
                "new": None,
                "snippet": "<ReviewOverviewSheet",
                "in_patch": True,
                "source_path": "frontend/src/review/ReviewOverviewSheet.tsx",
                "uncertainty": "",
            },
            {
                "kind": "callsite",
                "path": "tests/gateway/test_review_attention_ux.py:L100",
                "old": "",
                "new": None,
                "snippet": "build_chapter_lenses(",
                "in_patch": False,
                "source_path": "src/lemoncrow/pro/capabilities/review/chapters.py",
                "uncertainty": "",
            },
            {
                "kind": "callsite",
                "path": "server/src/lemoncrow_server_core/local_review.py:L50",
                "old": "",
                "new": None,
                "snippet": "build_chapter_lenses(",
                "in_patch": False,
                "source_path": "src/lemoncrow/pro/capabilities/review/chapters.py",
                "uncertainty": "",
            },
        ],
        "order": [
            {
                "path": "src/lemoncrow/pro/capabilities/review/chapters.py",
                "rank": 1,
                "reasons": ["public contract changed", "3 known callers"],
            }
        ],
        "provenance": {"host": "codex", "model": "gpt-5.6", "certainty": "exact"},
        "evidence": [],
        "degraded": [],
    }
    targets = (
        _target("target-chapters", "src/lemoncrow/pro/capabilities/review/chapters.py", 540, 570),
        _target("target-overview", "frontend/src/review/ReviewOverviewSheet.tsx", 17, 280),
    )
    monkeypatch.setattr(
        review_map,
        "_current_context",
        lambda **_kwargs: {
            "source": "unavailable",
            "exact_for_snapshot": False,
            "snapshot_head_matches_checkout": False,
            "index": {},
            "note": "no current index",
            "matches": {},
            "neighborhoods": {},
        },
    )

    payload = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=targets,
        evidence=({"name": "Tests", "status": "PASS", "detail": "", "source": "test"},),
        context_repo_root=root,
    )

    graph = payload["graph"]
    assert graph["level"] == "architecture"
    assert graph["nodes"]
    assert all(node["kind"] == "file" for node in graph["nodes"])
    by_path = {node["path"]: node for node in graph["nodes"]}
    assert "frontend/src/review/ReviewOverviewSheet.tsx" in by_path
    assert "src/lemoncrow/pro/capabilities/review/chapters.py" in by_path
    backend = by_path["src/lemoncrow/pro/capabilities/review/chapters.py"]
    assert backend["component"] == "src/lemoncrow/pro"
    assert backend["status"] == "changed"
    assert backend["important_public_contract"] is True
    assert "target-chapters" in backend["target_ids"]
    assert backend["selection"]["revision_id"] == "rrv-map"
    impacted = by_path["server/src/lemoncrow_server_core/local_review.py"]
    assert impacted["component"] == "server"
    assert impacted["status"] == "impacted"
    assert impacted["selection"]["path"] == "server/src/lemoncrow_server_core/local_review.py"
    assert impacted["selection"]["line"] == 50
    assert payload["snapshot"]["verification"]["pass"] == 1
    assert payload["context"]["source"] == "unavailable"
    assert any(edge["kind"] == "impacts" and edge["exactness"] == "snapshot" for edge in graph["edges"])


def test_symbol_drilldown_separates_snapshot_fact_from_current_index_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbol = {
        "symbol_name": "authorize",
        "qualified_name": "app.auth.authorize",
        "kind": "function",
        "file_path": "src/auth/security.py",
        "start_line": 30,
        "end_line": 55,
        "change": "modified",
        "caller_count": 7,
        "usage_count": 9,
        "centrality_rank": 4,
        "centrality_percentile": 0.97,
    }
    packet = {
        "files": [{"path": "src/auth/security.py", "status": "modified"}],
        "symbols": [symbol],
        "impact": [
            {
                "kind": "callsite",
                "path": "src/api/routes.py:L88",
                "old": "",
                "new": None,
                "snippet": "authorize(request)",
                "in_patch": False,
                "source_path": "src/auth/security.py",
                "uncertainty": "",
            }
        ],
        "order": [{"path": "src/auth/security.py", "rank": 1, "reasons": ["public contract changed"]}],
        "provenance": {"host": "claude", "model": "sonnet", "certainty": "exact"},
    }
    snapshot_id = review_map._stable_id("symbol", "src/auth/security.py", "app.auth.authorize", 30, "modified")
    monkeypatch.setattr(
        review_map,
        "_current_context",
        lambda **_kwargs: {
            "source": "current_index",
            "exact_for_snapshot": False,
            "snapshot_head_matches_checkout": False,
            "index": {"symbols_indexed": 1000},
            "note": "current index",
            "matches": {
                snapshot_id: {
                    "id": "sym-current-authorize",
                    "label": "authorize",
                    "qualified_name": "app.auth.authorize",
                    "path": "src/auth/security.py",
                }
            },
            "neighborhoods": {
                snapshot_id: {
                    "nodes": [
                        {
                            "id": "sym-current-authorize",
                            "label": "authorize",
                            "qualified_name": "app.auth.authorize",
                            "path": "src/auth/security.py",
                            "kind": "function",
                            "line": 31,
                            "end_line": 56,
                        },
                        {
                            "id": "sym-current-handler",
                            "label": "handle_request",
                            "qualified_name": "app.api.handle_request",
                            "path": "src/api/routes.py",
                            "kind": "function",
                            "line": 70,
                            "end_line": 105,
                        },
                    ],
                    "edges": [
                        {
                            "id": "call-1",
                            "source": "sym-current-handler",
                            "target": "sym-current-authorize",
                            "kind": "calls",
                        }
                    ],
                    "truncated": False,
                }
            },
            "references": {
                snapshot_id: {
                    "references": [
                        {
                            "path": "src/jobs/worker.py",
                            "line": 44,
                            "end_line": 44,
                            "caller": "run_job",
                            "edge_kind": "reference",
                        }
                    ],
                    "truncated": False,
                }
            },
        },
    )

    payload = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=(_target("target-authorize", "src/auth/security.py", 30, 55),),
        evidence=(),
        context_repo_root=Path("."),
        level="symbol",
        selected_node_id=snapshot_id,
    )

    graph = payload["graph"]
    focus = next(node for node in graph["nodes"] if node["id"] == snapshot_id)
    caller = next(node for node in graph["nodes"] if node["id"] == "current:sym-current-handler")
    impacted = next(node for node in graph["nodes"] if node["status"] == "impacted")
    reference = next(node for node in graph["nodes"] if node["path"] == "src/jobs/worker.py")
    assert focus["exactness"] == "snapshot"
    assert focus["important_public_contract"] is True
    assert focus["boundaries"] == ["security"]
    assert focus["current_symbol_id"] == "sym-current-authorize"
    assert caller["exactness"] == "current_index"
    assert impacted["exactness"] == "snapshot"
    assert reference["exactness"] == "current_index"
    assert any(edge["kind"] == "calls" and edge["exactness"] == "current_index" for edge in graph["edges"])
    assert any(edge["kind"] == "references" and edge["exactness"] == "current_index" for edge in graph["edges"])
    assert any(edge["kind"] == "impacts" and edge["exactness"] == "snapshot" for edge in graph["edges"])
    assert payload["context"]["exact_for_snapshot"] is False


def test_architecture_marks_new_security_and_persistence_boundaries_from_snapshot_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = {
        "files": [
            {"path": "src/lemoncrow/security/auth.py", "status": "added"},
            {"path": "src/lemoncrow/infra/storage/migrations/v9.sql", "status": "added"},
        ],
        "symbols": [],
        "impact": [],
        "order": [],
    }
    monkeypatch.setattr(
        review_map,
        "_current_context",
        lambda **_kwargs: {
            "source": "unavailable",
            "exact_for_snapshot": False,
            "snapshot_head_matches_checkout": False,
            "index": {},
            "note": "no current index",
            "matches": {},
            "neighborhoods": {},
            "references": {},
        },
    )

    payload = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=(),
        evidence=(),
        context_repo_root=None,
    )
    by_path = {node["path"]: node for node in payload["graph"]["nodes"]}
    auth = by_path["src/lemoncrow/security/auth.py"]
    migration = by_path["src/lemoncrow/infra/storage/migrations/v9.sql"]
    assert auth["status"] == "new"
    assert auth["component"] == "src/lemoncrow/security"
    assert auth["boundaries"] == ["security"]
    assert migration["status"] == "new"
    assert migration["component"] == "src/lemoncrow/infra"
    assert migration["boundaries"] == ["persistence"]
    assert migration["boundary_basis"] == "path"


def test_architecture_current_call_context_is_bounded_and_never_promoted_to_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    symbol = {
        "symbol_name": "build",
        "qualified_name": "pkg.review.build",
        "kind": "function",
        "file_path": "src/lemoncrow/pro/review_map_fixture.py",
        "start_line": 10,
        "end_line": 20,
        "change": "modified",
        "caller_count": 1,
        "usage_count": 1,
        "centrality_rank": 1,
        "centrality_percentile": 0.99,
    }
    snapshot_id = review_map._stable_id(
        "symbol", "src/lemoncrow/pro/review_map_fixture.py", "pkg.review.build", 10, "modified"
    )
    packet = {
        "files": [{"path": "src/lemoncrow/pro/review_map_fixture.py", "status": "modified"}],
        "symbols": [symbol],
        "impact": [],
        "order": [],
    }
    monkeypatch.setattr(
        review_map,
        "_current_context",
        lambda **_kwargs: {
            "source": "current_index",
            "exact_for_snapshot": False,
            "snapshot_head_matches_checkout": False,
            "index": {"symbols_indexed": 2},
            "note": "current index",
            "matches": {
                snapshot_id: {
                    "id": "current-build",
                    "label": "build",
                    "qualified_name": "pkg.review.build",
                    "path": "src/lemoncrow/pro/review_map_fixture.py",
                }
            },
            "neighborhoods": {
                snapshot_id: {
                    "nodes": [
                        {
                            "id": "current-build",
                            "label": "build",
                            "qualified_name": "pkg.review.build",
                            "path": "src/lemoncrow/pro/review_map_fixture.py",
                        },
                        {
                            "id": "frontend-caller",
                            "label": "ReviewPanel",
                            "qualified_name": "ReviewPanel",
                            "path": "frontend/src/review/ReviewPanel.tsx",
                        },
                    ],
                    "edges": [
                        {
                            "source": "frontend-caller",
                            "target": "current-build",
                            "kind": "calls",
                            "weight": 3,
                        }
                    ],
                }
            },
            "references": {},
        },
    )

    payload = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=(),
        evidence=(),
        context_repo_root=Path("."),
    )
    call = next(edge for edge in payload["graph"]["edges"] if edge["kind"] == "calls")
    assert call["weight"] == 3
    assert call["exactness"] == "current_index"
    frontend = next(node for node in payload["graph"]["nodes"] if node["path"] == "frontend/src/review/ReviewPanel.tsx")
    assert frontend["component"] == "frontend/src"
    assert frontend["status"] == "context"
    assert frontend["exactness"] == "current_index"


def test_architecture_preserves_relationships_inside_the_same_component(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = {
        "files": [
            {"path": "frontend/src/review/ReviewMapSheet.tsx", "status": "modified"},
            {"path": "frontend/src/review/ReviewReader.tsx", "status": "modified"},
        ],
        "symbols": [],
        "impact": [
            {
                "kind": "callsite",
                "path": "frontend/src/review/ReviewReader.tsx:L120",
                "old": "",
                "new": None,
                "snippet": "<ReviewMapSheet",
                "in_patch": True,
                "source_path": "frontend/src/review/ReviewMapSheet.tsx",
                "uncertainty": "",
            }
        ],
        "order": [],
    }
    monkeypatch.setattr(
        review_map,
        "_current_context",
        lambda **_kwargs: {
            "source": "unavailable",
            "exact_for_snapshot": False,
            "snapshot_head_matches_checkout": False,
            "index": {},
            "note": "no current index",
            "matches": {},
            "neighborhoods": {},
            "references": {},
        },
    )

    payload = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=(),
        evidence=(),
        context_repo_root=None,
    )

    by_path = {node["path"]: node for node in payload["graph"]["nodes"]}
    source = by_path["frontend/src/review/ReviewMapSheet.tsx"]
    target = by_path["frontend/src/review/ReviewReader.tsx"]
    assert source["component"] == "frontend/src"
    assert target["component"] == "frontend/src"
    assert any(
        edge["kind"] == "impacts"
        and edge["source"] == source["id"]
        and edge["target"] == target["id"]
        and edge["exactness"] == "snapshot"
        for edge in payload["graph"]["edges"]
    )


def test_symbol_drilldown_refuses_a_node_not_in_the_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    packet = {
        "files": [{"path": "src/auth/security.py", "status": "modified"}],
        "symbols": [
            {
                "symbol_name": "authorize",
                "qualified_name": "app.auth.authorize",
                "kind": "function",
                "file_path": "src/auth/security.py",
                "start_line": 30,
                "end_line": 55,
                "change": "modified",
            }
        ],
        "impact": [],
        "order": [],
    }
    monkeypatch.setattr(
        review_map,
        "_current_context",
        lambda **_kwargs: {
            "source": "unavailable",
            "exact_for_snapshot": False,
            "snapshot_head_matches_checkout": False,
            "index": {},
            "note": "no current index",
            "matches": {},
            "neighborhoods": {},
            "references": {},
        },
    )

    payload = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=(),
        evidence=(),
        context_repo_root=None,
        level="symbol",
        selected_node_id="symbol:not-from-this-snapshot",
    )
    assert payload["graph"]["nodes"] == []
    assert "not part of this immutable Snapshot" in payload["graph"]["reason"]
