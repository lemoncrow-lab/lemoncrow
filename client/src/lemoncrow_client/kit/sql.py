"""The ``sql`` tool: schema introspection, lint and bounded queries.

One engine for the thin client and the main package. Live introspection and
queries run on SQLite, which the standard library ships; any other dialect gets
a driver-required note and no driver is ever loaded. What only some callers
have -- the main package's spill store for oversized cells -- plugs in through
:class:`SqlHooks`.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .notices import spill_notice

#: The tool's actions, in the order its contract lists them.
SQL_ACTIONS = ("connect", "tables", "schema", "table", "relationships", "search", "lint", "query")
#: Old argument names the tool still accepts, mapped to the current ones.
SQL_PARAM_ALIASES = {"connection_string": "connection", "allow_writes": "write"}

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
# Verbs that are never allowed from the model-facing SQL surface, regardless of
# allow_writes or the server env flag: they touch other files (ATTACH/DETACH),
# change permissions (GRANT/REVOKE), or rewrite the whole DB file (VACUUM).
_ALWAYS_FORBIDDEN_VERBS = {"attach", "detach", "grant", "revoke", "vacuum"}
_AUTO_LIMIT_WRITE_VERBS = frozenset({"insert", "update", "delete", "replace"})
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


class SqlPathError(Exception):
    """Raised when a sqlite DSN resolves outside the repo sandbox."""


@dataclass(frozen=True, slots=True)
class SqlHooks:
    """Main-package capabilities the engine uses when a caller has them."""

    #: Persist an oversized cell's full text; return where, or ``None``.
    cell_spill: Callable[[str], Path | str | None] | None = None


def _environ(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _env_flag(env: Mapping[str, str] | None, name: str) -> bool:
    """An operator switch: on only for 1/true/yes/on, so ``=0`` means off."""
    raw = _environ(env).get(name, "")
    return bool(raw) and raw.strip().lower() in _TRUE_ENV_VALUES


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
            key = re.sub(r"^export\s+", "", key.strip())
            values.setdefault(key, value.strip().strip("\"'"))
    return values


def discover_connection(repo_root: str | Path | None = None, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    root = Path(repo_root or Path.cwd())
    env_map = _environ(env)
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


def _filesystem_path(dsn: str) -> tuple[str, bool, str]:
    """Map a sqlite DSN to (raw_path_for_connect, uri_flag, filesystem_path).

    ``filesystem_path`` is the on-disk path to sandbox-check; it is empty for
    pure in-memory databases, which never touch disk.
    """
    if dsn.startswith("sqlite:///"):
        raw = dsn[len("sqlite:///") :]
        return raw, False, raw
    if dsn.startswith("sqlite://"):
        raw = dsn[len("sqlite://") :]
        return raw, False, raw
    if dsn.startswith("file:"):
        # file:path?mode=ro ... ; the on-disk path is everything between the
        # `file:` scheme and the first query separator. `file::memory:` and
        # any `mode=memory` URI stay in RAM and have no disk path to check.
        # sqlite3.connect(..., uri=True) percent-decodes the path per the
        # SQLite URI spec, so the sandbox check must run on the DECODED path
        # (otherwise `file:..%2F..%2Ftmp%2Fpwn.db` escapes the sandbox while
        # passing an encoded-path check). A NUL byte truncates the C string
        # SQLite opens, so reject it outright.
        body = dsn[len("file:") :]
        fs_part = urllib.parse.unquote(body.split("?", 1)[0])
        if "\x00" in fs_part:
            raise SqlPathError("sqlite file path contains a NUL byte")
        if fs_part.startswith(":memory:") or "mode=memory" in dsn:
            return dsn, True, ""
        return dsn, True, fs_part
    if dsn == ":memory:":
        return dsn, False, ""
    return dsn, False, dsn


def _sqlite_path(dsn: str, repo_root: Path, env: Mapping[str, str] | None = None) -> tuple[str, bool, str]:
    """``(raw, uri, resolved)``: the sandbox-checked file, or ``""`` in memory."""
    raw, uri, fs_path = _filesystem_path(dsn)
    if not fs_path:
        return raw, uri, ""
    candidate = Path(fs_path)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = os.path.realpath(candidate)
    root_resolved = os.path.realpath(repo_root)
    if resolved != root_resolved and not resolved.startswith(root_resolved + os.sep):
        if not _env_flag(env, "LEMONCROW_SQL_ALLOW_EXTERNAL_DB"):
            raise SqlPathError(
                f"sqlite path resolves outside the repo sandbox ({resolved}); "
                "set LEMONCROW_SQL_ALLOW_EXTERNAL_DB=1 to allow external database files"
            )
    return raw, uri, resolved


def _connect_target(raw: str, uri: bool, resolved: str, *, read_only: bool) -> tuple[str, bool]:
    """What ``sqlite3.connect`` opens: the file the sandbox checked.

    A relative DSN names a file under the repository root, whatever the
    process's working directory. A read opens it ``mode=ro``, so SQLite itself
    refuses every write -- ``PRAGMA journal_mode(wal)`` included -- and never
    creates a missing file; ``query_only`` alone lets both through.
    """
    if not resolved:
        return raw, uri
    query = raw.split("?", 1)[1] if uri and "?" in raw else ""
    params = [param for param in query.split("&") if param]
    if read_only:
        params = [param for param in params if not param.startswith("mode=")] + ["mode=ro"]
    elif not uri:
        return resolved, False
    target = "file:" + urllib.parse.quote(resolved)
    return (f"{target}?{'&'.join(params)}" if params else target), True


def _strip_comments(sql: str) -> str:
    return re.sub(r"/\*.*?\*/", "", re.sub(r"--[^\n]*", "", sql), flags=re.S).strip()


def _is_multi_statement(sql: str) -> bool:
    """Quote-aware check for >1 statement; ignores `;` inside string literals."""
    body = sql.rstrip().rstrip(";").rstrip()
    in_single = in_double = False
    for ch in body:
        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ";" and not in_single and not in_double:
            return True
    return False


def _top_level_verb(sql: str) -> str:
    """Leading verb, skipping a leading WITH ... CTE list to the trailing verb."""
    first = re.split(r"\s+", sql, maxsplit=1)[0].lower()
    if first != "with":
        return first
    depth = 0
    in_single = in_double = False
    for match in re.finditer(r"[()'\"]|[A-Za-z_][A-Za-z_]*", sql):
        token = match.group(0)
        if in_single:
            if token == "'":
                in_single = False
            continue
        if in_double:
            if token == '"':
                in_double = False
            continue
        if token == "'":
            in_single = True
        elif token == '"':
            in_double = True
        elif token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0 and token.lower() in _WRITE_PREFIXES | {"select"}:
            return token.lower()
    return "with"


def _has_data_modifying_cte(sql: str) -> bool:
    """True if a write verb opens a parenthesized sub-statement.

    A data-modifying CTE looks like ``WITH x AS (DELETE ... RETURNING ...)``.
    Normal subqueries always open with SELECT/VALUES, so a write verb as the
    first identifier after ``(`` is a reliable signal of a write that
    :func:`_top_level_verb` (which skips parenthesized bodies) would otherwise
    misclassify as a read.
    """
    in_single = in_double = False
    expect_verb = False
    for match in re.finditer(r"[()'\"]|[A-Za-z_][A-Za-z_]*", sql):
        token = match.group(0)
        if in_single:
            if token == "'":
                in_single = False
            continue
        if in_double:
            if token == '"':
                in_double = False
            continue
        if token == "'":
            in_single = True
        elif token == '"':
            in_double = True
        elif token == "(":
            expect_verb = True
        elif token == ")":
            expect_verb = False
        elif expect_verb:
            expect_verb = False
            if token.lower() in _WRITE_PREFIXES:
                return True
    return False


def _writes_enabled(allow_writes: bool, env: Mapping[str, str] | None = None) -> bool:
    """Effective write permission: the caller arg AND the server env flag.

    Writes proceed only when the caller opts in *and* the operator has set
    ``LEMONCROW_SQL_ALLOW_WRITES`` on the server. A model-settable arg alone is
    never sufficient to mutate the database.
    """
    return allow_writes and _env_flag(env, "LEMONCROW_SQL_ALLOW_WRITES")


def _write_refusal(allow_writes: bool) -> str:
    if allow_writes:
        return "write SQL rejected: write=true also needs LEMONCROW_SQL_ALLOW_WRITES=1 in LemonCrow's environment"
    return (
        "write SQL rejected for read-only execution; pass write=true to allow it "
        "(LemonCrow's environment must also set LEMONCROW_SQL_ALLOW_WRITES=1)"
    )


def lint_sql(sql: str, *, allow_writes: bool = True, env: Mapping[str, str] | None = None) -> dict[str, Any]:
    normalized = _strip_comments(sql)
    if not normalized:
        return {"ok": False, "message": "sql is empty"}
    if _is_multi_statement(normalized):
        return {
            "ok": False,
            "message": "multiple statements are not allowed in one sql string; use queries[] for batching",
        }
    verb = _top_level_verb(normalized)
    if verb in _ALWAYS_FORBIDDEN_VERBS:
        return {"ok": False, "message": f"{verb.upper()} is not permitted from the SQL tool"}
    if not _writes_enabled(allow_writes, env) and (verb in _WRITE_PREFIXES or _has_data_modifying_cte(normalized)):
        return {"ok": False, "message": _write_refusal(allow_writes)}
    if not _writes_enabled(allow_writes, env):
        # The engine's query_only guard blocks most writes, but a PRAGMA
        # assignment (PRAGMA query_only=OFF / writable_schema=ON / user_version=N)
        # or REINDEX slips past both query_only and _WRITE_PREFIXES. Reject them so
        # read-only mode cannot be toggled off or the schema rewritten.
        if verb == "pragma" and "=" in normalized:
            return {"ok": False, "message": "PRAGMA assignments are rejected for read-only execution"}
        if verb == "reindex":
            return {"ok": False, "message": "REINDEX is rejected for read-only execution"}
    return {"ok": True, "message": "ok"}


def _compile_check(conn: sqlite3.Connection, sql: str) -> dict[str, Any]:
    """Compile ``sql`` against the live schema without running it (``EXPLAIN``).

    Catches syntax errors and unknown tables or columns. PRAGMAs are skipped:
    some act while they compile.
    """
    verb = _top_level_verb(_strip_comments(sql))
    if verb == "pragma":
        return {"ok": True, "message": "ok"}
    try:
        conn.execute(sql if verb == "explain" else f"EXPLAIN {sql}")
    except sqlite3.Error as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "message": "ok"}


def _cte_trailing_verb_is_write(sql: str) -> bool:
    """True if a leading `WITH ...` resolves to a top-level write verb.

    Mirrors the trailing-verb scan used by the SQL tool's path-confinement
    layer: skip the parenthesized CTE bodies and string literals, then read the
    first depth-0 verb after the CTE list. INSERT/UPDATE/DELETE/REPLACE there
    mean the statement modifies data and must not be wrapped/auto-limited.
    """
    depth = 0
    in_single = in_double = False
    for match in re.finditer(r"[()'\"]|[A-Za-z_][A-Za-z_]*", sql):
        token = match.group(0)
        if in_single:
            if token == "'":
                in_single = False
            continue
        if in_double:
            if token == '"':
                in_double = False
            continue
        if token == "'":
            in_single = True
        elif token == '"':
            in_double = True
        elif token == "(":
            depth += 1
        elif token == ")":
            depth -= 1
        elif depth == 0:
            lowered_token = token.lower()
            if lowered_token in _AUTO_LIMIT_WRITE_VERBS:
                return True
            if lowered_token == "select":
                return False
    return False


def sql_auto_limit(sql: str, max_rows: int, auto_limit: bool = True) -> dict[str, Any]:
    if not auto_limit:
        return {"sql": sql, "changed": False}
    stripped = sql.strip().rstrip(";")
    lowered = stripped.lower()
    is_select = lowered.startswith("select")
    is_cte = lowered.startswith("with")
    if not (is_select or is_cte):
        return {"sql": sql, "changed": False, "reason": "only select statements are auto-limited"}
    # A `WITH ...` prefix is not necessarily a read: a write-CTE
    # (`WITH x AS (...) DELETE FROM t ...`) starts with WITH but its effective
    # top-level verb is a write. Wrapping such a statement as
    # `SELECT * FROM (... DELETE ...)` produces invalid SQL, so detect the
    # trailing top-level verb and skip auto-limit when it modifies data.
    if is_cte and _cte_trailing_verb_is_write(stripped):
        return {"sql": sql, "changed": False, "reason": "write CTEs are not auto-limited"}
    if re.search(r"\blimit\b", lowered):
        return {"sql": sql, "changed": False}
    has_set_op = bool(re.search(r"\b(union|intersect|except)\b", lowered))
    # Plain selects can take a trailing LIMIT directly. Set-operations and
    # WITH-CTE selects must be wrapped so the bound applies to the whole result
    # rather than only the final SELECT branch (or being a syntax error).
    if is_cte or has_set_op:
        return {"sql": f"SELECT * FROM ({stripped}) LIMIT {max_rows}", "changed": True}
    return {"sql": f"{stripped} LIMIT {max_rows}", "changed": True}


def postgres_try_auto_fix(sql: str, error_signature: str) -> dict[str, Any]:
    if "column" in error_signature.lower() and "date_trunc" in sql.lower():
        fixed = re.sub(r'date_trunc\("([a-zA-Z_]+)",', r"date_trunc('\1',", sql)
        if fixed != sql:
            return {"fixed_sql": fixed, "retry": True}
    return {"fixed_sql": sql, "retry": False}


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


def _sqlite_all_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _sqlite_columns(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [
        dict(name=row[1], type=row[2], notnull=bool(row[3]), pk=bool(row[5]))
        for row in conn.execute(f"PRAGMA table_info({table!r})")
    ]


def _sqlite_table_fks(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    return [
        dict(from_column=row[3], table=row[2], to_column=row[4])
        for row in conn.execute(f"PRAGMA foreign_key_list({table!r})")
    ]


def _sqlite_relationships(conn: sqlite3.Connection) -> list[dict[str, str]]:
    rels: list[dict[str, str]] = []
    for table in _sqlite_all_tables(conn):
        for fk in _sqlite_table_fks(conn, table):
            rels.append({"from": f"{table}.{fk['from_column']}", "to": f"{fk['table']}.{fk['to_column']}"})
    return rels


def _sqlite_search(conn: sqlite3.Connection, terms: list[str], *, limit: int = 25) -> list[dict[str, Any]]:
    lowered = [t.lower() for t in terms if t]
    matches: list[dict[str, Any]] = []
    for table in _sqlite_all_tables(conn):
        columns = _sqlite_columns(conn, table)
        table_hit = any(t in table.lower() for t in lowered)
        col_hits = [c for c in columns if any(t in str(c["name"]).lower() for t in lowered)]
        if not table_hit and not col_hits:
            continue
        matches.append(
            {
                "table": table,
                "columns": columns if table_hit else col_hits,
                "foreign_keys": _sqlite_table_fks(conn, table),
            }
        )
        if len(matches) >= limit:
            break
    return matches


_MAX_SQL_CELL_BYTES = 4096


def _cell_spill_hint(full_text: str, *, kept_chars: int, spill: Callable[[str], Path | str | None] | None) -> str:
    """Canonical truncation footer for one oversized SQL cell.

    The full original goes to ``spill`` when the caller has a spill store, and
    the footer names the file; without one (or when the write fails) the
    footer reports a plain truncation -- always non-empty and informative.
    """
    path = spill(full_text) if spill is not None else None
    return " " + spill_notice(
        verb="truncated",
        original_chars=len(full_text),
        kept_chars=kept_chars,
        path=path,
    )


def _bound_cell(value: Any, spill: Callable[[str], Path | str | None] | None = None) -> Any:
    """Cap one cell so a large BLOB/TEXT column can't return MBs in a single response."""
    if isinstance(value, str) and len(value) > _MAX_SQL_CELL_BYTES:
        return value[:_MAX_SQL_CELL_BYTES] + _cell_spill_hint(value, kept_chars=_MAX_SQL_CELL_BYTES, spill=spill)
    if isinstance(value, (bytes, bytearray)) and len(value) > _MAX_SQL_CELL_BYTES:
        hex_val = bytes(value).hex()
        hint = _cell_spill_hint(hex_val, kept_chars=_MAX_SQL_CELL_BYTES, spill=spill)
        return f"<{len(value)} byte blob (hex-encoded)>{hint}"
    return value


def _run_sqlite(
    conn: sqlite3.Connection,
    sql: str,
    max_rows: int,
    spill: Callable[[str], Path | str | None] | None = None,
) -> dict[str, Any]:
    max_rows = max(1, max_rows)
    cursor = conn.execute(sql)
    rows = cursor.fetchmany(max_rows + 1)
    columns = [col[0] for col in cursor.description or []]
    # Rows are positional arrays keyed by `columns` — repeating column names
    # per row wastes tokens on every multi-row result.
    result: dict[str, Any] = {
        "columns": columns,
        "rows": [[_bound_cell(v, spill) for v in row] for row in rows[:max_rows]],
        "row_count": min(len(rows), max_rows),
        "truncated": len(rows) > max_rows,
    }
    if cursor.description is None and cursor.rowcount >= 0:
        result["rows_affected"] = cursor.rowcount
    return result


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
    env: Mapping[str, str] | None = None,
    hooks: SqlHooks | None = None,
) -> dict[str, Any]:
    """Run a structured SQL action with local-first behavior.

    ``env`` holds the operator's switches (``LEMONCROW_SQL_ALLOW_WRITES``,
    ``LEMONCROW_SQL_ALLOW_EXTERNAL_DB``) and the discoverable DSN variables;
    it defaults to this process's environment.
    """
    spill = hooks.cell_spill if hooks is not None else None
    root = Path(repo_root or Path.cwd())
    discovered = discover_connection(root, env) if not connection_string else {}
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

    try:
        raw_path, uri, resolved = _sqlite_path(str(dsn), root, env)
    except SqlPathError as exc:
        return {"isError": True, "message": str(exc)}
    read_only = not _writes_enabled(allow_writes, env)
    if read_only and resolved and not os.path.exists(resolved):
        return {"isError": True, "message": f"no such database file: {_filesystem_path(str(dsn))[2]}"}
    db_path, uri = _connect_target(raw_path, uri, resolved, read_only=read_only)
    started = time.perf_counter()
    conn = sqlite3.connect(db_path, uri=uri, timeout=max(1.0, timeout_ms / 1000.0))
    try:
        conn.row_factory = sqlite3.Row
        if not _writes_enabled(allow_writes, env):
            # Engine-level read-only enforcement (defense in depth beyond
            # lint_sql's verb list, which misses PRAGMA writable_schema /
            # ANALYZE / REINDEX): the connection itself refuses any statement
            # that writes to the database.
            conn.execute("PRAGMA query_only = ON")
        try:
            if action == "connect":
                overview = _sqlite_overview(conn)
                return {
                    "isError": False,
                    "dialect": "sqlite",
                    "connection": mask_connection_string(str(dsn)),
                    "overview": overview,
                    "source": discovered.get("source"),
                }
            if action == "tables":
                tables = _sqlite_all_tables(conn)
                return {"isError": False, "dialect": "sqlite", "tables": tables, "table_count": len(tables)}
            if action == "schema":
                return {"isError": False, "dialect": "sqlite", **_sqlite_overview(conn)}
            if action == "table":
                table_name = str(name or "")
                if not table_name:
                    return {"isError": True, "message": "action='table' requires name=<table>"}
                return {
                    "isError": False,
                    "table": table_name,
                    "columns": _sqlite_columns(conn, table_name),
                    "foreign_keys": _sqlite_table_fks(conn, table_name),
                }
            if action == "relationships":
                return {"isError": False, "dialect": "sqlite", "relationships": _sqlite_relationships(conn)}
            if action == "search":
                terms = name if isinstance(name, list) else [name] if name else []
                if not terms:
                    return {"isError": True, "message": "action='search' requires name=<keyword>"}
                return {"isError": False, "dialect": "sqlite", "matches": _sqlite_search(conn, terms)}
        except sqlite3.Error as exc:
            return {"isError": True, "message": str(exc)}
        if action == "lint":
            problems: list[str] = []
            for item in queries or [{"name": "result", "sql": sql or ""}]:
                statement = item.get("sql") or ""
                lint = lint_sql(statement, allow_writes=allow_writes, env=env)
                if lint["ok"]:
                    lint = _compile_check(conn, statement)
                if not lint["ok"]:
                    label = item.get("name") or "result"
                    problems.append(f"{label}: {lint['message']}" if queries else str(lint["message"]))
            return {"isError": bool(problems), "ok": not problems, "message": "; ".join(problems) or "ok"}
        if action != "query":
            return {"isError": True, "message": f"unsupported action: {action}"}
        batch = queries or [{"name": "result", "sql": sql or ""}]
        outputs: list[dict[str, Any]] = []
        for item in batch:
            label = item.get("name") or "result"
            query_sql = item.get("sql") or ""
            lint = lint_sql(query_sql, allow_writes=allow_writes, env=env)
            if not lint["ok"]:
                outputs.append({"name": label, "isError": True, "message": lint["message"]})
                continue
            limited = sql_auto_limit(query_sql, max_rows=max_rows, auto_limit=auto_limit)
            try:
                if not _writes_enabled(allow_writes, env):
                    # Re-arm read-only on the shared connection before every batch
                    # item so an earlier item cannot leave query_only disabled.
                    conn.execute("PRAGMA query_only = ON")
                result = _run_sqlite(conn, limited["sql"], max_rows, spill)
                if _top_level_verb(_strip_comments(query_sql)) in _WRITE_PREFIXES:
                    conn.commit()
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


