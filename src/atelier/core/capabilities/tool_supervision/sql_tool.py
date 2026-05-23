"""Atelier-native SQL tool surface."""

from __future__ import annotations

import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

from atelier.core.capabilities.plugin_runtime import postgres_try_auto_fix, sql_auto_limit

_CONNECTION_KEYS = ("DATABASE_URL", "POSTGRES_URL", "POSTGRESQL_URL", "MYSQL_URL", "SQLITE_URL")
_WRITE_PREFIXES = {
    "insert",
    "update",
    "delete",
    "create",
    "alter",
    "drop",
    "truncate",
    "replace",
    "grant",
    "revoke",
    "vacuum",
    "attach",
    "detach",
}


def mask_connection_string(dsn: str) -> str:
    return re.sub(r"(://[^:/@]+):([^@]+)@", r"\1:****@", dsn)


def detect_dialect(connection_string: str, dialect: str | None = None) -> str:
    if dialect:
        normalized = dialect.lower()
        if normalized in {"postgresql", "postgres", "psql"}:
            return "postgres"
        if normalized in {"mysql", "mariadb"}:
            return "mysql"
        return "sqlite"
    lowered = connection_string.lower()
    if lowered.startswith(("postgres://", "postgresql://")):
        return "postgres"
    if lowered.startswith(("mysql://", "mariadb://")):
        return "mysql"
    return "sqlite"


