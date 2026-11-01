from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

import lemoncrow.pro.capabilities.review.review_map as review_map
from lemoncrow.pro.capabilities.review.review_map import build_review_map


def _phase3_paths(root: Path) -> list[str]:
    try:
        output = subprocess.check_output(
            ["git", "-C", str(root), "diff-tree", "--no-commit-id", "--name-only", "-r", "eed5d1c9d"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        pytest.skip("fixture commit eed5d1c9d exists only in the private development history")
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
    assert backend["component"] == "src/lemoncrow/pro/capabilities/review"
    assert backend["status"] == "changed"
    assert backend["important_public_contract"] is True
    assert "target-chapters" in backend["target_ids"]
    assert backend["selection"]["revision_id"] == "rrv-map"
    impacted = by_path["server/src/lemoncrow_server_core/local_review.py"]
    assert impacted["component"] == "server/src/lemoncrow_server_core"
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
    assert migration["component"] == "src/lemoncrow/infra/storage/migrations"
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
            "matches": {},
            "neighborhoods": {},
            "file_relationships": {
                "edges": [
                    {
                        "source_path": "server/src/review_panel.py",
                        "target_path": "src/lemoncrow/pro/review_map_fixture.py",
                        "kind": "calls",
                        "weight": 3,
                        "evidence": [
                            {
                                "kind": "calls",
                                "path": "server/src/review_panel.py",
                                "line": 42,
                                "symbol": "build",
                            }
                        ],
                    }
                ],
                "degraded": False,
                "truncated": False,
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
    caller = next(node for node in payload["graph"]["nodes"] if node["path"] == "server/src/review_panel.py")
    assert caller["component"] == "server/src"
    assert caller["status"] == "context"
    assert caller["exactness"] == "current_index"
    assert caller["lane"] == "used_by"


def test_overview_links_are_fetched_for_the_changed_files_it_shows_by_review_rank() -> None:
    # Links were fetched for the first 256 files in packet order while the
    # overview shows the top 300 by review rank, so top-ranked files that came
    # late in the packet were shown with no links at all.
    files = [{"path": f"src/f{index:03d}.py", "status": "modified"} for index in range(320)]
    order = [{"path": f"src/f{index:03d}.py", "rank": 320 - index, "reasons": []} for index in range(320)]
    asked: list[tuple[str, ...]] = []
    readiness = SimpleNamespace(
        available=True,
        backend="native",
        view_id="view",
        view_revision=1,
        generation_id="generation",
        base_revision="",
        manifest_root="",
        to_wire=lambda: {},
    )

    def file_relationships(paths: tuple[str, ...], *, limit: int) -> dict[str, object]:
        asked.append(paths)
        return {"edges": [], "degraded": False, "truncated": False}

    provider = SimpleNamespace(readiness=lambda: readiness, file_relationships=file_relationships)
    review_map._current_context(
        provider=provider,  # type: ignore[arg-type]
        packet={"files": files, "symbols": [], "impact": [], "order": order},
        snapshot_head_sha="",
    )

    (paths,) = asked
    assert paths == tuple(f"src/f{index:03d}.py" for index in range(319, 19, -1))


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
    assert source["component"] == "frontend/src/review"
    assert target["component"] == "frontend/src/review"
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


def _context_with_relationships(rows: list[dict[str, object]]) -> dict[str, object]:
    return {
        "source": "current_index",
        "exact_for_snapshot": False,
        "snapshot_head_matches_checkout": False,
        "index": {},
        "note": "current index",
        "matches": {},
        "neighborhoods": {},
        "file_relationships": {"edges": rows, "degraded": False, "truncated": False},
        "references": {},
    }


def _relation(source: str, target: str, kind: str, weight: int = 1, symbol: str = "") -> dict[str, object]:
    return {
        "source_path": source,
        "target_path": target,
        "kind": kind,
        "weight": weight,
        "evidence": [{"kind": kind, "path": source, "line": 3, "symbol": symbol}],
    }


def test_architecture_drops_name_collision_relationships_and_places_context_in_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    packet = {
        "files": [
            {"path": "src/app/core.py", "status": "modified", "category": "production"},
            {"path": "tests/test_core.py", "status": "modified", "category": "test"},
        ],
        "symbols": [],
        "impact": [],
        "order": [],
    }
    rows = [
        # Python bool()/round() resolved onto same-named TypeScript symbols.
        _relation("src/app/core.py", "frontend/src/review/reviewEnvironment.ts", "calls", 14, "bool"),
        _relation("frontend/src/theme.css", "src/app/core.py", "references", 9, "core"),
        # Shipping code never calls into a test module.
        _relation("src/app/core.py", "tests/helpers/test_fixtures.py", "calls", 2, "make"),
        # Real relationships survive.
        _relation("tests/test_core.py", "src/app/core.py", "calls", 5, "load"),
        _relation("src/app/api.py", "src/app/core.py", "imports", 1),
        _relation("src/app/core.py", "src/app/util.py", "calls", 3, "slugify"),
        # A page importing a stylesheet is path-resolved and trusted.
        _relation("frontend/src/App.tsx", "frontend/src/theme.css", "imports", 1),
    ]
    monkeypatch.setattr(review_map, "_current_context", lambda **_kwargs: _context_with_relationships(rows))

    payload = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=(),
        evidence=(),
        context_repo_root=None,
    )

    graph = payload["graph"]
    by_path = {node["path"]: node for node in graph["nodes"]}
    ids = {node["id"]: node["path"] for node in graph["nodes"]}
    edges = {(ids[edge["source"]], ids[edge["target"]], edge["kind"]) for edge in graph["edges"]}
    assert edges == {
        ("tests/test_core.py", "src/app/core.py", "calls"),
        ("src/app/api.py", "src/app/core.py", "imports"),
        ("src/app/core.py", "src/app/util.py", "calls"),
    }
    assert "frontend/src/review/reviewEnvironment.ts" not in by_path
    assert "tests/helpers/test_fixtures.py" not in by_path
    # Neither endpoint of the stylesheet import is changed: it does not explain this change.
    assert "frontend/src/App.tsx" not in by_path
    assert by_path["src/app/api.py"]["lane"] == "used_by"
    assert by_path["src/app/util.py"]["lane"] == "uses"
    assert by_path["src/app/core.py"]["lane"] == ""
    assert graph["stats"]["filtered_relationships"] == 3
    assert graph["stats"]["filtered_reasons"] == {"cross_language": 2, "production_to_test": 1}
    assert graph["stats"]["changed_files"] == 2
    assert graph["stats"]["context_files"] == 2
    assert graph["omitted_node_count"] == 0
    call = next(edge for edge in graph["edges"] if edge["kind"] == "calls" and ids[edge["target"]] == "src/app/util.py")
    assert call["symbols"] == [{"name": "slugify", "count": 1}]
    assert call["evidence"][0]["symbol"] == "slugify"


def test_current_index_cannot_invent_or_strengthen_snapshot_impact_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    """The current index may navigate, not assert Snapshot impact or overwrite its evidence."""
    packet = {
        "files": [{"path": "src/auth/verify.py", "status": "modified"}],
        "symbols": [],
        "impact": [
            {
                "kind": "callsite",
                "path": "src/api/checkout.py:L31",
                "source_path": "src/auth/verify.py",
                "old": "verify",
                "snippet": "verify(user)",
                "uncertainty": "",
            }
        ],
        "order": [],
    }
    relations = [
        _relation("src/auth/verify.py", "src/api/checkout.py", "impacts", 100),
        _relation("src/auth/verify.py", "src/payments/phantom.py", "impacts", 100),
        _relation("src/auth/verify.py", "src/payments/phantom.py", "unrecognized_relation", 100),
        _relation("src/api/checkout.py", "src/auth/verify.py", "calls", 1, "verify"),
    ]
    monkeypatch.setattr(review_map, "_current_context", lambda **_kwargs: _context_with_relationships(relations))
    graph = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=(),
        evidence=(),
        context_repo_root=None,
    )["graph"]
    nodes = {node["id"]: node["path"] for node in graph["nodes"]}
    assert "src/payments/phantom.py" not in nodes.values()
    impacts = [edge for edge in graph["edges"] if edge["kind"] == "impacts"]
    assert len(impacts) == 1
    assert impacts[0]["exactness"] == "snapshot"
    assert impacts[0]["weight"] == 1
    assert impacts[0]["evidence"] == [
        {
            "kind": "callsite",
            "path": "src/api/checkout.py",
            "line": 31,
            "symbol": "verify",
            "snippet": "verify(user)",
            "uncertainty": "",
        }
    ]
    assert any(edge["kind"] == "calls" and edge["exactness"] == "current_index" for edge in graph["edges"])
    assert graph["stats"]["filtered_reasons"]["untrusted_relation_kind"] == 3

    # Drilling into a changed file cannot resurrect fabricated index impact.
    focused = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=(),
        evidence=(),
        context_repo_root=None,
        level="file",
        selected_node_id=review_map._stable_id("file", "src/auth/verify.py"),
    )["graph"]
    assert not any(node["path"] == "src/payments/phantom.py" for node in focused["nodes"])
    assert len([edge for edge in focused["edges"] if edge["kind"] == "impacts"]) == 1