def run_sql_tool(
    *,
    action: str,
    name: str | list[str] | None = None,
    sql: str | None = None,
    queries: list[dict[str, str]] | None = None,
    connection: str | None = None,
    max_rows: int = 500,
    timeout_ms: int = 30_000,
    auto_limit: bool = True,
    write: bool = False,
    repo_root: str | Path,
    env: Mapping[str, str] | None = None,
    hooks: SqlHooks | None = None,
) -> dict[str, Any]:
    """The ``sql`` tool's contract around :func:`sql_tool`.

    Refuses an unknown action and a query with nothing to run, and discovers a
    connection from ``DATABASE_URL`` / ``.env`` only when the operator set
    ``LEMONCROW_SQL_AUTODISCOVER``.
    """
    if action not in SQL_ACTIONS:
        return {
            "isError": True,
            "message": "unsupported action: use connect, tables, schema, table, relationships, search, lint, or query",
        }
    if action == "query" and not sql and not queries:
        return {"isError": True, "message": "action='query' requires sql or queries parameter"}
    if not connection and not _env_flag(env, "LEMONCROW_SQL_AUTODISCOVER"):
        return {
            "isError": True,
            "message": (
                "no connection given and auto-discovery is disabled: pass an explicit "
                "connection (e.g. connection='sqlite:///path/to.db') or set "
                "LEMONCROW_SQL_AUTODISCOVER=1 to allow discovering the connection "
                "from DATABASE_URL / .env"
            ),
        }

    result = sql_tool(
        action=action,
        name=name,
        sql=sql,
        queries=queries,
        connection_string=connection,
        max_rows=max_rows,
        timeout_ms=timeout_ms,
        auto_limit=auto_limit,
        allow_writes=write,
        repo_root=repo_root,
        env=env,
        hooks=hooks,
    )
    if isinstance(result, dict) and isinstance(queries, list) and len(queries) > 1:
        result.setdefault("calls_saved", len(queries) - 1)
    return result


