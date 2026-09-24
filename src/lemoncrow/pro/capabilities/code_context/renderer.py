"""Compact benchmark-facing renderers for code context payloads."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from lemoncrow_client.kit.astgrep import group_rows_by_file, render_pattern_text

# Matches the "\d+\t" line-number prefix baked into explore source sections.
_LINE_NUM_RE = re.compile(r"^\d+\t")

_CONTEXT_ENTRY_CAP = 8
_CONTEXT_RELATED_CAP = 10
_CONTEXT_CODE_BLOCK_CAP = 3
_CONTEXT_PER_FILE_CAP = 3
_EXPLORE_FILE_SYMBOL_CAP = 8
_NODE_BODY_MAX_LINES = 400
_NODE_BODY_HEAD_LINES = 120


def render_code_payload(op: str, payload: Mapping[str, Any]) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    if payload.get("error"):
        return None
    if op == "search":
        return _render_search(payload)
    if op in {"symbol", "node"}:
        # The compact `symbol` view is location/signature-only; only the `node`
        # view emits the source body.
        return _render_symbol(payload, include_source=(op == "node"))
    if op in {"callers", "callees", "usages"}:
        return _render_relations(op, payload)
    if op == "pattern":
        return render_pattern_text(payload)
    if op == "blame":
        return _render_blame(payload)
    if op == "outline":
        return _render_outline(payload)
    if op == "status":
        return _render_status(payload)
    if op == "index":
        return _render_index(payload)
    if op == "cache_status":
        return _render_cache_status(payload)
    if op == "context":
        return _render_context(payload)
    if op == "explore":
        return _render_explore(payload)
    if op == "files":
        return _render_files(payload)
    if op == "routes":
        return _render_routes(payload)
    return None


def _render_search(payload: Mapping[str, Any]) -> str:
    items = payload.get("items")
    if not isinstance(items, list):
        return "- no matches"
    rows: list[tuple[str, int, str]] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        file_path = str(item.get("path") or item.get("file_path") or "?")
        line = int(item.get("line") or item.get("start_line") or 0)
        name = str(item.get("qualified_name") or item.get("name") or item.get("symbol_name") or "?")
        kind = str(item.get("kind") or "?")
        rows.append((file_path, line, f"{name} [{kind}]"))
    if not rows:
        return "- no matches"
    lines: list[str] = []
    lines.extend(group_rows_by_file(rows))
    return "\n".join(lines)


def _render_symbol(payload: Mapping[str, Any], *, include_source: bool = True) -> str:
    symbol_id = str(payload.get("symbol_id") or "").strip()
    file_path = str(payload.get("path") or payload.get("file_path") or "?")
    start_line = int(payload.get("line") or payload.get("start_line") or 0)
    end_line = int(payload.get("end_line") or 0)
    symbol = str(payload.get("qualified_name") or payload.get("name") or payload.get("symbol_name") or symbol_id or "?")
    kind = str(payload.get("kind") or "?")
    signature = str(payload.get("signature") or "").strip()
    # Header: same format as explore sections — #### path:Lx-Ly — name [kind]
    range_tag = (
        f":L{start_line}-L{end_line}"
        if start_line and end_line >= start_line
        else (f":L{start_line}" if start_line else "")
    )
    header = f"#### {file_path}{range_tag} — {symbol} [{kind}]"
    lines: list[str] = [header]
    if symbol_id:
        lines.append(f"- id: {symbol_id}")
    if signature:
        lines.append(f"- signature: {signature}")
    source = str(payload.get("source") or "")
    if source and include_source:
        language = str(payload.get("language") or "")
        body_lines = source.splitlines()
        if len(body_lines) <= _NODE_BODY_MAX_LINES:
            kept = body_lines
        else:
            kept = body_lines[:_NODE_BODY_HEAD_LINES]
            lines.append(
                f"*first {_NODE_BODY_HEAD_LINES} of {len(body_lines)} lines; "
                f"read L{start_line}-L{end_line} for the rest*"
            )
        if start_line > 0:
            body = "\n".join(f"{start_line + idx}\t{line}" for idx, line in enumerate(kept))
        else:
            body = "\n".join(kept)
        lines.append(f"```{language}" if language else "```")
        lines.append(body)
        lines.append("```")
    return "\n".join(lines)


def _render_relations(op: str, payload: Mapping[str, Any]) -> str:
    rows: list[tuple[str, int, str]] = []
    if op == "usages":
        for ref in _flatten_usages(payload.get("references")):
            file_path = str(ref.get("path") or ref.get("file_path") or "?")
            line = int(ref.get("line") or 0)
            caller = str(ref.get("caller") or ref.get("enclosing_qualified_name") or "").strip()
            rows.append((file_path, line, caller))
    else:
        related = payload.get("related")
        if isinstance(related, list):
            for item in related:
                if not isinstance(item, Mapping):
                    continue
                name = str(item.get("qualified_name") or item.get("name") or item.get("symbol_name") or "?")
                file_path = str(item.get("path") or item.get("file_path") or "?")
                line = int(item.get("line") or item.get("start_line") or 0)
                rows.append((file_path, line, name))

    if not rows:
        return f"{op}\n- none"

    from itertools import groupby

    lines = [op]
    ordered = sorted(rows, key=lambda row: (row[0], row[1], row[2]))
    for file_path, group in groupby(ordered, key=lambda row: row[0]):
        entries = list(group)
        if len(entries) == 1:
            _path, line, label = entries[0]
            pointer = file_path + (f":L{line}" if line > 0 else "")
            lines.append(f"→ {pointer}" + (f" · {label}" if label else ""))
            continue
        compact: list[str] = []
        for _path, line, label in entries:
            loc = f"L{line}" if line > 0 else "?"
            compact.append(loc + (f" · {label}" if label else ""))
        lines.append(f"→ {file_path}:" + "; ".join(compact))

    if payload.get("truncated"):
        total = payload.get("total_matches") or payload.get("reference_count")
        if isinstance(total, int) and total > len(rows):
            lines.append(f"+{total - len(rows)} more")
        else:
            lines.append("+more")
    return "\n".join(lines)


def _flatten_usages(references: Any) -> list[Mapping[str, Any]]:
    if isinstance(references, list):
        rows = [item for item in references if isinstance(item, Mapping)]
    elif isinstance(references, Mapping):
        rows = []
        for key in sorted(references.keys(), key=lambda value: str(value)):
            value = references[key]
            if isinstance(value, list):
                rows.extend(item for item in value if isinstance(item, Mapping))
    else:
        rows = []
    return sorted(
        rows,
        key=lambda item: (
            str(item.get("path") or item.get("file_path") or ""),
            int(item.get("line") or 0),
            int(item.get("column") or 0),
        ),
    )


def _render_blame(payload: Mapping[str, Any]) -> str:
    name = str(payload.get("qualified_name") or payload.get("name") or payload.get("symbol_name") or "?")
    file_path = str(payload.get("path") or payload.get("file_path") or "?")
    start = int(payload.get("line_start") or 0)
    end = int(payload.get("line_end") or 0)
    loc = f"{file_path}:{start}-{end}" if start and end else file_path
    lines = [f"- target: {name} ({loc})"]
    last_author = str(payload.get("last_author") or payload.get("author") or "").strip()
    last_sha = str(payload.get("last_commit_sha") or "").strip()[:10]
    summary = str(payload.get("last_commit_summary") or "").strip()
    if last_sha or last_author:
        head = " ".join(part for part in (last_sha, last_author) if part)
        lines.append(f"- last: {head}" + (f" — {summary}" if summary else ""))
    meta: list[str] = []
    if payload.get("age_days") is not None:
        meta.append(f"age_days={payload['age_days']}")
    if payload.get("distinct_authors") is not None:
        meta.append(f"authors={payload['distinct_authors']}")
    if payload.get("local_edits"):
        meta.append("local_edits=true")
    freshness = str(payload.get("freshness") or "").strip()
    if freshness:
        meta.append(f"freshness={freshness}")
    if meta:
        lines.append("- " + ", ".join(meta))
    hunks = payload.get("hunks")
    if isinstance(hunks, list) and hunks:
        lines.append(f"- hunks ({len(hunks)}):")
        for hunk in hunks:
            if not isinstance(hunk, Mapping):
                continue
            hs = int(hunk.get("start_line") or hunk.get("line") or 0)
            he = int(hunk.get("end_line") or 0)
            rng = f"{hs}-{he}" if hs and he else (str(hs) if hs else "?")
            sha = str(hunk.get("commit_sha") or "").strip()[:10]
            author = str(hunk.get("author_email") or "").strip()
            lines.append(f"  - {rng} " + " ".join(part for part in (sha, author) if part))
    churn = payload.get("churn")
    if isinstance(churn, Mapping) and churn:
        parts = []
        if churn.get("commit_count") is not None:
            parts.append(f"commits={churn['commit_count']}")
        if churn.get("score") is not None:
            parts.append(f"score={churn['score']}")
        if parts:
            lines.append("- churn: " + ", ".join(parts))
    return "\n".join(lines)


def _render_outline(payload: Mapping[str, Any]) -> str | None:
    files = payload.get("files")
    if not isinstance(files, Mapping):
        return None
    total = payload.get("symbol_count")
    lines: list[str] = []
    if isinstance(total, int):
        lines.append(f"- outline: {total} symbols")
    for file_path in sorted(files.keys(), key=str):
        symbols = files[file_path]
        if not isinstance(symbols, list):
            continue
        lines.append(f"- {file_path}")
        for symbol in symbols:
            if not isinstance(symbol, Mapping):
                continue
            name = str(symbol.get("qualified_name") or symbol.get("name") or "?")
            kind = str(symbol.get("kind") or "?")
            start = symbol.get("line_start") or symbol.get("start_line")
            end = symbol.get("line_end") or symbol.get("end_line")
            rng = f"{start}-{end}" if start and end else (str(start) if start else "")
            lines.append(f"  - {rng}: {name} [{kind}]" if rng else f"  - {name} [{kind}]")
    return "\n".join(lines)


def _render_status(payload: Mapping[str, Any]) -> str:
    index = payload.get("index")
    cache = payload.get("cache")
    freshness = payload.get("freshness")
    providers = payload.get("providers")
    files_indexed = int(index.get("files_indexed") or 0) if isinstance(index, Mapping) else 0
    symbols_indexed = int(index.get("symbols_indexed") or 0) if isinstance(index, Mapping) else 0
    entry_count = int(cache.get("entry_count") or 0) if isinstance(cache, Mapping) else 0
    freshness_status = str(freshness.get("status") or "unknown") if isinstance(freshness, Mapping) else "unknown"
    lines = [
        f"- repo: {(payload.get('repo_root') or payload.get('repo_id') or '?')!s}",
        f"- index: files={files_indexed}, symbols={symbols_indexed}",
        f"- cache_entries: {entry_count}",
        f"- freshness: {freshness_status}",
    ]
    if isinstance(providers, list):
        provider_rows = sorted(
            (provider for provider in providers if isinstance(provider, Mapping)),
            key=lambda provider: str(provider.get("name") or ""),
        )
        for provider in provider_rows:
            lines.append(f"- provider:{(provider.get('name') or '?')!s}={(provider.get('status') or 'unknown')!s}")
    return "\n".join(lines)


def _render_index(payload: Mapping[str, Any]) -> str:
    files_indexed = int(payload.get("files_indexed") or 0)
    symbols_indexed = int(payload.get("symbols_indexed") or 0)
    imports_indexed = int(payload.get("imports_indexed") or 0)
    index_version = int(payload.get("index_version") or 0)
    lines = [
        f"- repo: {(payload.get('repo_id') or '?')!s}",
        f"- version: {index_version}",
        f"- counts: files={files_indexed}, symbols={symbols_indexed}, imports={imports_indexed}",
    ]
    return "\n".join(lines)


def _render_cache_status(payload: Mapping[str, Any]) -> str:
    entry_count = int(payload.get("entry_count") or 0)
    total_bytes = int(payload.get("total_bytes") or 0)
    max_bytes = int(payload.get("max_bytes") or 0)
    index_version = int(payload.get("index_version") or 0)
    by_tool = payload.get("entries_by_tool")
    tool_summary = ""
    if isinstance(by_tool, Mapping):
        entries = [f"{key!s}={int(value)}" for key, value in sorted(by_tool.items(), key=lambda item: str(item[0]))]
        if entries:
            tool_summary = ", ".join(entries[:4])
            if len(entries) > 4:
                tool_summary = f"{tool_summary}, +{len(entries) - 4} more"
    lines = [
        f"- repo: {(payload.get('repo_id') or '?')!s}",
        f"- index_version: {index_version}",
        f"- entries: {entry_count}",
        f"- bytes: {total_bytes}/{max_bytes}",
    ]
    if tool_summary:
        lines.append(f"- tools: {tool_summary}")
    return "\n".join(lines)


def _render_context(payload: Mapping[str, Any]) -> str:
    entry_points = _normalize_context_symbols(payload.get("entry_points"), fallback=payload.get("symbols"))
    related_symbols = _normalize_context_symbols(payload.get("related_symbols"), fallback=[])
    code_blocks = _normalize_code_blocks(payload.get("code_blocks"))
    import_neighbors = payload.get("import_neighbors")
    parts: list[str] = []

    # Source is the highest-value context. Render it first and treat its exact
    # symbol location as already navigated so the pointer sections do not repeat
    # the same path/name a second time.
    covered: set[tuple[str, int, str]] = set()
    for block in code_blocks[:_CONTEXT_CODE_BLOCK_CAP]:
        path = str(block["file_path"])
        start = int(block["start_line"])
        end = int(block["end_line"])
        name = str(block["qualified_name"])
        covered.add((path, start, name))
        header = f"## {path}:L{start}-L{end} · {name}"
        source = str(block["source"]).rstrip()
        if source:
            language = str(block.get("language") or "")
            fence = f"```{language}" if language else "```"
            parts.append(f"{header}\n{fence}\n{source}\n```")
        else:
            parts.append(header)

    def pointer_rows(rows: list[dict[str, Any]], *, cap: int) -> list[str]:
        out: list[str] = []
        for row in rows[:cap]:
            path = str(row["file_path"])
            line = int(row["start_line"])
            name = str(row["qualified_name"])
            if (path, line, name) in covered:
                continue
            kind = str(row.get("kind") or "").strip().lower()
            suffix = "" if kind in {"", "?", "function"} else f" · {kind}"
            pointer = path + (f":L{line}" if line > 0 else "")
            out.append(f"→ {pointer} · {name}{suffix}")
        return out

    entry_lines = pointer_rows(entry_points, cap=_CONTEXT_ENTRY_CAP)
    if entry_lines:
        parts.append("entry\n" + "\n".join(entry_lines))

    related_lines = pointer_rows(related_symbols, cap=_CONTEXT_RELATED_CAP)
    if related_lines:
        parts.append("related\n" + "\n".join(related_lines))
    elif isinstance(import_neighbors, list) and import_neighbors:
        neighbors = [str(value) for value in import_neighbors[:_CONTEXT_RELATED_CAP] if str(value).strip()]
        if neighbors:
            parts.append("related\n" + "\n".join(f"→ {item}" for item in sorted(neighbors)))

    return "\n\n".join(parts) if parts else "no context"


def _normalize_context_symbols(items: Any, *, fallback: Any) -> list[dict[str, Any]]:
    source_items = items if isinstance(items, list) and items else fallback
    rows = [item for item in source_items if isinstance(item, Mapping)] if isinstance(source_items, list) else []
    normalized: list[dict[str, Any]] = []
    for item in rows:
        kind = str(item.get("kind") or "?").strip().lower()
        if kind in {"import", "export"}:
            continue
        normalized.append(
            {
                "qualified_name": str(item.get("qualified_name") or item.get("symbol_name") or "?"),
                "file_path": str(item.get("file_path") or item.get("path") or "?"),
                "start_line": int(item.get("start_line") or item.get("line") or 0),
                "kind": str(item.get("kind") or "?"),
            }
        )
    normalized.sort(
        key=lambda row: (
            row["file_path"],
            row["start_line"],
            row["qualified_name"],
            row["kind"],
        )
    )
    return _cap_symbols_per_file(normalized, max_per_file=_CONTEXT_PER_FILE_CAP)


def _cap_symbols_per_file(rows: list[dict[str, Any]], *, max_per_file: int) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    out: list[dict[str, Any]] = []
    for row in rows:
        file_path = str(row["file_path"])
        seen = counts.get(file_path, 0)
        if seen >= max_per_file:
            continue
        counts[file_path] = seen + 1
        out.append(row)
    return out


def _normalize_code_blocks(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    rows = [item for item in items if isinstance(item, Mapping)]
    normalized: list[dict[str, Any]] = []
    for item in rows:
        normalized.append(
            {
                "qualified_name": str(item.get("qualified_name") or item.get("symbol_name") or "?"),
                "file_path": str(item.get("file_path") or item.get("path") or "?"),
                "start_line": int(item.get("start_line") or item.get("line") or 0),
                "end_line": int(item.get("end_line") or 0),
                "language": str(item.get("language") or ""),
                "source": str(item.get("source") or "").strip(),
            }
        )
    normalized.sort(
        key=lambda row: (
            row["file_path"],
            row["start_line"],
            row["qualified_name"],
        )
    )
    return normalized


def _render_explore(payload: Mapping[str, Any]) -> str:
    files = payload.get("files")
    if not isinstance(files, list) or not files:
        return _render_explore_items(payload)
    parts: list[str] = []
    for file_entry in files:
        if not isinstance(file_entry, Mapping):
            continue
        file_path = str(file_entry.get("file_path") or file_entry.get("path") or "?")
        sections = file_entry.get("source_sections")
        if not isinstance(sections, list):
            continue
        for section in sections:
            if not isinstance(section, Mapping):
                continue
            content = str(section.get("content") or "").rstrip("\n")
            if not content:
                continue
            # Build the range-tagged header:  #### path:Lstart-Lend — name [kind]
            start_line = int(section.get("start_line") or section.get("line") or 0)
            end_line = int(section.get("end_line") or 0)
            range_tag = ""
            if start_line and end_line:
                range_tag = f":L{start_line}-L{end_line}"
            elif start_line:
                range_tag = f":L{start_line}"
            label = ""
            sym_name = str(section.get("symbol_name") or section.get("name") or section.get("qualified_name") or "")
            sym_kind = str(section.get("kind") or "")
            if sym_name:
                label = f" — {sym_name} [{sym_kind}]" if sym_kind else f" — {sym_name}"
            header = f"#### {file_path}{range_tag}{label}"
            # Skeleton notice and query-match tag are inline on the header.
            if section.get("skeleton"):
                header += " · skeleton"
            if section.get("matched"):
                header += " · match"
            lines: list[str] = [header]
            cleaned = "\n".join(_LINE_NUM_RE.sub("", ln) for ln in content.splitlines())
            lines.append(cleaned)
            parts.append("\n".join(lines))
    if parts:
        rel_lines = _render_explore_relationships(payload.get("relationships"))
        if rel_lines:
            parts.append("\n".join(rel_lines))
    extra = payload.get("additional_relevant_files")
    if isinstance(extra, list) and extra:
        extra_block = ["#### additional_relevant_files"]
        extra_block.extend(f"- {path}" for path in extra[:_CONTEXT_RELATED_CAP])
        parts.append("\n".join(extra_block))
    if not parts:
        return "no results"
    if payload.get("exact_match") is False:
        query = str(payload.get("query") or "")
        note = f'*no exact-name match for "{query}"' if query else "*no exact-name match"
        parts.append(note + " — results are nearest FTS; try _node for direct symbol lookup*")
    return "\n\n".join(parts)


def _render_explore_relationships(relationships: Any) -> list[str]:
    if not isinstance(relationships, Mapping):
        return []
    out: list[str] = []
    for op_name in ("callers", "callees", "usages"):
        groups = relationships.get(op_name)
        if not isinstance(groups, list) or not groups:
            continue
        rows: list[tuple[str, int, str]] = []
        for group in groups:
            if not isinstance(group, Mapping):
                continue
            entries = group.get("references") if op_name == "usages" else group.get("related")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, Mapping):
                    continue
                file_path = str(entry.get("file_path") or entry.get("path") or "?")
                line = int(entry.get("line") or entry.get("start_line") or 0)
                label = str(
                    entry.get("qualified_name")
                    or entry.get("symbol_name")
                    or entry.get("name")
                    or entry.get("caller")
                    or ""
                )
                rows.append((file_path, line, label))
        if rows:
            out.append(f"#### {op_name}")
            out.extend(group_rows_by_file(rows))
    return out


def _render_explore_items(payload: Mapping[str, Any]) -> str:
    items = payload.get("items")
    if not isinstance(items, list) or not items:
        return "no results"
    lines: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        file_path = str(item.get("file_path") or item.get("path") or "?")
        name = str(item.get("qualified_name") or item.get("symbol_name") or "")
        source = str(item.get("source") or "").strip()
        lines.append(f"{file_path} — {name}" if name else file_path)
        if source:
            lines.append(source)
    return "\n".join(lines) if lines else "no results"


def _render_files(payload: Mapping[str, Any]) -> str:
    files = payload.get("files")
    if isinstance(files, list):
        return "\n".join(f"- {entry}" for entry in files) if files else "no files"
    if isinstance(files, Mapping):
        return "\n".join(f"- {path}" for path in sorted(str(key) for key in files)) if files else "no files"
    return "no files"


def _render_routes(payload: Mapping[str, Any]) -> str:
    routes = payload.get("routes")
    if not isinstance(routes, list) or not routes:
        return "no routes"
    lines: list[str] = []
    for route in routes:
        if not isinstance(route, Mapping):
            continue
        method = str(route.get("method") or "?").upper()
        path = str(route.get("path") or route.get("route") or "?")
        handler = str(route.get("handler") or route.get("function") or "")
        file_path = str(route.get("file_path") or "")
        line = int(route.get("line") or route.get("start_line") or 0)
        loc = f" ({file_path}:{line})" if file_path and line > 0 else (f" ({file_path})" if file_path else "")
        handler_part = f" — {handler}" if handler else ""
        lines.append(f"- {method} {path}{handler_part}{loc}")
    return "\n".join(lines) if lines else "no routes"


def render_graph_payload(payload: Mapping[str, Any]) -> str | None:
    """Compact agent view of structural graph analytics.

    The caller retains the complete structured payload. This view omits only
    transport/serving metadata (backend, view revision) while preserving the
    graph facts an agent can act on: affected paths, metrics, factors, tests,
    dependency lists and truncation state.
    """
    kind = str(payload.get("kind") or "").strip()
    if kind == "blast_radius":
        return _render_graph_blast_radius(payload)
    if kind == "dead_code":
        return _render_graph_dead_code(payload)
    if kind == "cycles":
        return _render_graph_cycles(payload)
    if kind == "coupling":
        return _render_graph_coupling(payload)
    if kind == "centrality":
        return _render_graph_centrality(payload)
    if kind == "topology":
        return _render_graph_topology(payload)
    if kind == "pr_risk":
        return _render_graph_pr_risk(payload)
    return None


def _graph_more(lines: list[str], *, total: int | None, shown: int, truncated: bool) -> None:
    if isinstance(total, int) and total > shown:
        lines.append(f"+{total - shown} more")
    elif truncated:
        lines.append("+more")


def _graph_paths(label: str, paths: Any) -> list[str]:
    if not isinstance(paths, list) or not paths:
        return []
    clean = [str(path) for path in paths if str(path).strip()]
    if not clean:
        return []
    return [label, *(f"→ {path}" for path in clean)]


def _render_graph_blast_radius(payload: Mapping[str, Any]) -> str:
    path = str(payload.get("modified_file") or "?")
    risk = str(payload.get("risk_level") or "unknown")
    direct = payload.get("direct_importers")
    transitive = payload.get("transitive_importers")
    tests = payload.get("affected_tests")
    direct_n = len(direct) if isinstance(direct, list) else 0
    transitive_n = len(transitive) if isinstance(transitive, list) else 0
    lines = [f"blast_radius {path} · {risk} · {direct_n + transitive_n} affected"]
    lines.extend(_graph_paths("direct", direct))
    lines.extend(_graph_paths("transitive", transitive))
    lines.extend(_graph_paths("tests", tests))
    return "\n".join(lines)


def _render_graph_dead_code(payload: Mapping[str, Any]) -> str:
    rows = payload.get("dead_files")
    rows = rows if isinstance(rows, list) else []
    total = int(payload.get("dead_file_count") or len(rows))
    analyzed = int(payload.get("analyzed_files") or 0)
    lines = [f"dead_code {total}/{analyzed} files"]
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        path = str(row.get("path") or "?")
        complexity = int(row.get("complexity_score") or 0)
        loc = int(row.get("lines_total") or 0)
        language = str(row.get("language") or "").strip()
        exports = row.get("exports")
        suffix = [f"complexity {complexity}", f"{loc}L"]
        if language:
            suffix.append(language)
        if isinstance(exports, list) and exports:
            suffix.append("exports " + ",".join(str(item) for item in exports))
        lines.append(f"→ {path} · " + " · ".join(suffix))
    _graph_more(lines, total=total, shown=len(rows), truncated=bool(payload.get("truncated")))
    return "\n".join(lines)


def _render_graph_cycles(payload: Mapping[str, Any]) -> str:
    cycles = payload.get("cycles")
    cycles = cycles if isinstance(cycles, list) else []
    total = int(payload.get("cycle_count") or len(cycles))
    analyzed = int(payload.get("analyzed_files") or 0)
    lines = [f"cycles {total} · {analyzed} files"]
    for cycle in cycles:
        if isinstance(cycle, list):
            paths = [str(path) for path in cycle if str(path).strip()]
            if paths:
                lines.append("→ " + " ↔ ".join(paths))
    _graph_more(lines, total=total, shown=len(cycles), truncated=bool(payload.get("truncated")))
    return "\n".join(lines)


def _render_graph_coupling(payload: Mapping[str, Any]) -> str:
    rows = payload.get("files")
    rows = rows if isinstance(rows, list) else []
    total = int(payload.get("coupled_file_count") or len(rows))
    analyzed = int(payload.get("analyzed_files") or 0)
    lines = [f"coupling {total}/{analyzed} files"]
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"→ {row.get('path') or '?'} · in {int(row.get('afferent') or 0)} "
            f"out {int(row.get('efferent') or 0)} · instability {row.get('instability', 0)}"
        )
    _graph_more(lines, total=total, shown=len(rows), truncated=bool(payload.get("truncated")))
    return "\n".join(lines)


def _render_graph_centrality(payload: Mapping[str, Any]) -> str:
    rows = payload.get("ranking")
    rows = rows if isinstance(rows, list) else []
    nodes = int(payload.get("node_count") or len(rows))
    edges = int(payload.get("edge_count") or 0)
    lines = [f"centrality {nodes} nodes · {edges} edges"]
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"→ {row.get('symbol') or '?'} · in {int(row.get('in_degree') or 0)} "
            f"out {int(row.get('out_degree') or 0)} · degree {row.get('degree', 0)} "
            f"· eig {row.get('eigenvector', 0)}"
        )
    _graph_more(lines, total=nodes, shown=len(rows), truncated=bool(payload.get("truncated")))
    return "\n".join(lines)


def _render_graph_topology(payload: Mapping[str, Any]) -> str:
    modules = payload.get("modules")
    modules = modules if isinstance(modules, list) else []
    total = int(payload.get("module_count") or len(modules))
    analyzed = int(payload.get("analyzed_files") or 0)
    lines = [f"topology {total} modules · {analyzed} files"]
    for row in modules:
        if not isinstance(row, Mapping):
            continue
        deps = row.get("depends_on")
        dep_text = ",".join(str(dep) for dep in deps) if isinstance(deps, list) and deps else ""
        suffix = (
            f"{int(row.get('files') or 0)} files · in {int(row.get('afferent_modules') or 0)} "
            f"out {int(row.get('efferent_modules') or 0)}"
        )
        if dep_text:
            suffix += f" → {dep_text}"
        lines.append(f"→ {row.get('module') or '?'} · {suffix}")
    _graph_more(lines, total=total, shown=len(modules), truncated=bool(payload.get("truncated")))
    hotspots = payload.get("hotspots")
    if isinstance(hotspots, list) and hotspots:
        lines.append("hotspots")
        for row in hotspots:
            if not isinstance(row, Mapping):
                continue
            lines.append(
                f"→ {row.get('path') or '?'} · in {int(row.get('afferent') or 0)} "
                f"out {int(row.get('efferent') or 0)} · instability {row.get('instability', 0)}"
            )
    return "\n".join(lines)


def _render_graph_pr_risk(payload: Mapping[str, Any]) -> str:
    rows = payload.get("files")
    rows = rows if isinstance(rows, list) else []
    tier = str(payload.get("overall_tier") or "unknown")
    score = payload.get("overall_score", 0)
    lines = [f"pr_risk {tier} {score} · {int(payload.get('file_count') or len(rows))} files · heuristic"]
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        factors = row.get("factors")
        factors = factors if isinstance(factors, Mapping) else {}
        blast = factors.get("blast_radius")
        blast = blast if isinstance(blast, Mapping) else {}
        churn = factors.get("churn")
        churn = churn if isinstance(churn, Mapping) else {}
        test_gap = factors.get("test_gap")
        test_gap = test_gap if isinstance(test_gap, Mapping) else {}
        complexity = factors.get("complexity")
        complexity = complexity if isinstance(complexity, Mapping) else {}
        missing = bool(test_gap.get("missing_tests"))
        lines.append(
            f"→ {row.get('path') or '?'} · {row.get('tier') or row.get('risk_level') or '?'} {row.get('score', 0)} "
            f"· impact {int(blast.get('impacted_files') or 0)} · churn {int(churn.get('commit_count') or 0)} "
            f"· complexity {int(complexity.get('score') or 0)} · tests {'missing' if missing else 'present'}"
        )
        affected = blast.get("affected_tests")
        if isinstance(affected, list) and affected:
            lines.append("  tests " + ",".join(str(path) for path in affected))
    weights = payload.get("weights")
    if isinstance(weights, Mapping) and weights:
        ordered = ("blast_radius", "churn", "test_gap", "complexity")
        compact = [f"{name} {weights[name]}" for name in ordered if name in weights]
        if compact:
            lines.append("weights " + " · ".join(compact))
    return "\n".join(lines)
