"""Client ``sql`` and the main package's ``sql`` show the same text and leave the same database.

Both run ``lemoncrow_client.kit.sql``. This pins the glue on each side --
argument aliases and coercion, the operator switches read from each side's
environment, ``.env`` discovery, rendering, the compact-JSON fallback and cell
spills -- over seeded calls, each against its own fresh copy of one fixture.
"""

from __future__ import annotations

import random
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.config import load_config
from lemoncrow_client.localtools import LocalContext, executor_for

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.adapters.mcp.framework import TOOLS
from lemoncrow.gateway.tools.presentation import assemble_response_text
from lemoncrow.gateway.tools.results import clean_tool_result

_SPILL = re.compile(r"full: [^\]]+\]")
_SWITCHES = (
    "DATABASE_URL",
    "POSTGRES_URL",
    "POSTGRESQL_URL",
    "MYSQL_URL",
    "SQLITE_URL",
    "LEMONCROW_SQL_AUTODISCOVER",
    "LEMONCROW_SQL_ALLOW_WRITES",
    "LEMONCROW_SQL_ALLOW_EXTERNAL_DB",
    "LEMONCROW_TOOL_OUTPUT_SPILL",
)
_DSNS = [
    "sqlite:///shop.db",
    "sqlite:///{ROOT}/shop.db",
    "shop.db",
    "file:shop.db",
    "file:shop.db?mode=ro",
    "sqlite:///sub/nested.db",
    "sqlite:///empty.db",
    ":memory:",
    "sqlite:///../outside/ext.db",
    "file:..%2Foutside%2Fext.db",
    "sqlite:///link.db",
    "sqlite:///missing.db",
    "sqlite:///notadb.txt",
    "postgresql://user:secret@db.example/app",
    "mysql://root:hunter2@localhost/app",
    None,
]
_SQLS = [
    "SELECT * FROM users ORDER BY id",
    "SELECT id, total FROM orders ORDER BY id",
    "SELECT id FROM users UNION SELECT id FROM orders WHERE id < 3",
    "WITH t AS (SELECT id FROM users WHERE id < 4) SELECT * FROM t",
    "SELECT note, payload FROM orders WHERE id IN (5, 6, 7) ORDER BY id",
    "SELECT 1.5, NULL, '', 'tab\there' AS \"a\tb\"",
    "SELECT * FROM nosuch",
    "SELEC 1",
    "",
    "PRAGMA table_info(users)",
    "PRAGMA journal_mode(wal)",
    "DELETE FROM users WHERE id = 1",
    "UPDATE users SET name = 'x' WHERE id < 4",
    "INSERT INTO users(name) VALUES('z')",
    "CREATE TABLE t2(a)",
    "ATTACH DATABASE 'x.db' AS x",
    "PRAGMA user_version = 7",
    "SELECT 1; DROP TABLE users",
    'SELECT date_trunc("day", created) FROM t',
]
_BATCHES = [
    [
        {"name": "u", "sql": "SELECT id FROM users ORDER BY id LIMIT 2"},
        {"name": "o", "sql": "SELECT count(*) FROM orders"},
    ],
    [{"sql": "SELECT 1"}, {"name": "bad", "sql": "SELECT * FROM nosuch"}],
    [{"name": "w", "sql": "DELETE FROM users WHERE id = 2"}, {"name": "r", "sql": "SELECT count(*) FROM users"}],
    '[{"name": "json", "sql": "SELECT 2 AS two"}]',
]
_NAMES: list[Any] = ["users", "orders", "nosuch", "USER", ["user", "sku"], ""]


def _build(root: Path) -> None:
    repo, outside = root / "repo", root / "outside"
    (repo / "sub").mkdir(parents=True)
    outside.mkdir()
    conn = sqlite3.connect(repo / "shop.db")
    conn.executescript(
        "CREATE TABLE users(id INTEGER PRIMARY KEY, name TEXT NOT NULL, email TEXT);"
        "CREATE TABLE orders(id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), total REAL, note TEXT, payload BLOB);"
        "CREATE TABLE order_items(order_id INTEGER NOT NULL REFERENCES orders(id), sku TEXT, qty INTEGER);"
    )
    conn.executemany(
        "INSERT INTO users VALUES(?,?,?)", [(i, f"u\t{i}" if i == 3 else f"u{i}", None) for i in range(1, 21)]
    )
    notes = {5: "y" * 5000 + "TAIL", 7: "✓"}
    blobs = {6: bytes(range(256)) * 20, 7: b"\x00\xff"}
    conn.executemany(
        "INSERT INTO orders VALUES(?,?,?,?,?)",
        [(i, i % 20 + 1, i * 1.25, notes.get(i), blobs.get(i)) for i in range(1, 601)],
    )
    conn.commit()
    conn.close()
    for path, script in (
        (repo / "sub" / "nested.db", "CREATE TABLE kv(k TEXT PRIMARY KEY, v TEXT); INSERT INTO kv VALUES ('a', '1');"),
        (repo / "empty.db", "PRAGMA user_version = 1;"),
        (outside / "ext.db", "CREATE TABLE secret(x TEXT); INSERT INTO secret VALUES ('s');"),
    ):
        conn = sqlite3.connect(path)
        conn.executescript(script)
        conn.close()
    (repo / "notadb.txt").write_text("hello\n", encoding="utf-8")
    (repo / "link.db").symlink_to("../outside/ext.db")
    (repo / ".git").mkdir()


