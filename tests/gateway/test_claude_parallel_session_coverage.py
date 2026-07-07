from __future__ import annotations

import json
from pathlib import Path

from atelier.core.foundation.models import FileEditRecord
from atelier.core.foundation.store import ContextStore
from atelier.gateway.hosts.session_parsers._common import snapshot_edited_files
from atelier.gateway.hosts.session_parsers.claude import ClaudeImporter, find_claude_sessions


def _write_jsonl(path: Path, events: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )


def test_find_claude_sessions_discovers_project_jsonl_files(tmp_path: Path) -> None:
    root = tmp_path / ".claude" / "projects"
    _write_jsonl(root / "workspace-a" / "session-a.jsonl", [{"type": "meta", "sessionId": "session-a"}])
    _write_jsonl(root / "workspace-b" / "session-b.jsonl", [{"type": "meta", "sessionId": "session-b"}])

    sessions = list(find_claude_sessions(root))

    assert sessions == [
        ("workspace-a", root / "workspace-a" / "session-a.jsonl"),
        ("workspace-b", root / "workspace-b" / "session-b.jsonl"),
    ]


def test_claude_import_session_merges_subagent_jsonls(tmp_path: Path) -> None:
    store = ContextStore(tmp_path / "atelier")
    store.init()
    importer = ClaudeImporter(store)

    workspace_slug = "workspace-a"
    filename_session_id = "parent-session"
    logical_session_id = "logical-session"
    root_jsonl = tmp_path / workspace_slug / f"{filename_session_id}.jsonl"
    subagent_jsonl = tmp_path / workspace_slug / filename_session_id / "subagents" / "subagent-worker.jsonl"

    _write_jsonl(
        root_jsonl,
        [
            {"type": "meta", "sessionId": logical_session_id},
            {"type": "user", "message": {"content": "Investigate the regression"}},
            {
                "type": "assistant",
                "message": {
                    "id": "msg-root",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                    "content": [{"type": "text", "text": "Starting on the root session."}],
                },
            },
        ],
    )
    _write_jsonl(
        subagent_jsonl,
        [
            {
                "type": "assistant",
                "message": {
                    "id": "msg-subagent",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 8, "output_tokens": 3},
                    "content": [{"type": "text", "text": "Subagent result."}],
                },
            }
        ],
    )

    result = importer.import_session(workspace_slug, root_jsonl, force=True)

    assert result is not None
    artifacts = store.list_raw_artifacts(source="claude", source_session_id=logical_session_id, limit=10)
    assert [artifact.relative_path for artifact in artifacts] == [
        f"{filename_session_id}/subagents/subagent-worker.jsonl",
        f"{filename_session_id}.jsonl",
    ]


def test_claude_import_accumulates_tools_across_main_and_subagent_files(tmp_path: Path) -> None:
    """Tool tallies must merge across all session files, not just the last one.

    Regression: tally dicts were declared per-file, so a session whose last
    subagent used only builtin tools reported the whole session as builtin-only.
    """
    store = ContextStore(tmp_path / "atelier")
    store.init()
    importer = ClaudeImporter(store)

    workspace_slug = "workspace-a"
    filename_session_id = "tools-session"
    root_jsonl = tmp_path / workspace_slug / f"{filename_session_id}.jsonl"
    subagent_jsonl = tmp_path / workspace_slug / filename_session_id / "subagents" / "subagent-worker.jsonl"

    _write_jsonl(
        root_jsonl,
        [
            {"type": "meta", "sessionId": filename_session_id},
            {"type": "user", "message": {"content": "Do the work"}},
            {
                "type": "assistant",
                "message": {
                    "id": "msg-root",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                    "content": [
                        {"type": "tool_use", "id": "tu-1", "name": "mcp__atelier__read", "input": {"path": "a.py"}},
                        {"type": "tool_use", "id": "tu-2", "name": "mcp__atelier__grep", "input": {"q": "x"}},
                    ],
                },
            },
        ],
    )
    _write_jsonl(
        subagent_jsonl,
        [
            {
                "type": "assistant",
                "message": {
                    "id": "msg-subagent",
                    "model": "claude-sonnet-4-6",
                    "usage": {"input_tokens": 8, "output_tokens": 3},
                    "content": [
                        {"type": "tool_use", "id": "tu-3", "name": "Bash", "input": {"command": "ls"}},
                    ],
                },
            }
        ],
    )

    result = importer.import_session(workspace_slug, root_jsonl, force=True)

    assert result is not None
    traces = [t for t in store.list_traces(limit=10) if t.session_id == filename_session_id]
    assert len(traces) == 1
    tool_counts = {c.name: c.count for c in traces[0].tools_called}
    assert tool_counts == {"mcp__atelier__read": 1, "mcp__atelier__grep": 1, "Bash": 1}


def test_claude_parallel_coverage_matrix_doc_exists_and_has_required_rows() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    # Engineering docs were relocated to docs-internal/ on the bench refactor.
    matrix = repo_root / "docs-internal" / "engineering" / "claude-parallel-session-harvest-matrix.md"
    if not matrix.exists():
        matrix = repo_root / "docs" / "engineering" / "claude-parallel-session-harvest-matrix.md"

    content = matrix.read_text(encoding="utf-8")

    assert "| Foreground |" in content
    assert "| Subagent |" in content
    assert "| Background / agent-view |" in content
    assert "| Teammate |" in content
    assert "| Workflow agent |" in content
    assert "Do not add `~/.claude/jobs/*` scanning" in content


def test_snapshot_edited_files_normalizes_absolute_content_path(tmp_path: Path) -> None:
    store = ContextStore(tmp_path / "atelier")
    store.init()

    edited = tmp_path / "workspace" / "scripts" / "install_claude.sh"
    edited.parent.mkdir(parents=True, exist_ok=True)
    edited.write_text("echo test\n", encoding="utf-8")

    saved = snapshot_edited_files(
        store,
        [FileEditRecord(path=str(edited), diff="", event="edit")],
        session_id="sess-123",
        source="claude",
    )

    assert saved == 1
    artifacts = store.list_raw_artifacts(source="claude", source_session_id="sess-123", limit=10)
    snapshots = [artifact for artifact in artifacts if artifact.kind == "file.snapshot"]
    assert len(snapshots) == 1
    assert snapshots[0].content_path.startswith("raw/claude/snapshots/sess-123/")
    assert not snapshots[0].content_path.startswith("/")