def _dotenv_values(repo_root: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for name in (".env", ".env.local", ".env.development", ".env.production"):
        path = repo_root / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line or line.strip().startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values.setdefault(key.strip(), value.strip().strip("\"'"))
    return values


def discover_connection(repo_root: str | Path | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
    root = Path(repo_root or Path.cwd())
    env_map = env or dict(os.environ)
    for key in _CONNECTION_KEYS:
        if env_map.get(key):
            return {
                "connection_string": env_map[key],
                "source": f"env:{key}",
                "dialect": detect_dialect(env_map[key]),
            }
    dotenv = _dotenv_values(root)
    for key in _CONNECTION_KEYS:
        if dotenv.get(key):
            return {
                "connection_string": dotenv[key],
                "source": f"dotenv:{key}",
                "dialect": detect_dialect(dotenv[key]),
            }
    return {"connection_string": None, "source": None, "dialect": None}


def _sqlite_path(dsn: str, repo_root: Path) -> tuple[str, bool]:
    if dsn.startswith("sqlite:///"):
        return dsn[len("sqlite:///") :], False
    if dsn.startswith("sqlite://"):
        return dsn[len("sqlite://") :], False
    if dsn.startswith("file:"):
        return dsn, True
    path = Path(dsn)
    if not path.is_absolute():
        path = repo_root / path
    return str(path), False


def _strip_comments(sql: str) -> str:
    return re.sub(r"/\*.*?\*/", "", re.sub(r"--[^\n]*", "", sql), flags=re.S).strip()


def lint_sql(sql: str, *, allow_writes: bool = True) -> dict[str, Any]:
    normalized = _strip_comments(sql)
    if not normalized:
        return {"ok": False, "message": "sql is empty"}
    statements = [part.strip() for part in normalized.split(";") if part.strip()]
    if len(statements) > 1:
        return {
            "ok": False,
            "message": "multiple statements are not allowed in one sql string; use queries[] for batching",
        }
    first = re.split(r"\s+", statements[0], maxsplit=1)[0].lower()
    if not allow_writes and first in _WRITE_PREFIXES:
        return {"ok": False, "message": "write SQL rejected for read-only execution"}
    return {"ok": True, "message": "ok"}


def _sqlite_overview(conn: sqlite3.Connection) -> dict[str, Any]:
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]
    table_info: dict[str, Any] = {}
    for table in tables[:20]:
        columns = [
            dict(cid=row[0], name=row[1], type=row[2], notnull=bool(row[3]), pk=bool(row[5]))
            for row in conn.execute(f"PRAGMA table_info({table!r})")
        ]
        fks = [
            dict(table=row[2], from_column=row[3], to_column=row[4])
            for row in conn.execute(f"PRAGMA foreign_key_list({table!r})")
        ]
        table_info[table] = {"columns": columns, "foreign_keys": fks}
    return {"tables": tables, "table_count": len(tables), "schema": table_info}


def _run_sqlite(conn: sqlite3.Connection, sql: str, max_rows: int) -> dict[str, Any]:
    cursor = conn.execute(sql)
    rows = cursor.fetchmany(max_rows + 1)
    columns = [col[0] for col in cursor.description or []]
    return {
        "columns": columns,
        "rows": [dict(zip(columns, row, strict=False)) for row in rows[:max_rows]],
        "row_count": min(len(rows), max_rows),
        "truncated": len(rows) > max_rows,
    }


def sql_tool(
    *,
    action: str,
    name: str | list[str] | None = None,
    sql: str | None = None,
    queries: list[dict[str, str]] | None = None,
    connection_string: str | None = None,
    dialect: str | None = None,
    max_rows: int = 500,
    timeout_ms: int = 30_000,
    auto_limit: bool = True,
    repo_root: str | Path | None = None,
    allow_writes: bool = True,
) -> dict[str, Any]:
    """Run a structured SQL action with local-first behavior."""
    root = Path(repo_root or Path.cwd())
    discovered = discover_connection(root) if not connection_string else {}
    dsn = connection_string or discovered.get("connection_string")
    if action == "connect" and not dsn:
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": "No database connection configured. Pass connection_string or set DATABASE_URL in the environment or .env file.",
                }
            ],
        }
    if not dsn:
        return {
            "isError": True,
            "content": [
                {
                    "type": "text",
                    "text": "No database connection configured. First run sql(action='connect', connection_string='...') or set DATABASE_URL.",
                }
            ],
        }
    resolved_dialect = detect_dialect(str(dsn), dialect)
    if resolved_dialect != "sqlite":
        if action == "connect":
            return {
                "isError": False,
                "dialect": resolved_dialect,
                "connection": mask_connection_string(str(dsn)),
                "note": "Install the optional database driver to run live queries for this dialect.",
            }
        if resolved_dialect == "postgres" and sql:
            fixed = postgres_try_auto_fix(sql, "column does not exist")
            return {
                "isError": True,
                "dialect": resolved_dialect,
                "connection": mask_connection_string(str(dsn)),
                "driver_required": True,
                "auto_fix_preview": fixed,
            }
        return {
            "isError": True,
            "dialect": resolved_dialect,
            "connection": mask_connection_string(str(dsn)),
            "message": "Optional live driver not installed for this dialect.",
        }

    db_path, uri = _sqlite_path(str(dsn), root)
    started = time.perf_counter()
    conn = sqlite3.connect(db_path, uri=uri, timeout=max(1.0, timeout_ms / 1000.0))
    try:
        conn.row_factory = sqlite3.Row
        if action == "connect":
            overview = _sqlite_overview(conn)
            return {
                "isError": False,
                "dialect": "sqlite",
                "connection": mask_connection_string(str(dsn)),
                "overview": overview,
                "source": discovered.get("source"),
            }
        if action in {"tables", "schema"}:
            return {"isError": False, "dialect": "sqlite", **_sqlite_overview(conn)}
        if action == "table":
            table_name = str(name or "")
            return {
                "isError": False,
                "table": table_name,
                "columns": _sqlite_overview(conn)["schema"].get(table_name, {}).get("columns", []),
            }
        if action == "lint":
            lint = lint_sql(sql or "", allow_writes=allow_writes)
            return {"isError": not lint["ok"], **lint}
        if action != "query":
            return {"isError": True, "message": f"unsupported action: {action}"}
        batch = queries or [{"name": "result", "sql": sql or ""}]
        outputs: list[dict[str, Any]] = []
        for item in batch:
            label = item.get("name") or "result"
            query_sql = item.get("sql") or ""
            lint = lint_sql(query_sql, allow_writes=allow_writes)
            if not lint["ok"]:
                outputs.append({"name": label, "isError": True, "message": lint["message"]})
                continue
            limited = sql_auto_limit(query_sql, max_rows=max_rows, auto_limit=auto_limit)
            try:
                result = _run_sqlite(conn, limited["sql"], max_rows)
                outputs.append({"name": label, **result, "auto_limit_changed": limited.get("changed", False)})
            except sqlite3.Error as exc:
                outputs.append({"name": label, "isError": True, "message": str(exc)})
        return {
            "isError": any(item.get("isError") for item in outputs),
            "dialect": "sqlite",
            "results": outputs,
            "took_ms": int((time.perf_counter() - started) * 1000),
        }
    finally:
        conn.close()


__all__ = [
    "detect_dialect",
    "discover_connection",
    "lint_sql",
    "mask_connection_string",
    "sql_tool",
]