def test_map_drops_out_of_repository_index_paths_and_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale/foreign index cannot make files outside this repository navigable."""
    changed = "src/auth/verify.py"
    packet = {"files": [{"path": changed, "status": "modified"}], "symbols": [], "impact": [], "order": []}
    unsafe = ["../other-repo/private.py", "/etc/passwd", "C:\\secrets\\key.py", "src/../../private.py"]
    relations = [_relation(path, changed, "calls", 9) for path in unsafe]
    relations.append(
        {
            "source_path": "src/api/checkout.py",
            "target_path": changed,
            "kind": "calls",
            "weight": 1,
            "evidence": [{"path": "../../private.py", "line": 7, "symbol": "verify"}],
        }
    )
    current = _context_with_relationships(relations)
    current["matches"] = {"symbol:foreign": {"id": "foreign-symbol", "path": "../../foreign.py"}}
    current["neighborhoods"] = {
        "symbol:foreign": {
            "nodes": [{"id": "foreign-symbol", "path": "../../foreign.py"}],
            "edges": [],
        }
    }
    monkeypatch.setattr(review_map, "_current_context", lambda **_kwargs: current)
    graph = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=(),
        evidence=(),
        context_repo_root=None,
    )["graph"]
    assert {node["path"] for node in graph["nodes"]} == {changed, "src/api/checkout.py"}
    assert graph["stats"]["filtered_reasons"]["unsafe_index_path"] == len(unsafe)
    assert len(graph["edges"]) == 1
    assert graph["edges"][0]["evidence"] == []


def test_architecture_keeps_surrounding_links_when_changed_files_link_densely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    changed = [f"src/app/mod{index:02d}.py" for index in range(30)]
    packet = {
        "files": [{"path": path, "status": "modified", "category": "production"} for path in changed],
        "symbols": [],
        "impact": [],
        "order": [],
    }
    # 870 strong links among the changed files alone exceed the edge budget.
    rows = [_relation(source, target, "calls", 9, "f") for source in changed for target in changed if source != target]
    rows.append(_relation("src/app/cli.py", changed[0], "imports", 1))
    rows.append(_relation(changed[1], "src/lib/util.py", "references", 1, "slug"))
    monkeypatch.setattr(review_map, "_current_context", lambda **_kwargs: _context_with_relationships(rows))

    graph = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=(),
        evidence=(),
        context_repo_root=None,
    )["graph"]

    by_path = {node["path"]: node for node in graph["nodes"]}
    ids = {node["id"]: node["path"] for node in graph["nodes"]}
    assert by_path["src/app/cli.py"]["lane"] == "used_by"
    assert by_path["src/lib/util.py"]["lane"] == "uses"
    shown = {(ids[edge["source"]], ids[edge["target"]]) for edge in graph["edges"]}
    assert ("src/app/cli.py", changed[0]) in shown
    assert (changed[1], "src/lib/util.py") in shown
    assert len(graph["edges"]) == review_map._MAX_OVERVIEW_EDGES


def test_architecture_carries_review_facts_for_triage(monkeypatch: pytest.MonkeyPatch) -> None:
    packet = {
        "files": [
            {
                "path": "src/app/auth/session.py",
                "status": "modified",
                "category": "production",
                "language": "python",
                "additions": 42,
                "deletions": 7,
            },
            {"path": "src/app/new_flow.py", "status": "added", "additions": 90, "deletions": 0},
        ],
        "symbols": [],
        "impact": [],
        "order": [
            {"path": "src/app/new_flow.py", "rank": 2, "score": 1.0, "reasons": ["new production code"]},
            {
                "path": "src/app/auth/session.py",
                "rank": 1,
                "score": 3.0,
                "reasons": ["public contract changed", "6 known callers"],
            },
        ],
    }
    targets = [
        SimpleNamespace(
            target_id="t-1",
            path="src/app/auth/session.py",
            start_line=1,
            end_line=10,
            symbol="",
            state="reviewed",
            changed_since_mark=False,
            attention_level="normal",
            annotation_counts=SimpleNamespace(open=1),
        ),
        SimpleNamespace(
            target_id="t-2",
            path="src/app/auth/session.py",
            start_line=20,
            end_line=30,
            symbol="",
            state="needs_changes",
            changed_since_mark=False,
            attention_level="high",
            annotation_counts=SimpleNamespace(open=2),
        ),
    ]
    monkeypatch.setattr(review_map, "_current_context", lambda **_kwargs: _context_with_relationships([]))

    payload = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=targets,
        evidence=(),
        context_repo_root=None,
    )

    nodes = payload["graph"]["nodes"]
    # Reading order decides the overview order, not path order.
    assert [node["path"] for node in nodes] == ["src/app/auth/session.py", "src/app/new_flow.py"]
    session = nodes[0]
    assert session["additions"] == 42
    assert session["deletions"] == 7
    assert session["change"] == "modified"
    assert session["language"] == "python"
    assert session["module"] == "src/app/auth"
    assert session["rank"] == 1
    assert session["reasons"] == ["public contract changed", "6 known callers"]
    assert session["boundaries"] == ["security"]
    assert session["review"] == {
        "targets": 2,
        "reviewed": 1,
        "needs_changes": 1,
        "changed_since_review": 0,
        "open_threads": 3,
        "attention": "high",
        "outstanding": 1,
        "next_position": -1,
    }
    assert nodes[1]["status"] == "new"
    assert nodes[1]["change"] == "added"
    assert nodes[1]["review"] is None


def test_map_review_facts_carry_the_canonical_queue_position(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spec §2 defect 3: the Map's start list must not keep a queue of its own."""

    from lemoncrow.pro.capabilities.review.review_priority import TargetPriority

    packet = {
        "files": [
            {"path": "src/a.py", "status": "modified"},
            {"path": "src/b.py", "status": "modified"},
        ],
        "symbols": [],
        "impact": [],
        "order": [
            {"path": "src/a.py", "rank": 1, "score": 3.0, "reasons": ["public contract changed"]},
            {"path": "src/b.py", "rank": 2, "score": 1.0, "reasons": []},
        ],
    }

    def _target(target_id: str, path: str, position: int, outstanding: bool) -> SimpleNamespace:
        return SimpleNamespace(
            target_id=target_id,
            path=path,
            start_line=1,
            end_line=2,
            symbol="",
            state="unreviewed" if outstanding else "reviewed",
            changed_since_mark=False,
            attention_level="normal",
            annotation_counts=SimpleNamespace(open=0),
            priority=TargetPriority(position=position, outstanding=outstanding),
        )

    targets = [
        _target("a-1", "src/a.py", 2, False),
        _target("b-1", "src/b.py", 0, True),
        _target("b-2", "src/b.py", 1, True),
    ]
    monkeypatch.setattr(review_map, "_current_context", lambda **_kwargs: _context_with_relationships([]))
    payload = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=targets,
        evidence=(),
        context_repo_root=None,
    )
    review = {node["path"]: node["review"] for node in payload["graph"]["nodes"]}
    assert (review["src/b.py"]["outstanding"], review["src/b.py"]["next_position"]) == (2, 0)
    assert (review["src/a.py"]["outstanding"], review["src/a.py"]["next_position"]) == (0, -1)