def render_sql_text(result: dict[str, Any]) -> str | None:
    """Compact rendering for sql introspection (schema/table/search/relationships/lint).

    Collapses per-column dicts ({cid,name,type,notnull,pk}) -- which repeat their keys
    on every column -- into one line per column. Query rows are already positional;
    render them as a typed tabular stream so the JSON result wrapper does not stay
    resident in model context.
    """

    def _cols(columns: Any) -> list[str]:
        out: list[str] = []
        if isinstance(columns, list):
            for col in columns:
                if not isinstance(col, dict):
                    continue
                parts = [str(col.get("name") or "?"), str(col.get("type") or "")]
                if col.get("pk"):
                    parts.append("pk")
                if col.get("notnull"):
                    parts.append("notnull")
                out.append("  - " + " ".join(p for p in parts if p))
        return out

    def _fks(fks: Any) -> list[str]:
        out: list[str] = []
        if isinstance(fks, list):
            for fk in fks:
                if isinstance(fk, dict):
                    out.append(
                        f"  fk: {fk.get('from_column', '?')} -> {fk.get('table', '?')}.{fk.get('to_column', '?')}"
                    )
        return out

    if isinstance(result.get("results"), list):
        blocks: list[str] = []

        def _header_cell(value: Any) -> str:
            return str(value).replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")

        def _cell(value: Any) -> str:
            if isinstance(value, (bytes, bytearray)):
                return "0x" + bytes(value).hex()
            try:
                return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            except (TypeError, ValueError):
                return json.dumps(str(value), ensure_ascii=False, separators=(",", ":"))

        for item in result["results"]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "result")
            if item.get("isError"):
                message = str(item.get("message") or "query failed").strip()
                blocks.append(f"### sql query {name} · error\n{message}")
                continue
            rows = item.get("rows")
            columns = item.get("columns")
            rows = rows if isinstance(rows, list) else []
            columns = columns if isinstance(columns, list) else []
            row_count = int(item.get("row_count") or len(rows))
            flags: list[str] = [f"{row_count} rows"]
            if item.get("truncated"):
                flags.append("truncated")
            if item.get("auto_limit_changed"):
                flags.append("auto-limit")
            if isinstance(item.get("rows_affected"), int):
                flags.append(f"{item['rows_affected']} affected")
            lines = [f"### sql query {name} · " + " · ".join(flags)]
            if columns:
                lines.append("\t".join(_header_cell(column) for column in columns))
            for row in rows:
                if isinstance(row, (list, tuple)):
                    lines.append("\t".join(_cell(value) for value in row))
                else:
                    lines.append(_cell(row))
            blocks.append("\n".join(lines))
        return "\n\n".join(blocks) if blocks else None
    if isinstance(result.get("schema"), dict):
        schema = result["schema"]
        lines = [f"### sql schema ({result.get('table_count', len(schema))} tables)"]
        for table, info in schema.items():
            lines.append(f"- {table}")
            if isinstance(info, dict):
                lines.extend(_cols(info.get("columns")))
                lines.extend(_fks(info.get("foreign_keys")))
        return "\n".join(lines)
    if isinstance(result.get("matches"), list):
        lines = ["### sql search"]
        for match in result["matches"]:
            if not isinstance(match, dict):
                continue
            lines.append(f"- {match.get('table', '?')}")
            lines.extend(_cols(match.get("columns")))
            lines.extend(_fks(match.get("foreign_keys")))
        return "\n".join(lines)
    if isinstance(result.get("relationships"), list):
        lines = ["### sql relationships"]
        for rel in result["relationships"]:
            if isinstance(rel, dict):
                lines.append(f"- {rel.get('from', '?')} -> {rel.get('to', '?')}")
        return "\n".join(lines)
    if isinstance(result.get("columns"), list) and "table" in result:
        lines = [f"### sql table {result.get('table', '?')}"]
        lines.extend(_cols(result.get("columns")))
        lines.extend(_fks(result.get("foreign_keys")))
        return "\n".join(lines)
    if "ok" in result:  # lint
        return f"### sql lint: {'ok' if result.get('ok') else (result.get('message') or 'invalid')}"
    if isinstance(result.get("tables"), list):
        tables = result["tables"]
        return "\n".join([f"### sql tables ({result.get('table_count', len(tables))})", *(f"- {t}" for t in tables)])
    return None


__all__ = [
    "SQL_ACTIONS",
    "SQL_PARAM_ALIASES",
    "SqlHooks",
    "SqlPathError",
    "detect_dialect",
    "discover_connection",
    "lint_sql",
    "mask_connection_string",
    "postgres_try_auto_fix",
    "render_sql_text",
    "run_sql_tool",
    "sql_auto_limit",
    "sql_tool",
]
