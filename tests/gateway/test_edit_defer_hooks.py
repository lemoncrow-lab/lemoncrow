from __future__ import annotations

from pathlib import Path

import pytest

from lemoncrow.gateway.adapters.mcp_server import tool_smart_edit


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    f = tmp_path / "m.py"
    # `x=2` is deliberately mis-formatted and is NOT the edited line, so it only
    # gets normalized if the post-edit FORMAT step runs on the touched file.
    f.write_text("y = 1\nx=2\n", encoding="utf-8")
    return f


def test_default_formats_touched_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_DEFER_EDIT_HOOKS", raising=False)
    f = _seed(tmp_path, monkeypatch)
    tool_smart_edit({"edits": [{"path": str(f), "old": "y = 1", "new": "y = 10"}]})
    txt = f.read_text(encoding="utf-8")
    assert "y = 10" in txt  # edit applied
    assert "x = 2" in txt  # default: formatter ran and normalized the unrelated line


def test_defer_skips_mutating_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_DEFER_EDIT_HOOKS", "1")
    f = _seed(tmp_path, monkeypatch)
    tool_smart_edit({"edits": [{"path": str(f), "old": "y = 1", "new": "y = 10"}]})
    txt = f.read_text(encoding="utf-8")
    assert "y = 10" in txt  # edit still applied
    assert "x=2" in txt  # deferred: formatter did NOT touch the file mid-session


def _load_stop_module():
    import sys

    hooks = Path(__file__).resolve().parents[2] / "integrations" / "claude" / "plugin" / "hooks"
    sys.path.insert(0, str(hooks))
    try:
        import stop

        return stop
    finally:
        sys.path.remove(str(hooks))


def test_stop_extracts_edited_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import json

    stop = _load_stop_module()
    tp = tmp_path / "t.jsonl"
    rows = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "name": "mcp__lemon__edit", "input": {"edits": [{"path": "foo.py"}]}}
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": "Write", "input": {"file_path": "bar.py"}}]},
        },
        {"type": "user", "message": {"content": "hi"}},
    ]
    tp.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    assert set(stop._extract_edited_paths(str(tp))) == {"foo.py", "bar.py"}
    # defer off -> no-op, never raises
    monkeypatch.delenv("LEMONCROW_DEFER_EDIT_HOOKS", raising=False)
    assert stop._format_deferred_edits(str(tp)) is None