def test_file_focus_returns_one_files_full_bounded_neighborhood(monkeypatch: pytest.MonkeyPatch) -> None:
    packet = {
        "files": [
            {"path": "src/app/core.py", "status": "modified"},
            {"path": "src/app/api.py", "status": "modified"},
        ],
        "symbols": [
            {
                "symbol_name": "load",
                "qualified_name": "app.core.load",
                "kind": "function",
                "file_path": "src/app/core.py",
                "start_line": 10,
                "end_line": 30,
                "change": "modified",
            }
        ],
        "impact": [
            {
                "kind": "untouched_caller",
                "path": "src/app/jobs.py:L44",
                "old": "load",
                "snippet": "load(job)",
                "source_path": "src/app/core.py",
                "uncertainty": "",
            }
        ],
        "order": [],
    }
    callers = [
        _relation(f"src/app/callers/c{index:02d}.py", "src/app/core.py", "calls", 1, "load") for index in range(60)
    ]
    rows = [
        *callers,
        _relation("src/app/api.py", "src/app/core.py", "calls", 4, "load"),
        _relation("src/app/core.py", "src/app/util.py", "imports", 1),
        # Relationships of other files are not part of this file's focus.
        _relation("src/app/api.py", "src/app/other.py", "calls", 9, "other"),
    ]
    seen: dict[str, object] = {}

    def fake_context(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return _context_with_relationships(rows)

    monkeypatch.setattr(review_map, "_current_context", fake_context)
    focus_id = review_map._stable_id("file", "src/app/core.py")

    payload = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=(),
        evidence=(),
        context_repo_root=None,
        level="file",
        selected_node_id=focus_id,
    )

    assert seen["relationship_paths"] == ("src/app/core.py",)
    graph = payload["graph"]
    assert graph["level"] == "file"
    assert graph["focus"] == focus_id
    by_path = {node["path"]: node for node in graph["nodes"]}
    focus = by_path["src/app/core.py"]
    assert focus["symbols"][0]["label"] == "load"
    assert focus["selection"]["level"] == "file"
    assert focus["selection"]["focus_node_id"] == focus_id
    # The changed neighbor outranks equally related context callers.
    assert "src/app/api.py" in by_path
    assert by_path["src/app/api.py"]["status"] == "changed"
    assert by_path["src/app/jobs.py"]["status"] == "impacted"
    assert by_path["src/app/jobs.py"]["lane"] == "used_by"
    assert by_path["src/app/util.py"]["lane"] == "uses"
    assert "src/app/other.py" not in by_path
    used_by = [node for node in graph["nodes"] if node["lane"] == "used_by"]
    # 24 per lane, and the sparse "uses" lane lends its 23 spare slots.
    assert len(used_by) == 47
    assert graph["omitted_node_count"] == 15
    assert graph["truncated"] is True
    assert all(focus_id in {edge["source"], edge["target"]} for edge in graph["edges"])
    impact = next(edge for edge in graph["edges"] if edge["kind"] == "impacts")
    assert impact["exactness"] == "snapshot"
    assert impact["evidence"][0]["snippet"] == "load(job)"
    assert impact["selection"]["level"] == "file"

    missing = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet=packet,
        targets=(),
        evidence=(),
        context_repo_root=None,
        level="file",
        selected_node_id="file:not-in-snapshot",
    )
    assert missing["graph"]["nodes"] == []
    assert "not part of this immutable Snapshot" in missing["graph"]["reason"]


