from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from lemoncrow.core.service.code_map import (
    build_full_graph,
    build_neighborhood,
    classify_activity_event,
    find_references,
    list_file_nodes,
    list_file_symbols,
    recent_activity,
    resolve_project_root,
    search_symbols,
    search_text,
)
from lemoncrow.pro.capabilities.code_context import CodeContextEngine


class _FakeEngine:
    repo_id = "repo-1"

    def search_symbols(self, query: str, **_: object) -> list[SimpleNamespace]:
        assert query == "chargeCard"
        return [
            SimpleNamespace(
                symbol_id="charge",
                symbol_name="chargeCard",
                qualified_name="PaymentGateway.chargeCard",
                file_path="src/payment.py",
                kind="method",
                language="python",
                start_line=12,
                end_line=24,
                score=42.5,
            )
        ]

    def search_text(self, query: str, **_: object) -> list[SimpleNamespace]:
        assert query == "retry"
        return [
            SimpleNamespace(
                file_path="src/payment.py",
                line=18,
                column=9,
                text="return retry(payment)",
            )
        ]

    def tool_callers(self, **_: object) -> dict[str, object]:
        return {
            "target": {
                "id": "charge",
                "name": "chargeCard",
                "qualified_name": "PaymentGateway.chargeCard",
                "path": "src/payment.py",
                "kind": "method",
                "language": "python",
                "line": 12,
                "end_line": 24,
            },
            "related": [
                {
                    "id": "checkout",
                    "name": "checkout",
                    "qualified_name": "checkout",
                    "path": "src/checkout.py",
                    "kind": "function",
                    "line": 4,
                    "end_line": 9,
                }
            ],
            "edges": [
                {
                    "caller_symbol_id": "checkout",
                    "callee_symbol_id": "charge",
                    "depth": 1,
                }
            ],
            "truncated": False,
        }

    def tool_callees(self, **_: object) -> dict[str, object]:
        return {
            "target": {
                "id": "charge",
                "name": "chargeCard",
                "qualified_name": "PaymentGateway.chargeCard",
                "path": "src/payment.py",
                "kind": "method",
                "language": "python",
                "line": 12,
                "end_line": 24,
            },
            "related": [
                {
                    "id": "gateway",
                    "name": "send",
                    "qualified_name": "PaymentGateway.send",
                    "path": "src/payment.py",
                    "kind": "method",
                    "line": 26,
                    "end_line": 31,
                }
            ],
            "edges": [
                {
                    "caller_symbol_id": "charge",
                    "callee_symbol_id": "gateway",
                    "depth": 1,
                }
            ],
            "truncated": True,
        }

    def find_references(self, **kwargs: object) -> dict[str, object]:
        assert kwargs["symbol_id"] == "charge"
        assert kwargs["group_by"] == "none"
        return {
            "reference_count": 1,
            "references": [
                {
                    "file_path": "src/checkout.py",
                    "line": 8,
                    "column": 12,
                    "end_line": 8,
                    "snippet": "return gateway.chargeCard(total)",
                    "caller": "checkout",
                    "edge_kind": "call",
                    "provenance": "ast",
                    "confidence": 1.0,
                }
            ],
            "truncated": False,
        }


def test_resolve_project_root_accepts_registered_project_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.core.service import project_registry

    project_root = tmp_path / "repo"
    project_root.mkdir()
    (project_root / ".lemoncrow").mkdir()
    project_id = project_registry.project_id_for_root(project_root)
    project = project_registry.RegisteredProject(
        project_id=project_id,
        root=project_root.resolve(),
    )
    monkeypatch.setattr(
        project_registry,
        "resolve_registered_project",
        lambda reference: project if reference == project_id else None,
    )

    assert resolve_project_root(project_id) == project_root.resolve()


