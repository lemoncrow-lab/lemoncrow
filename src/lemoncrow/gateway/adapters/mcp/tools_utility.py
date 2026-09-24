"""Low-coupling utility MCP handlers.

These tools own no MCP transport state and can register independently from the
legacy ``mcp_server`` composition module.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from lemoncrow_client.kit.security_scan import run_scan_tool
from lemoncrow_client.kit.sql import SQL_PARAM_ALIASES, run_sql_tool

from lemoncrow.core.capabilities.orientation import orientation_playbook
from lemoncrow.gateway.adapters.mcp.framework import mcp_tool
from lemoncrow.gateway.tools.workspace import workspace_root
from lemoncrow.infra.code_intel.astgrep import astgrep_adapter
from lemoncrow.pro.capabilities.tool_supervision.sql_tool import SQL_HOOKS

SQL_TOOL_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": [
                "connect",
                "tables",
                "schema",
                "table",
                "relationships",
                "search",
                "lint",
                "query",
            ],
            "description": "table/search need name; lint/query need sql or queries[].",
        },
        "name": {
            "type": "string",
            "description": "Target table for action=table, or keyword for action=search.",
        },
        "sql": {
            "type": "string",
            "description": "SQL string for action=lint or action=query.",
        },
        "queries": {
            "type": "array",
            "description": "Batch for action=query: [{name, sql}, ...]. Prefer over repeated calls.",
            "items": {
                "type": "object",
                "required": ["sql"],
                "properties": {
                    "name": {"type": "string"},
                    "sql": {"type": "string"},
                },
            },
        },
        "connection": {
            "type": "string",
            "description": (
                "DSN (sqlite:///path, postgresql://...). If omitted, auto-discovery "
                "from DATABASE_URL/.env requires LEMONCROW_SQL_AUTODISCOVER=1."
            ),
        },
        "write": {
            "type": "boolean",
            "default": False,
            "description": "Permit INSERT/UPDATE/DELETE/DDL on action=query/lint. Off by default; reads always allowed.",
        },
    },
    "required": ["action"],
    "additionalProperties": False,
}


@mcp_tool(
    name="sql",
    input_schema=SQL_TOOL_INPUT_SCHEMA,
    description=(
        "SQL op-dispatch: schema introspection (connect/tables/schema/table/"
        "relationships/search), lint, bounded query execution (single `sql` or "
        "`queries[]` batch). Pass connection (DSN); auto-discovery from "
        "DATABASE_URL/.env requires LEMONCROW_SQL_AUTODISCOVER=1. Live introspection/queries = SQLite; "
        "other dialects → driver-required note."
    ),
    param_aliases=SQL_PARAM_ALIASES,
)
def tool_sql(
    action: str,
    name: str | list[str] | None = None,
    sql: str | None = None,
    queries: list[dict[str, str]] | None = None,
    connection: str | None = None,
    max_rows: int = 500,
    timeout_ms: int = 30_000,
    auto_limit: bool = True,
    write: bool = False,
) -> dict[str, Any]:
    """SQL op-dispatch for connect, lint, and bounded query batching.

    Actions:
      connect       — discover database and show schema overview
      tables        — list table names (+ count)
      schema        — columns + foreign keys per table
      table         — one table's columns + foreign keys (needs name)
      relationships — foreign-key graph as {from: "t.col", to: "rt.col"}
      search        — keyword over table/column names -> matching tables with columns + FKs (needs name)
      lint          — compile SQL against the schema without running it (needs sql or queries[])
      query         — execute SQL (needs sql or queries[{name,sql},...])

    Pass connection explicitly (DSN). Auto-discovery from the DATABASE_URL env
    var or a .env file is opt-in: set LEMONCROW_SQL_AUTODISCOVER=1.
    Live introspection/queries run on SQLite; other dialects report a
    driver-required note.
    """
    return run_sql_tool(
        action=action,
        name=name,
        sql=sql,
        queries=queries,
        connection=connection,
        max_rows=max_rows,
        timeout_ms=timeout_ms,
        auto_limit=auto_limit,
        write=write,
        repo_root=os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd()),
        hooks=SQL_HOOKS,
    )


@mcp_tool(name="scan")
def tool_scan(
    path: str | None = None,
    include_taint: bool = True,
    include_rules: bool = True,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """Security scan (SAST, first iteration) over the repo or a sub-path.

    Runs a small bundled pack of high-signal OWASP/CWE ast-grep rules plus a
    bounded intra-procedural Python taint check. This is intentionally a first
    iteration, not an exhaustive SAST engine.
    """
    workspace = workspace_root()
    root_arg = repo_root or "."
    root_path = Path(root_arg)
    resolved_root = (root_path if root_path.is_absolute() else workspace / root_path).resolve()
    return run_scan_tool(
        resolved_root,
        path=path,
        include_taint=include_taint,
        include_rules=include_rules,
        adapter_factory=astgrep_adapter,
    )


@mcp_tool(name="orient")
def tool_orient(topic: str | None = None) -> dict[str, Any]:
    """Return LemonCrow's tool-usage playbook on demand (N8)."""
    return orientation_playbook(topic)


__all__ = [
    "SQL_TOOL_INPUT_SCHEMA",
    "tool_orient",
    "tool_scan",
    "tool_sql",
]
