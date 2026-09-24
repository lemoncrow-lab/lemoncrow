from __future__ import annotations

from lemoncrow.pro.capabilities.code_context.renderer import render_graph_payload


def test_blast_radius_keeps_all_navigation_categories() -> None:
    payload = {
        "kind": "blast_radius",
        "modified_file": "src/base.py",
        "direct_importers": ["src/api.py"],
        "transitive_importers": ["src/app.py"],
        "affected_tests": ["tests/test_api.py"],
        "risk_level": "medium",
        "backend": "server_index",
        "view_revision": 9,
    }
    rendered = render_graph_payload(payload)
    assert rendered == (
        "blast_radius src/base.py · medium · 2 affected\n"
        "direct\n→ src/api.py\n"
        "transitive\n→ src/app.py\n"
        "tests\n→ tests/test_api.py"
    )
    assert "server_index" not in rendered
    assert "view_revision" not in rendered


def test_dead_code_and_cycles_preserve_metrics_and_tail() -> None:
    dead = render_graph_payload(
        {
            "kind": "dead_code",
            "analyzed_files": 50,
            "dead_file_count": 3,
            "dead_files": [
                {
                    "path": "src/old.py",
                    "language": "python",
                    "exports": ["old", "unused"],
                    "complexity_score": 9,
                    "lines_total": 120,
                }
            ],
            "truncated": True,
        }
    )
    assert dead == "dead_code 3/50 files\n→ src/old.py · complexity 9 · 120L · python · exports old,unused\n+2 more"

    cycles = render_graph_payload(
        {"kind": "cycles", "analyzed_files": 20, "cycle_count": 2, "cycles": [["a.py", "b.py"]], "truncated": True}
    )
    assert cycles == "cycles 2 · 20 files\n→ a.py ↔ b.py\n+1 more"


def test_coupling_and_centrality_keep_rank_metrics() -> None:
    coupling = render_graph_payload(
        {
            "kind": "coupling",
            "analyzed_files": 10,
            "coupled_file_count": 1,
            "files": [{"path": "src/a.py", "afferent": 7, "efferent": 3, "instability": 0.3}],
        }
    )
    assert coupling == "coupling 1/10 files\n→ src/a.py · in 7 out 3 · instability 0.3"

    centrality = render_graph_payload(
        {
            "kind": "centrality",
            "node_count": 2,
            "edge_count": 4,
            "ranking": [{"symbol": "svc.run", "in_degree": 3, "out_degree": 1, "degree": 0.8, "eigenvector": 0.42}],
            "truncated": True,
        }
    )
    assert centrality == "centrality 2 nodes · 4 edges\n→ svc.run · in 3 out 1 · degree 0.8 · eig 0.42\n+1 more"


def test_topology_keeps_dependencies_and_hotspots() -> None:
    rendered = render_graph_payload(
        {
            "kind": "topology",
            "analyzed_files": 40,
            "module_count": 1,
            "modules": [
                {
                    "module": "src/api",
                    "files": 7,
                    "depends_on": ["src/db", "src/auth"],
                    "efferent_modules": 2,
                    "afferent_modules": 3,
                }
            ],
            "hotspots": [{"path": "src/api/routes.py", "afferent": 9, "efferent": 4, "instability": 0.3077}],
        }
    )
    assert rendered == (
        "topology 1 modules · 40 files\n"
        "→ src/api · 7 files · in 3 out 2 → src/db,src/auth\n"
        "hotspots\n→ src/api/routes.py · in 9 out 4 · instability 0.3077"
    )


def test_pr_risk_keeps_factors_tests_and_weights() -> None:
    rendered = render_graph_payload(
        {
            "kind": "pr_risk",
            "overall_score": 0.72,
            "overall_tier": "high",
            "file_count": 1,
            "files": [
                {
                    "path": "src/a.py",
                    "score": 0.72,
                    "tier": "high",
                    "factors": {
                        "blast_radius": {"impacted_files": 9, "affected_tests": ["tests/test_a.py"], "factor": 0.8},
                        "churn": {"commit_count": 12, "factor": 0.6, "available": True},
                        "test_gap": {"missing_tests": False, "factor": 0.0},
                        "complexity": {"score": 17, "factor": 0.85},
                    },
                }
            ],
            "weights": {"blast_radius": 0.35, "churn": 0.25, "test_gap": 0.25, "complexity": 0.15},
            "heuristic": True,
        }
    )
    assert rendered == (
        "pr_risk high 0.72 · 1 files · heuristic\n"
        "→ src/a.py · high 0.72 · impact 9 · churn 12 · complexity 17 · tests present\n"
        "  tests tests/test_a.py\n"
        "weights blast_radius 0.35 · churn 0.25 · test_gap 0.25 · complexity 0.15"
    )


def test_unknown_graph_kind_keeps_json_fallback_available() -> None:
    assert render_graph_payload({"kind": "design_gaps", "items": []}) is None