def test_symbol_callers_without_their_own_span_are_labelled_by_location(monkeypatch: pytest.MonkeyPatch) -> None:
    symbol = {
        "symbol_name": "total_due",
        "qualified_name": "billing.total_due",
        "kind": "function",
        "file_path": "src/billing/invoice.py",
        "start_line": 40,
        "end_line": 61,
        "change": "modified",
    }
    focus_id = review_map._symbol_node_id(symbol)
    context = _context_with_relationships([])
    context["matches"] = {focus_id: {"id": "native-focus", "path": "src/billing/invoice.py"}}
    context["neighborhoods"] = {
        focus_id: {
            "nodes": [
                {"id": "native-focus", "path": "src/billing/invoice.py", "label": "total_due", "line": 40},
                # Native edges name the symbol they land on, not the caller.
                {"id": "c1", "path": "src/billing/reminders.py", "label": "total_due", "kind": "caller", "line": 88},
                # A caller from another language is a name collision.
                {"id": "c2", "path": "web/src/Invoice.tsx", "label": "total_due", "kind": "caller", "line": 5},
            ],
            "edges": [
                {"source": "c1", "target": "native-focus", "kind": "call", "weight": 1},
                {"source": "c2", "target": "native-focus", "kind": "call", "weight": 1},
            ],
        }
    }
    monkeypatch.setattr(review_map, "_current_context", lambda **_kwargs: context)

    payload = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet={"files": [{"path": "src/billing/invoice.py", "status": "modified"}], "symbols": [symbol], "impact": []},
        targets=(),
        evidence=(),
        context_repo_root=None,
        level="symbol",
        selected_node_id=focus_id,
    )

    labels = sorted(node["label"] for node in payload["graph"]["nodes"])
    assert labels == ["reminders.py:88", "total_due"]
    assert payload["graph"]["stats"]["filtered_relationships"] == 1


