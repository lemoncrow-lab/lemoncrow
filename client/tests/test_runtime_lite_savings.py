"""Workspaces Lite writes the full server's per-call savings rows."""

from __future__ import annotations

import json
import subprocess
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.lite.index import LiteIndex
from lemoncrow_client.lite.mcp import LiteMcp
from lemoncrow_client.lite.savings import SavingsLedger
from lemoncrow_client.localtools.shell import _compaction_tokens_saved

SID = "7f3c2a10-aaaa-bbbb-cccc-000000000001"
MODEL = "claude-opus-4-5-20251101"


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    return root


def _transcript(tmp_path: Path) -> Path:
    """A Claude transcript: two API responses, the second split into two entries."""
    path = tmp_path / "claude" / "projects" / "-repo" / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)

    def usage(msg_id: str, output: int, cache_read: int) -> str:
        return json.dumps(
            {
                "type": "assistant",
                "message": {
                    "id": msg_id,
                    "model": MODEL,
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": cache_read,
                        "cache_creation_input_tokens": 2_000,
                        "output_tokens": output,
                    },
                },
            }
        )

    path.write_text(
        "\n".join([usage("m1", 300, 50_000), usage("m2", 500, 90_000), usage("m2", 500, 90_000)]) + "\n",
        encoding="utf-8",
    )
    return path


def _env(root: Path, tmp_path: Path, *, session: bool = True) -> dict[str, str]:
    env = {
        "LEMONCROW_WORKSPACE_ROOT": str(root),
        "LEMONCROW_HOME": str(tmp_path / "state"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "HOME": str(tmp_path),
    }
    if session:
        env["CLAUDE_CODE_SESSION_ID"] = SID
    return env


def _rows(tmp_path: Path) -> list[dict[str, Any]]:
    today = date.today()
    path = tmp_path / "state" / "sessions" / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}" / "claude" / SID
    ledger = path / "savings.jsonl"
    if not ledger.exists():
        return []
    return [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def server(tmp_path: Path) -> Any:
    root = _repo(tmp_path)
    _transcript(tmp_path)
    lite = LiteMcp(environment=_env(root, tmp_path))
    try:
        yield lite, root
    finally:
        lite.close()


def test_batched_read_credits_answered_files_with_live_context(server: Any, tmp_path: Path) -> None:
    lite, root = server
    for name in ("a.py", "b.py", "c.py"):
        (root / name).write_text("x = 1\n", encoding="utf-8")

    lite.call("read", {"files": ["a.py", "b.py", "c.py", "missing.py"]})

    [row] = _rows(tmp_path)
    assert row["tool"] == "read"
    assert row["calls"] == 2  # three answered files, one call: two avoided; the missing file earns nothing
    assert row["tokens"] == 0
    assert row["model"] == MODEL
    assert row["ctx_tokens"] == 10 + 90_000 + 2_000  # the latest response's context
    assert row["avg_output_tokens"] == 400  # per response, not per transcript entry
    assert row["source"] == "lite"


def test_single_read_writes_no_row(server: Any, tmp_path: Path) -> None:
    lite, root = server
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    lite.call("read", {"files": ["a.py"]})
    assert _rows(tmp_path) == []


def test_edit_credits_files_past_the_first(server: Any, tmp_path: Path) -> None:
    lite, root = server
    (root / "a.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    (root / "b.py").write_text("z = 3\n", encoding="utf-8")

    answer = lite.call(
        "edit",
        {
            "edits": [
                {"path": "a.py", "old_string": "x = 1", "new_string": "x = 10"},
                {"path": "a.py", "old_string": "y = 2", "new_string": "y = 20"},
                {"path": "b.py", "old_string": "z = 3", "new_string": "z = 30"},
            ]
        },
    )

    assert not answer["isError"]
    [row] = _rows(tmp_path)
    assert (row["tool"], row["calls"], row["tokens"]) == ("edit", 1, 0)


def test_compacted_bash_output_credits_hidden_chars(server: Any, tmp_path: Path) -> None:
    lite, _root = server
    lite.call("bash", {"command": "seq 1 50000"})

    [row] = _rows(tmp_path)
    assert row["tool"] == "bash"
    # Capped at what the host's own Bash would have shown (~30k chars).
    assert 0 < row["tokens"] <= 30_000 // 4
    assert row["calls"] == 0
    assert "ctx_tokens" not in row  # context prices avoided calls only


def test_compaction_credit_is_bounded_by_the_host_cut() -> None:
    assert _compaction_tokens_saved(1_000, 0) == 0
    assert _compaction_tokens_saved(1_000, 4_000) == 1_000
    assert _compaction_tokens_saved(1_000, 10_000_000) == (30_000 - 1_000) // 4
    assert _compaction_tokens_saved(40_000, 5_000) == 0


def test_code_search_credits_each_file_once(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _transcript(tmp_path)
    body = "".join(f"def filler_{n}(value):\n    return value + {n}\n\n" for n in range(200))
    (root / "app.py").write_text(f"def credited_symbol(value):\n    return value * 2\n\n{body}", encoding="utf-8")
    LiteIndex(root).prepare()
    env = _env(root, tmp_path)
    env["LEMONCROW_LITE_PREPARED"] = "1"
    lite = LiteMcp(environment=env)
    try:
        lite.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        first = lite.call("code_search", {"query": "credited_symbol"})
        assert first["structuredContent"]["files"][0].get("source")
        lite.call("code_search", {"query": "credited_symbol"})
    finally:
        lite.close()

    rows = _rows(tmp_path)
    assert rows[0]["tool"] == "code_search"
    assert rows[0]["calls"] >= 1  # the read of app.py the inline source replaced
    assert rows[0]["tokens"] > 0
    # The repeat search re-reads nothing: app.py is not credited again.
    assert all(row["tokens"] == 0 for row in rows[1:])


def test_code_search_skips_source_the_model_was_not_shown(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "a.py").write_text("x" * 4_000, encoding="utf-8")
    ledger = SavingsLedger(tmp_path / "state", {}, root)
    payload = {"files": [{"path": "a.py", "source": "x" * 100}]}
    assert ledger.code_search_savings(payload, "trimmed", root) == (0, 0)
    assert ledger.code_search_savings(payload, "x" * 100, root) == (100 // 4, 1)
    assert ledger.code_search_savings(payload, "x" * 100, root) == (0, 0)


def test_no_host_session_writes_no_row(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    for name in ("a.py", "b.py"):
        (root / name).write_text("x = 1\n", encoding="utf-8")
    lite = LiteMcp(environment=_env(root, tmp_path, session=False))
    try:
        lite.call("read", {"files": ["a.py", "b.py"]})
    finally:
        lite.close()
    assert not (tmp_path / "state" / "sessions").exists()


def test_existing_ledger_partition_is_reused(tmp_path: Path) -> None:
    older = tmp_path / "state" / "sessions" / "2026" / "01" / "02" / "claude" / SID
    older.mkdir(parents=True)
    env = {"CLAUDE_CODE_SESSION_ID": SID, "CLAUDE_CONFIG_DIR": str(tmp_path / "claude")}
    ledger = SavingsLedger(tmp_path / "state", env, tmp_path)
    ledger.record("read", calls=1)
    assert (older / "savings.jsonl").is_file()


def test_profile_runs_write_no_row(tmp_path: Path) -> None:
    env = {"CLAUDE_CODE_SESSION_ID": SID, "LEMONCROW_TOOL_PROFILE_PATH": str(tmp_path / "p.jsonl")}
    SavingsLedger(tmp_path / "state", env, tmp_path).record("read", calls=3)
    assert not (tmp_path / "state" / "sessions").exists()
