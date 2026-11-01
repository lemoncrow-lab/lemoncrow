"""Regression coverage for stale MCP processes and model-visible token waste."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from lemoncrow_client.lite.index import LiteIndex, LiteIndexUpgradeRequired
from lemoncrow_client.lite.mcp import LiteMcp


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / "app.py").write_text("def credit_probe():\n    return 42\n", encoding="utf-8")
    return root


def test_newer_index_requires_restart_and_preserves_database(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    db = LiteIndex(root).db_path
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO meta VALUES('schema_version','999')")
        connection.execute("CREATE TABLE future_data (value TEXT)")
        connection.execute("INSERT INTO future_data VALUES('do-not-delete')")
    with pytest.raises(LiteIndexUpgradeRequired, match="restart the coding-host session"):
        LiteIndex(root)._connect(initialize=True)
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT value FROM future_data").fetchone() == ("do-not-delete",)
        assert connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("999",)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name='files'").fetchone() is None


def test_stale_mcp_returns_actionable_one_shot_error_without_deleting_index(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    db = LiteIndex(root).db_path
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO meta VALUES('schema_version','999')")
    server = LiteMcp(environment={"LEMONCROW_WORKSPACE_ROOT": str(root), "LEMONCROW_HOME": str(tmp_path)})
    try:
        server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "code_search", "arguments": {"query": "credit_probe"}},
            }
        )
        assert response is not None
        result = response["result"]
        assert result["isError"] is True
        message = result["content"][0]["text"]
        assert message.startswith("[lc:upgrade_required]")
        assert "restart the coding-host session" in message
        assert "Do not delete" in message
        assert "retryable=false" in message
        assert "structuredContent" not in result
    finally:
        server.close()


def test_local_lemoncrow_state_never_enters_model_search(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    state = root / ".lemoncrow"
    state.mkdir()
    (state / "telemetry.db").write_bytes(b"SENSITIVE LOCAL INDEX STATE")
    index = LiteIndex(root)
    index.prepare()
    with sqlite3.connect(index.db_path) as connection:
        paths = [str(row[0]) for row in connection.execute("SELECT path FROM files")]
    assert paths == ["app.py"]
    assert not index.search("SENSITIVE LOCAL INDEX STATE")["files"]


def test_v3_bootstrap_does_not_modify_v2_index_used_by_old_process(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    old_db = root / ".lemoncrow/workspace/runtime-lite.sqlite3"
    old_db.parent.mkdir(parents=True)
    with sqlite3.connect(old_db) as connection:
        connection.row_factory = sqlite3.Row
        LiteIndex._ensure_schema(connection)
        connection.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    newer = LiteIndex(root)
    newer.prepare()
    assert newer.db_path != old_db
    assert newer.db_path.is_file()
    with sqlite3.connect(old_db) as old:
        assert old.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("2",)
    with sqlite3.connect(newer.db_path) as current:
        assert current.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone() == ("4",)
    assert newer.search("credit_probe")["files"][0]["path"] == "app.py"


def test_model_search_output_is_bounded_without_hiding_first_match() -> None:
    payload = {
        "files": [
            {
                "path": f"package/module_{i}.py",
                "line": 10,
                "end_line": 20,
                "source": "def matched():\n" + " return value\n" * 150,
            }
            for i in range(20)
        ],
        "candidate_files": ["package/more.py"],
    }
    message = LiteMcp._search_markdown(payload)
    assert message.startswith("## package/module_0.py:L10-L20")
    assert "[lc:output_trimmed]" in message
    assert len(message) <= 12_150
    assert "package/module_19.py" not in message