def test_symbol_callees_keep_their_names_and_point_out_of_the_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    symbol = {
        "symbol_name": "total_due",
        "qualified_name": "billing.total_due",
        "kind": "function",
        "file_path": "src/billing/invoice.py",
        "start_line": 40,
        "end_line": 61,
        "change": "modified",
    }
    focus_id = review_map._symbol_node_id(symbol)
    context = _context_with_relationships([])
    context["matches"] = {focus_id: {"id": "native-focus", "path": "src/billing/invoice.py"}}
    context["neighborhoods"] = {
        focus_id: {
            "nodes": [
                {"id": "native-focus", "path": "src/billing/invoice.py", "label": "total_due", "line": 40},
                {"id": "k1", "path": "src/billing/tax.py", "label": "rate_for", "kind": "callee"},
                # An override elsewhere shares the focus name; it is still a callee.
                {"id": "k2", "path": "src/billing/base.py", "label": "total_due", "kind": "callee"},
                # Shipping code does not call into a test module.
                {"id": "k3", "path": "tests/billing/test_invoice.py", "label": "make", "kind": "callee"},
            ],
            "edges": [
                {"source": "native-focus", "target": "k1", "kind": "call", "weight": 3},
                {"source": "native-focus", "target": "k2", "kind": "call", "weight": 1},
                {"source": "native-focus", "target": "k3", "kind": "call", "weight": 1},
            ],
            "outgoing_attributed": True,
        }
    }
    monkeypatch.setattr(review_map, "_current_context", lambda **_kwargs: context)

    graph = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet={
            "files": [
                {"path": "src/billing/invoice.py", "status": "modified", "category": "production"},
                {"path": "tests/billing/test_invoice.py", "status": "modified", "category": "test"},
            ],
            "symbols": [symbol],
            "impact": [],
        },
        targets=(),
        evidence=(),
        context_repo_root=None,
        level="symbol",
        selected_node_id=focus_id,
    )["graph"]

    labels = {node["id"]: node["label"] for node in graph["nodes"]}
    calls = sorted((labels[edge["source"]], labels[edge["target"]], edge["weight"]) for edge in graph["edges"])
    assert calls == [("total_due", "rate_for", 3), ("total_due", "total_due", 1)]
    assert graph["stats"] == {"filtered_relationships": 1, "outgoing_attributed": True}


