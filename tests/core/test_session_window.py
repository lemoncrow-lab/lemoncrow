"""Window-anchored session-id resolution: per-window identity files.

Each window writes its OWN file (single writer per file), so concurrent windows
in one workspace never clobber each other -- the race the old shared registry
could not avoid.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atelier.core.foundation import session_window as sw


def _write_window(root: Path, ws: str, pid: int, btime: int, session_id: str) -> None:
    p = sw.window_file_path(root, ws, pid, btime)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps({"session_id": session_id, "window_pid": pid, "window_btime": btime}),
        encoding="utf-8",
    )


def test_window_file_beats_stale_env(tmp_path: Path, monkeypatch: Any) -> None:
    ws = "deadbeef0001"
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: (4242, 999))
    _write_window(tmp_path, ws, 4242, 999, "mine")
    # A sibling window's file must not be consulted; the launch env is stale.
    _write_window(tmp_path, ws, 7777, 111, "sibling")
    assert sw.resolve_window_session_id(tmp_path, ws, env_session_id="stale-launch") == "mine"


def test_clear_overwrites_same_window(tmp_path: Path, monkeypatch: Any) -> None:
    ws = "deadbeef0002"
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: (4242, 999))
    monkeypatch.setattr(sw, "_pid_alive", lambda pid: True)
    sw.register_window_session(tmp_path, ws, session_id="pre-clear", source="startup")
    sw.register_window_session(tmp_path, ws, session_id="post-clear", source="clear")
    assert sw.resolve_window_session_id(tmp_path, ws, env_session_id="pre-clear") == "post-clear"
    # Same window -> exactly one file, overwritten in place.
    assert len(list(sw.windows_dir(tmp_path, ws).glob("*.json"))) == 1


def test_concurrent_windows_isolated(tmp_path: Path, monkeypatch: Any) -> None:
    ws = "deadbeef0003"
    monkeypatch.setattr(sw, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: (100, 1))
    sw.register_window_session(tmp_path, ws, session_id="win-a", source="startup")
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: (200, 2))
    sw.register_window_session(tmp_path, ws, session_id="win-b", source="startup")
    # Neither register clobbered the other: each window resolves its own id.
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: (100, 1))
    assert sw.resolve_window_session_id(tmp_path, ws, env_session_id="") == "win-a"
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: (200, 2))
    assert sw.resolve_window_session_id(tmp_path, ws, env_session_id="") == "win-b"


def test_pid_reuse_guarded_by_btime(tmp_path: Path, monkeypatch: Any) -> None:
    ws = "deadbeef0004"
    _write_window(tmp_path, ws, 4242, 111, "old-proc")
    # Same pid, different start time -> different file -> no match -> env fallback.
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: (4242, 222))
    assert sw.resolve_window_session_id(tmp_path, ws, env_session_id="env-live") == "env-live"


def test_env_fallback_when_no_window(tmp_path: Path, monkeypatch: Any) -> None:
    ws = "deadbeef0005"
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: None)
    _write_window(tmp_path, ws, 1, 1, "some-window")
    assert sw.resolve_window_session_id(tmp_path, ws, env_session_id="env-x") == "env-x"


def test_empty_when_no_window_no_env(tmp_path: Path, monkeypatch: Any) -> None:
    ws = "deadbeef0006"
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: None)
    _write_window(tmp_path, ws, 2, 2, "newest")
    # No window match and no env -> "" (no MRU guess that could mis-attribute).
    assert sw.resolve_window_session_id(tmp_path, ws, env_session_id="") == ""


def test_register_writes_own_file(tmp_path: Path, monkeypatch: Any) -> None:
    ws = "deadbeef0007"
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: (555, 42))
    monkeypatch.setattr(sw, "_pid_alive", lambda pid: True)
    sw.register_window_session(tmp_path, ws, session_id="s1", source="startup", model="m", transcript_path="/t")
    path = sw.window_file_path(tmp_path, ws, 555, 42)
    assert path.is_file()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["session_id"] == "s1" and data["window_pid"] == 555 and data["window_btime"] == 42
    assert sw.resolve_window_session_id(tmp_path, ws, env_session_id="") == "s1"


def test_empty_session_id_not_registered(tmp_path: Path, monkeypatch: Any) -> None:
    ws = "deadbeef0008"
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: (1, 1))
    sw.register_window_session(tmp_path, ws, session_id="")
    assert not list(sw.windows_dir(tmp_path, ws).glob("*.json"))


def test_register_noop_when_no_window(tmp_path: Path, monkeypatch: Any) -> None:
    ws = "deadbeef0009"
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: None)
    sw.register_window_session(tmp_path, ws, session_id="s1")
    assert not list(sw.windows_dir(tmp_path, ws).glob("*.json"))


def test_ps_host_window_id_walks_ancestry(monkeypatch: Any) -> None:
    # pid -> (ppid, btime, name): 300 (worker) <- 200 (claude) <- 100 (zsh)
    table = {300: (200, 5555, "bash"), 200: (100, 4444, "claude"), 100: (1, 3333, "zsh")}
    monkeypatch.setattr(sw, "_ps_proc_table", lambda: table)
    monkeypatch.setattr(sw.os.path, "isdir", lambda p: False)  # no /proc -> ps path
    assert sw.host_window_id(300) == (200, 4444)
    # No claude ancestor -> None (env fallback)
    assert sw.host_window_id(100) is None
    # Unknown pid -> None
    assert sw.host_window_id(999) is None


def test_ps_proc_table_parses_lstart_lines(monkeypatch: Any) -> None:
    out = (
        "  200   100 Fri Jul 10 18:17:26 2026 claude\n"
        "  300   200 Fri Jul  4 08:01:02 2026 /usr/local/bin/some tool\n"
        "garbage line\n"
    )

    class _P:
        stdout = out

    monkeypatch.setattr(sw.subprocess, "run", lambda *a, **k: _P())
    table = sw._ps_proc_table()
    assert table[200][0] == 100 and table[200][2] == "claude"
    assert table[300][2] == "some tool" and table[300][1] > 0


def test_prune_removes_dead_window_files(tmp_path: Path, monkeypatch: Any) -> None:
    ws = "deadbeef0010"
    # A stale file from a dead window, plus the live window registering now.
    _write_window(tmp_path, ws, 999001, 5, "dead-window")
    monkeypatch.setattr(sw, "host_window_id", lambda *a, **k: (999002, 6))
    monkeypatch.setattr(sw, "_pid_alive", lambda pid: pid == 999002)
    sw.register_window_session(tmp_path, ws, session_id="live", source="startup")
    files = {p.name for p in sw.windows_dir(tmp_path, ws).glob("*.json")}
    assert files == {"999002-6.json"}  # dead window's file pruned, live kept
