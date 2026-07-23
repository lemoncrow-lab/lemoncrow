"""Cursor (IDE + cursor-agent CLI) parity smoke tests.

The real end-to-end proof (driving the actual ``cursor-agent`` binary) lives in
``scripts/smoke_cursor.sh``; these are the CI-safe, binary-free equivalents:

1. ``install_cursor.sh --workspace`` writes a consistent MCP config, both
   lifecycle hooks, and the rules -- and does not regress the ``lc``/``bash``
   naming the loader actually registers.
2. The sessionStart hook bridges the live session id + (real) model so the MCP
   server can attribute and price savings, and it skips ``auto``/``default``
   placeholder models instead of stamping a bogus id.
3. A Cursor MCP tool call is attributed to a real ``cursor`` session (not the
   unattributed quarantine ledger) and surfaces in ``compute_savings_summary``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lemoncrow.core.capabilities.savings_summary import compute_savings_summary
from lemoncrow.core.foundation.paths import session_dir
from lemoncrow.gateway.adapters import mcp_server

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "scripts" / "install_cursor.sh"
HOOK_SESSION_START = REPO / "integrations" / "cursor" / "hooks" / "session_start.py"


def _run_install(ws: Path) -> subprocess.CompletedProcess:
    # Files are written before the script's own post-install verification, so a
    # nonzero exit (e.g. `lc` not on the runner PATH) still leaves the artifacts
    # to assert on. Bounded timeout guards the best-effort `cursor-agent mcp
    # enable` call from ever hanging the suite.
    return subprocess.run(
        ["bash", str(INSTALL), "--workspace", str(ws)],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )


def test_workspace_install_writes_consistent_artifacts(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _run_install(ws)

    mcp = json.loads((ws / ".cursor" / "mcp.json").read_text())
    server = mcp["mcpServers"]["lemoncrow"]
    # The create-branch and merge-branch used to disagree (lemoncrow vs lc,
    # shell vs bash); pin the one true shape so it can't drift again.
    assert server["command"] == "lc"
    assert server["args"] == ["mcp", "--host", "cursor"]
    allow = server["alwaysAllow"]
    assert "bash" in allow and "shell" not in allow

    hooks = json.loads((ws / ".cursor" / "hooks.json").read_text())["hooks"]
    assert any("session_start.py" in e["command"] for e in hooks["sessionStart"])
    assert any("stop.py" in e["command"] for e in hooks["stop"])
    assert (ws / ".cursor" / "hooks" / "session_start.py").is_file()
    assert (ws / ".cursor" / "hooks" / "stop.py").is_file()
    assert list((ws / ".cursor" / "rules").glob("lemoncrow*.mdc"))


def _run_session_start_hook(ws: Path, root: Path, payload: dict) -> None:
    subprocess.run(
        [sys.executable, str(HOOK_SESSION_START)],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
        env={**os.environ, "LEMONCROW_ROOT": str(root), "LEMONCROW_STORE_ROOT": str(root)},
    )


def test_session_start_hook_bridges_id_and_real_model(tmp_path: Path) -> None:
    ws, root = tmp_path / "ws", tmp_path / "store"
    ws.mkdir()
    sid = "conv-real-model"
    _run_session_start_hook(ws, root, {"session_id": sid, "workspace_roots": [str(ws)], "model_id": "claude-opus-4-7"})

    state = json.loads((ws / ".lemoncrow" / "workspace" / "session_state.json").read_text())
    assert state == {"session_id": sid, "host": "cursor"} or (state["session_id"] == sid and state["host"] == "cursor")
    stats = json.loads((session_dir(root, "cursor", sid) / "stats.json").read_text())
    assert stats["model"] == "claude-opus-4-7"
    assert stats["last_model"] == "claude-opus-4-7"


def test_session_start_hook_skips_placeholder_model(tmp_path: Path) -> None:
    # Cursor "auto" selection reports "default"/"auto"; those never resolve to a
    # rate card, so no stats.json should be minted (pricing falls back cleanly).
    ws, root = tmp_path / "ws", tmp_path / "store"
    ws.mkdir()
    sid = "conv-auto"
    _run_session_start_hook(ws, root, {"session_id": sid, "workspace_roots": [str(ws)], "model": "default"})

    assert (ws / ".lemoncrow" / "workspace" / "session_state.json").is_file()
    assert not (session_dir(root, "cursor", sid) / "stats.json").exists()


def test_cursor_mcp_savings_row_is_attributed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `lc mcp --host cursor` does not set CURSOR_SESSION_ID for its own process;
    # the sessionStart bridge does. Simulate the resolved state directly via the
    # env session id (the fast path _resolved_host_session tries before the
    # workspace bridge) and assert the row lands under the real cursor session,
    # not the unattributed quarantine ledger.
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))
    monkeypatch.setenv("LEMONCROW_AGENT", "cursor")
    monkeypatch.setenv("CURSOR_SESSION_ID", "conv-savings")
    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "OPENCODE_SESSION_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(mcp_server, "_resolve_live_session_id", lambda: "")
    mcp_server._SAVINGS_SIDECAR_PATH_BY_SID.clear()

    assert mcp_server._resolved_host_session() == ("conv-savings", "cursor")

    mcp_server._append_savings("code_search", tokens_saved=5000, calls_saved=1)

    sidecar = session_dir(tmp_path, "cursor", "conv-savings") / "savings.jsonl"
    assert sidecar.is_file(), "cursor savings row was not written to the per-session sidecar"
    rows = [json.loads(x) for x in sidecar.read_text().splitlines() if x.strip()]
    assert rows and rows[0]["tokens"] == 5000
    assert not any(r.get("unattributed") for r in rows), "row wrongly quarantined as unattributed"

    summary = compute_savings_summary("conv-savings", lemoncrow_root=tmp_path)
    assert summary.ctx_saved >= 5000


def _make_cursor_agent_session(
    chats: Path, sid: str, cwd: str, messages: list, *, has_conversation: bool = True
) -> None:
    """Write a synthetic cursor-agent chat dir: meta.json + a store.db blob DAG."""
    import sqlite3

    sess = chats / "projhash" / sid
    sess.mkdir(parents=True)
    (sess / "meta.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "createdAtMs": 1784805000000,
                "updatedAtMs": 1784805100000,
                "hasConversation": has_conversation,
                "cwd": cwd,
            }
        )
    )
    conn = sqlite3.connect(sess / "store.db")
    conn.execute("CREATE TABLE blobs (id TEXT PRIMARY KEY, data BLOB)")
    for i, (role, content) in enumerate(messages):
        conn.execute(
            "INSERT INTO blobs VALUES (?, ?)",
            (f"b{i}", json.dumps({"role": role, "content": content}).encode()),
        )
    # A binary link blob (the real DAG has these) must be skipped, not crash.
    conn.execute("INSERT INTO blobs VALUES (?, ?)", ("link", b"\x00\x01\x02not-json"))
    conn.commit()
    conn.close()


def test_cursor_agent_cli_chat_import(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store) -> None:
    from lemoncrow.gateway.hosts.session_parsers.cursor import CursorImporter

    # Isolate discovery to tmp: XDG points at tmp/cursor/chats; HOME at tmp so
    # the ~/.config and macOS fallbacks resolve inside tmp (never the real home).
    chats = tmp_path / "cursor" / "chats"
    _make_cursor_agent_session(
        chats,
        "uuid-real",
        "/work/proj",
        [
            ("system", "You are an AI coding assistant."),
            ("user", "<user_info>OS: linux</user_info>"),
            ("user", "<user_query>Fix the failing test</user_query>"),
            ("assistant", "I'll use LemonCrow code_search then read the file."),
            ("tool", ""),
        ],
    )
    # A ghost/privacy session (no real conversation) must be skipped.
    _make_cursor_agent_session(
        chats, "uuid-ghost", "/work/proj", [("user", "<user_query>/usage</user_query>")], has_conversation=False
    )
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    ids = CursorImporter(store)._import_cursor_agent_chats(force=True)

    assert len(ids) == 1, "expected exactly the one real conversation (ghost session skipped)"
    trace = store.history.get_trace(ids[0])
    assert trace.host == "cursor"
    assert trace.workspace_path == "/work/proj"
    art = store.history.get_raw_artifact(trace.raw_artifact_ids[0])
    content = store.history.read_raw_artifact_content(art)
    rows = [json.loads(x) for x in content.splitlines() if x.strip()]
    # session line + the real user query + the assistant reply; the env-preamble
    # user turn and the system/tool blobs are dropped.
    kinds = [r.get("type") for r in rows]
    assert kinds[0] == "session"
    joined = content
    assert "Fix the failing test" in joined
    assert "user_info" not in joined  # env preamble stripped