def test_legacy_symbol_callees_are_judged_as_calls_out_of_the_focus(monkeypatch: pytest.MonkeyPatch) -> None:
    # The legacy index gives each neighbor its symbol kind, never "callee", so
    # a test's call into shipping code was judged as shipping code calling the
    # test, filtered as implausible, and a same-named callee lost its name.
    symbol = {
        "symbol_name": "total_due",
        "qualified_name": "test_invoice.total_due",
        "kind": "function",
        "file_path": "tests/billing/test_invoice.py",
        "start_line": 10,
        "end_line": 14,
        "change": "modified",
    }
    focus_id = review_map._symbol_node_id(symbol)
    context = _context_with_relationships([])
    context["matches"] = {focus_id: {"id": "legacy-focus", "path": "tests/billing/test_invoice.py"}}
    context["neighborhoods"] = {
        focus_id: {
            "nodes": [
                {
                    "id": "legacy-focus",
                    "path": "tests/billing/test_invoice.py",
                    "label": "total_due",
                    "kind": "function",
                },
                {
                    "id": "shipping",
                    "path": "src/billing/invoice.py",
                    "label": "total_due",
                    "kind": "function",
                    "line": 40,
                },
            ],
            "edges": [{"source": "legacy-focus", "target": "shipping", "kind": "calls"}],
        }
    }
    monkeypatch.setattr(review_map, "_current_context", lambda **_kwargs: context)

    graph = build_review_map(
        review_id="review-map",
        revision=_revision(),
        packet={
            "files": [
                {"path": "src/billing/invoice.py", "status": "modified", "category": "production"},
                {"path": "tests/billing/test_invoice.py", "status": "modified", "category": "test"},
            ],
            "symbols": [symbol],
            "impact": [],
        },
        targets=(),
        evidence=(),
        context_repo_root=None,
        level="symbol",
        selected_node_id=focus_id,
    )["graph"]

    nodes = {node["id"]: node for node in graph["nodes"]}
    calls = [(nodes[edge["source"]]["path"], nodes[edge["target"]]["path"]) for edge in graph["edges"]]
    assert calls == [("tests/billing/test_invoice.py", "src/billing/invoice.py")]
    assert nodes[graph["edges"][0]["target"]]["label"] == "total_due"
    assert graph["stats"]["filtered_relationships"] == 0