def test_search_text_returns_exact_source_locations() -> None:
    matches = search_text(_FakeEngine(), "retry", limit=12)

    assert matches == [
        {
            "id": "text::src/payment.py:18:9",
            "path": "src/payment.py",
            "line": 18,
            "column": 9,
            "text": "return retry(payment)",
            "language": "Python",
            "file_type": "source",
        }
    ]


def test_search_symbols_returns_compact_ranked_records() -> None:
    payload = search_symbols(_FakeEngine(), "chargeCard", limit=10)

    assert payload[0] == {
        "id": "charge",
        "label": "chargeCard",
        "qualified_name": "PaymentGateway.chargeCard",
        "path": "src/payment.py",
        "kind": "method",
        "language": "python",
        "line": 12,
        "end_line": 24,
        "score": 42.5,
    }


def test_find_references_returns_compact_usage_locations() -> None:
    payload = find_references(_FakeEngine(), "charge", limit=20)

    assert payload == {
        "symbol_id": "charge",
        "reference_count": 1,
        "references": [
            {
                "path": "src/checkout.py",
                "line": 8,
                "column": 12,
                "end_line": 8,
                "snippet": "return gateway.chargeCard(total)",
                "caller": "checkout",
                "edge_kind": "call",
                "provenance": "ast",
                "confidence": 1.0,
            }
        ],
        "truncated": False,
    }


def test_build_neighborhood_merges_both_directions_and_marks_focus() -> None:
    payload = build_neighborhood(_FakeEngine(), "charge", depth=1, limit=20)

    assert payload["focus"] == "charge"
    assert {node["id"] for node in payload["nodes"]} == {"charge", "checkout", "gateway"}
    assert next(node for node in payload["nodes"] if node["id"] == "charge")["focus"] is True
    assert {(edge["source"], edge["target"]) for edge in payload["edges"]} == {
        ("checkout", "charge"),
        ("charge", "gateway"),
    }
    assert payload["truncated"] is True


def test_classify_activity_event_does_not_expose_raw_output() -> None:
    event = classify_activity_event(
        {
            "kind": "tool_call",
            "at": "2026-07-16T12:00:00Z",
            "summary": "code_search(query=chargeCard)",
            "payload": {
                "tool": "code_search",
                "args": {"query": "chargeCard"},
                "output": "private source that must not cross the API",
            },
        },
        session_id="session-1",
        sequence=3,
    )

    assert event is not None
    assert event["kind"] == "search"
    assert event["query"] == "chargeCard"
    assert "private source" not in json.dumps(event)


def test_classify_read_range_from_batched_files() -> None:
    event = classify_activity_event(
        {
            "kind": "tool_call",
            "at": "2026-07-16T12:00:00Z",
            "payload": {"tool": "mcp__lc__read", "args": {"files": ["src/payment.py:L12-L24"]}},
        },
        session_id="session-1",
        sequence=4,
        project_root=Path("/repo"),
    )

    assert event is not None
    assert event["kind"] == "read"
    assert event["path"] == "src/payment.py"
    assert event["line"] == 12