def _cases(count: int) -> list[tuple[dict[str, Any], dict[str, str], str | None]]:
    rng = random.Random(20260923)
    cases = []
    actions = [*mcp_server.SQL_TOOL_INPUT_SCHEMA["properties"]["action"]["enum"], "drop"]
    for _ in range(count):
        # Half the calls are well-formed queries on the fixture, so rows, writes
        # and spills are exercised as often as the refusals.
        valid = rng.random() < 0.5
        args: dict[str, Any] = {"action": "query" if valid else rng.choice(actions)}
        dsn = rng.choice(_DSNS[:5] if valid else _DSNS)
        if dsn is not None:
            args["connection_string" if rng.random() < 0.2 else "connection"] = dsn
        if valid or rng.random() < 0.7:
            args["sql"] = rng.choice(_SQLS[:6] if valid and rng.random() < 0.6 else _SQLS)
        if rng.random() < 0.2:
            args["queries"] = rng.choice(_BATCHES)
        if rng.random() < 0.4:
            args["name"] = rng.choice(_NAMES)
        if rng.random() < 0.4:
            args["allow_writes" if rng.random() < 0.2 else "write"] = rng.choice([True, False, "true", 0])
        if rng.random() < 0.3:
            args["max_rows"] = rng.choice([1, 3, "2", 500])
        if rng.random() < 0.2:
            args["auto_limit"] = rng.choice([True, False, "false"])
        env: dict[str, str] = {}
        if valid and rng.random() < 0.3:
            args["write"] = True
            env["LEMONCROW_SQL_ALLOW_WRITES"] = "1"
        for switch, values in (
            ("LEMONCROW_SQL_ALLOW_WRITES", ["1", "0"]),
            ("LEMONCROW_SQL_ALLOW_EXTERNAL_DB", ["1"]),
            ("LEMONCROW_SQL_AUTODISCOVER", ["1", "true"]),
            ("DATABASE_URL", ["sqlite:///sub/nested.db", "postgres://u:p@h/db"]),
        ):
            if switch not in env and rng.random() < 0.3:
                env[switch] = rng.choice(values)
        dotenv = rng.choice(
            [None, None, "export DATABASE_URL=sqlite:///shop.db\n", "SQLITE_URL='sqlite:///empty.db'\n"]
        )
        cases.append((args, env, dotenv))
    return cases


def _substitute(value: Any, root: Path) -> Any:
    if isinstance(value, str):
        return value.replace("{ROOT}", str(root))
    if isinstance(value, list):
        return [_substitute(item, root) for item in value]
    return value


def _normalize(text: str, root: Path) -> str:
    return _SPILL.sub("full: PATH]", text.replace(str(root.parent), "{BASE}"))


def _dump(repo: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{repo / 'shop.db'}?mode=ro", uri=True)
    try:
        return [*conn.iterdump(), str(conn.execute("PRAGMA journal_mode").fetchone()[0])]
    finally:
        conn.close()


def test_client_and_main_sql_show_the_same_text_and_leave_the_same_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = tmp_path / "template"
    _build(template)
    state = tmp_path / "home" / "lemoncrow"
    state.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(tmp_path / "spill"))
    diverged = []
    seen = {"rows": 0, "errors": 0, "spills": 0, "affected": 0, "json": 0}
    for index, (args, env, dotenv) in enumerate(_cases(300)):
        outputs = []
        for side in ("main", "client"):
            base = tmp_path / "runs" / f"{index}-{side}"
            shutil.copytree(template, base, symlinks=True)
            repo = base / "repo"
            if dotenv is not None:
                (repo / ".env").write_text(dotenv, encoding="utf-8")
            call = {key: _substitute(value, repo) for key, value in args.items()}
            if side == "main":
                for switch in _SWITCHES:
                    monkeypatch.delenv(switch, raising=False)
                for switch, value in env.items():
                    monkeypatch.setenv(switch, value)
                monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(repo))
                payload = TOOLS["sql"]["handler"](dict(call))
                payload = clean_tool_result(payload, "sql")
                text = assemble_response_text(payload, mcp_server.render_tool_result_text("sql", payload))
            else:
                config = load_config({"LEMONCROW_HOME": str(state), "HOME": str(state.parent)}, cwd=repo)
                context = LocalContext(config=config, repo_root=repo, sync=None, environment=dict(env))
                result = executor_for("sql")(context, dict(call))
                text = "\n".join(str(block.get("text", "")) for block in result.content)
            outputs.append((_normalize(text, repo), _dump(repo)))
        (main_text, main_db), (client_text, client_db) = outputs
        if main_text != client_text or main_db != client_db:
            diverged.append((index, args, env, main_text[:300], client_text[:300]))
        seen["rows"] += " rows" in main_text and "· error" not in main_text
        seen["errors"] += '"isError":true' in main_text or "· error" in main_text
        seen["spills"] += "full: PATH]" in main_text
        seen["affected"] += " affected" in main_text
        seen["json"] += main_text.startswith("{")
    assert not diverged, f"{len(diverged)} cases diverge, first: {diverged[:2]}"
    assert seen["rows"] >= 80 and seen["errors"] >= 80 and seen["json"] >= 50, seen
    assert seen["spills"] >= 5 and seen["affected"] >= 2, seen
