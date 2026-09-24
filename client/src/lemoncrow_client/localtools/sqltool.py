"""``sql``: the kit's SQL engine over the developer's local databases.

The DSN, and the ``.env`` that may hold it, are on the developer's machine, so
the tool runs here -- on :mod:`lemoncrow_client.kit.sql`, the engine the main
package runs too. SQLite is served completely, because ``sqlite3`` is in the
standard library; any other dialect gets the engine's driver-required note.
This package never installs a driver on first use: that would be exactly the
"network-fetched binaries at runtime" finding the design exists to remove.

A read opens the database read-only, so SQLite itself refuses a write. A write
needs ``write=true`` *and* the operator's ``LEMONCROW_SQL_ALLOW_WRITES=1``: a
model-settable argument alone never mutates a database.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import LocalContext, LocalResult, remap_aliases
from .arguments import bool_arg, int_arg, invalid_arg, parse_json_list, text_arg

__all__ = ["run_sql"]


def run_sql(context: LocalContext, arguments: Mapping[str, Any]) -> LocalResult:
    """``sql`` with the main package's contract, arguments and rendering."""
    from ..kit.present import payload_text, strip_empty_values
    from ..kit.sql import SQL_PARAM_ALIASES, SqlHooks, render_sql_text, run_sql_tool

    args = remap_aliases(arguments, SQL_PARAM_ALIASES)
    payload = run_sql_tool(
        action=text_arg("sql", args, "action") or "",
        name=_name(args.get("name")),
        sql=text_arg("sql", args, "sql"),
        queries=_queries(args.get("queries")),
        connection=text_arg("sql", args, "connection"),
        max_rows=int_arg("sql", args, "max_rows", 500),
        timeout_ms=int_arg("sql", args, "timeout_ms", 30_000),
        auto_limit=bool_arg("sql", args, "auto_limit", True),
        write=bool_arg("sql", args, "write", False),
        repo_root=context.repo_root,
        env=context.environment,
        hooks=SqlHooks(cell_spill=_spill_to(context.config.state_dir)),
    )
    payload = strip_empty_values(payload)
    text = payload_text(payload, render_sql_text(payload))
    return LocalResult(content=({"type": "text", "text": text},), is_error=bool(payload.get("isError")))


def _spill_to(state_dir: Path) -> Callable[[str], Path | None]:
    def spill(full_text: str) -> Path | None:
        from .shell import spill_text

        return spill_text(full_text, state_dir, kind="sql")

    return spill


def _name(raw: object) -> str | list[str] | None:
    if raw is None or isinstance(raw, str):
        return raw
    if isinstance(raw, Sequence) and all(isinstance(term, str) for term in raw):
        return list(raw)
    raise invalid_arg("sql", "name", "a string or a list of strings", raw)


def _queries(raw: object) -> list[dict[str, str]] | None:
    """``queries[]``, accepting the JSON string some hosts send for an array."""
    raw = parse_json_list(raw)
    if raw is None:
        return None
    if not isinstance(raw, list) or not all(
        isinstance(entry, Mapping) and all(isinstance(value, str) for value in entry.values()) for entry in raw
    ):
        raise invalid_arg("sql", "queries", "a list of {name, sql} objects", raw)
    return [dict(entry) for entry in raw]