def test_recent_activity_filters_to_project_and_after_cursor(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    sessions = tmp_path / "runtime" / "sessions" / "2026" / "07" / "16" / "codex" / "session-1"
    sessions.mkdir(parents=True)
    (sessions / "run.json").write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "status": "running",
                "files_touched": [str(project / "src" / "payment.py")],
                "events": [
                    {
                        "kind": "file_edit",
                        "at": "2026-07-16T12:00:00Z",
                        "summary": "edited payment.py",
                        "payload": {"path": str(project / "src" / "payment.py")},
                    },
                    {
                        "kind": "test_result",
                        "at": "2026-07-16T12:01:00Z",
                        "summary": "unit=pass",
                        "payload": {"test_id": "unit", "passed": True},
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = recent_activity(
        tmp_path / "runtime",
        project,
        after="2026-07-16T12:00:30Z",
        limit=20,
    )

    assert payload["session_id"] == "session-1"
    assert payload["host"] == "codex"
    assert payload["status"] == "running"
    assert [event["kind"] for event in payload["events"]] == ["verify"]


def test_recent_activity_uses_workspace_and_resolves_exact_live_targets(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    sessions = tmp_path / "runtime" / "sessions" / "2026" / "07" / "16" / "codex" / "session-2"
    sessions.mkdir(parents=True)
    (sessions / "run.json").write_text(
        json.dumps(
            {
                "session_id": "session-2",
                "workspace_path": str(project),
                "status": "running",
                "files_touched": [],
                "events": [
                    {
                        "kind": "tool_call",
                        "at": "2026-07-16T12:00:00Z",
                        "summary": "code_search(query=chargeCard)",
                        "payload": {"tool": "code_search", "args": {"query": "chargeCard"}},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    payload = recent_activity(tmp_path / "runtime", project, engine=_FakeEngine())

    assert payload["events"][0]["symbol_ids"] == ["charge"]


def test_build_full_graph_includes_every_symbol_file_and_resolved_call(tmp_path: Path) -> None:
    source = tmp_path / "src" / "payments.py"
    source.parent.mkdir()
    source.write_text(
        "def send(amount: int) -> int:\n"
        "    return amount\n\n"
        "def charge_card(amount: int) -> int:\n"
        "    return send(amount)\n",
        encoding="utf-8",
    )
    (tmp_path / "README.md").write_text("# Payments\n", encoding="utf-8")
    engine = CodeContextEngine(
        tmp_path,
        db_path=tmp_path / "index" / "code_context.sqlite",
        autosync_enabled=False,
    )
    engine.index_repo(force=True)

    payload = build_full_graph(engine, tmp_path, max_symbols=100)

    labels = {node["label"] for node in payload["graph"]["nodes"]}
    assert {"payments.py", "send", "charge_card"} <= labels
    assert any(edge["kind"] == "contains" for edge in payload["graph"]["edges"])
    call_edges = [edge for edge in payload["graph"]["edges"] if edge["kind"] == "calls"]
    assert any(
        next(node for node in payload["graph"]["nodes"] if node["id"] == edge["source"])["label"] == "charge_card"
        and next(node for node in payload["graph"]["nodes"] if node["id"] == edge["target"])["label"] == "send"
        for edge in call_edges
    )
    assert payload["total_symbols"] == 3, payload["total_symbols"]
    assert payload["truncated"] is False
    assert payload["groups"][0]["count"] >= 1
    readme = next(node for node in payload["graph"]["nodes"] if node["label"] == "README.md")
    assert readme["file_type"] == "docs"
    assert readme["language"] == "Markdown"

    file_symbols = list_file_symbols(engine, "src/payments.py")
    assert file_symbols["total_symbols"] == 2
    assert [symbol["label"] for symbol in file_symbols["symbols"]] == [
        "send",
        "charge_card",
    ]
    assert file_symbols["truncated"] is False


def test_source_file_listing_is_not_limited_by_graph_file_cap(tmp_path: Path) -> None:
    for name in ("alpha.py", "beta.py", "gamma.py"):
        (tmp_path / name).write_text(
            f"def {name.removesuffix('.py')}() -> None:\n    pass\n",
            encoding="utf-8",
        )
    engine = CodeContextEngine(
        tmp_path,
        db_path=tmp_path / "index" / "code_context.sqlite",
        autosync_enabled=False,
    )
    engine.index_repo(force=True)

    graph = build_full_graph(engine, tmp_path, max_files=1)
    listing = list_file_nodes(engine, tmp_path)

    graph_files = [node for node in graph["graph"]["nodes"] if node.get("node_type") == "file"]
    assert len(graph_files) == 1
    assert listing["total_files"] >= 3
    assert len(listing["files"]) == listing["total_files"]
    assert {"alpha.py", "beta.py", "gamma.py"} <= {file["path"] for file in listing["files"]}
