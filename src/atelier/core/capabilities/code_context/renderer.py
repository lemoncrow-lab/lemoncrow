"""Compact benchmark-facing renderers for code context payloads."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

_CONTEXT_ENTRY_CAP = 8
_CONTEXT_RELATED_CAP = 10
_CONTEXT_CODE_BLOCK_CAP = 3
_CONTEXT_PER_FILE_CAP = 3


def render_code_payload(op: str, payload: Mapping[str, Any]) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    if payload.get("error"):
        return None
    if op == "search":
        return _render_search(payload)
    if op == "symbol":
        return _render_symbol(payload)
    if op == "outline":
        return _render_outline(payload)
    if op in {"callers", "callees", "usages"}:
        return _render_relations(op, payload)
    if op == "impact":
        return _render_impact(payload)
    if op == "status":
        return _render_status(payload)
    if op == "index":
        return _render_index(payload)
    if op == "cache_status":
        return _render_cache_status(payload)
    if op == "context":
        return _render_context(payload)
    return None


def _render_search(payload: Mapping[str, Any]) -> str:
    items = payload.get("items")
    if not isinstance(items, list):
        return "### search\n- no matches"
    rows: list[str] = []
    sorted_items = sorted(
        (item for item in items if isinstance(item, Mapping)),
        key=lambda item: (
            str(item.get("file_path") or ""),
            int(item.get("start_line") or 0),
            str(item.get("qualified_name") or item.get("symbol_name") or ""),
        ),
    )
    for item in sorted_items:
        file_path = str(item.get("file_path") or "?")
        line = int(item.get("start_line") or 0)
        name = str(item.get("qualified_name") or item.get("symbol_name") or "?")
        kind = str(item.get("kind") or "?")
        if line > 0:
            rows.append(f"- {file_path}:{line} — {name} [{kind}]")
        else:
            rows.append(f"- {file_path} — {name} [{kind}]")
    if not rows:
        rows.append("- no matches")
    return "\n".join(["### search", *rows])


def _render_symbol(payload: Mapping[str, Any]) -> str:
    symbol_id = str(payload.get("symbol_id") or "").strip()
    file_path = str(payload.get("file_path") or "?")
    start_line = int(payload.get("start_line") or 0)
    end_line = int(payload.get("end_line") or 0)
    symbol = str(payload.get("qualified_name") or payload.get("symbol_name") or symbol_id or "?")
    kind = str(payload.get("kind") or "?")
    signature = str(payload.get("signature") or "").strip()
    lines = ["### symbol", f"- {symbol} [{kind}]"]
    if symbol_id:
        lines.append(f"- id: {symbol_id}")
    if start_line > 0 and end_line >= start_line:
        lines.append(f"- location: {file_path}:{start_line}-{end_line}")
    else:
        lines.append(f"- location: {file_path}")
    if signature:
        lines.append(f"- signature: {signature}")
    return "\n".join(lines)


def _render_outline(payload: Mapping[str, Any]) -> str:
    files = payload.get("files")
    if not isinstance(files, Mapping):
        return "### outline\n- no symbols"
    lines = ["### outline"]
    for file_path in sorted(str(key) for key in files):
        entries = files.get(file_path)
        if not isinstance(entries, list):
            continue
        lines.append(f"- {file_path}")
        normalized = sorted(
            (item for item in entries if isinstance(item, Mapping)),
            key=lambda item: (
                int(item.get("line_start") or 0),
                str(item.get("qualified_name") or item.get("name") or ""),
                str(item.get("kind") or ""),
            ),
        )
        for item in normalized:
            name = str(item.get("qualified_name") or item.get("name") or "?")
            kind = str(item.get("kind") or "?")
            line = int(item.get("line_start") or 0)
            end_line = int(item.get("line_end") or 0)
            signature = str(item.get("signature") or "").strip()
            line_label = f"{line}"
            if line > 0 and end_line > line:
                line_label = f"{line}-{end_line}"
            summary = f"  - {line_label}: {name} [{kind}]"
            if signature:
                summary = f"{summary} — {signature}"
            lines.append(summary)
    if len(lines) == 1:
        lines.append("- no symbols")
    return "\n".join(lines)


def _render_relations(op: str, payload: Mapping[str, Any]) -> str:
    target = payload.get("target")
    target_name = "?"
    target_loc = ""
    if isinstance(target, Mapping):
        target_name = str(target.get("qualified_name") or target.get("symbol_name") or "?")
        t_file = str(target.get("file_path") or "")
        t_line = int(target.get("start_line") or 0)
        if t_file and t_line > 0:
            target_loc = f" ({t_file}:{t_line})"
    lines = [f"### {op}", f"- target: {target_name}{target_loc}"]
    if op == "usages":
        refs = _flatten_usages(payload.get("references"))
        for ref in refs:
            file_path = str(ref.get("file_path") or "?")
            line = int(ref.get("line") or 0)
            caller = str(ref.get("caller") or ref.get("enclosing_qualified_name") or "").strip()
            if caller:
                lines.append(f"- {file_path}:{line} — {caller}")
            else:
                lines.append(f"- {file_path}:{line}")
        if len(lines) == 2:
            lines.append("- no references")
        return "\n".join(lines)

    related = payload.get("related")
    if isinstance(related, list):
        rows = sorted(
            (item for item in related if isinstance(item, Mapping)),
            key=lambda item: (
                str(item.get("file_path") or ""),
                int(item.get("start_line") or 0),
                str(item.get("qualified_name") or item.get("symbol_name") or ""),
            ),
        )
        for item in rows:
            name = str(item.get("qualified_name") or item.get("symbol_name") or "?")
            file_path = str(item.get("file_path") or "?")
            line = int(item.get("start_line") or 0)
            if line > 0:
                lines.append(f"- {file_path}:{line} — {name}")
            else:
                lines.append(f"- {file_path} — {name}")
    if len(lines) == 2:
        lines.append("- no related symbols")
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
        key=lambda item: (str(item.get("file_path") or ""), int(item.get("line") or 0), int(item.get("column") or 0)),
    )


def _render_impact(payload: Mapping[str, Any]) -> str:
    target = payload.get("target")
    target_label = str(payload.get("file_path") or "?")
    if isinstance(target, Mapping):
        target_type = str(target.get("type") or payload.get("target_type") or "file")
        if target_type == "symbol":
            matches = target.get("matches")
            first_match: Mapping[str, Any] | None = None
            if isinstance(matches, list):
                for item in matches:
                    if isinstance(item, Mapping):
                        first_match = item
                        break
            if first_match is not None:
                symbol = str(first_match.get("qualified_name") or first_match.get("symbol_name") or "?")
                file_path = str(first_match.get("file_path") or "?")
                line = int(first_match.get("start_line") or 0)
                target_label = f"symbol {symbol} @ {file_path}:{line}" if line > 0 else f"symbol {symbol} @ {file_path}"
            else:
                target_label = f"symbol {(target.get('query') or '?')!s}"
        else:
            target_label = str(target.get("path") or payload.get("file_path") or "?")
    lines = ["### impact", f"- target: {target_label}"]
    lines.append(f"- risk: {(payload.get('risk_level') or 'unknown')!s}")
    grouped: dict[str, list[str]] = defaultdict(list)
    for label, key in (
        ("direct", "direct_importers"),
        ("transitive", "transitive_importers"),
        ("tests", "affected_tests"),
    ):
        values = payload.get(key)
        if isinstance(values, list):
            grouped[label] = sorted(str(value) for value in values)
    for label in ("direct", "transitive", "tests"):
        values = grouped.get(label, [])
        lines.append(f"- {label}: {len(values)}")
        for value in values:
            lines.append(f"  - {value}")
    affected_files = payload.get("affected_files")
    if isinstance(affected_files, list):
        rows = sorted(
            (item for item in affected_files if isinstance(item, Mapping)),
            key=lambda item: str(item.get("file_path") or ""),
        )
        lines.append(f"- affected_files: {len(rows)}")
        for item in rows:
            file_path = str(item.get("file_path") or "?")
            reasons = item.get("reasons")
            reason_text = ""
            if isinstance(reasons, list):
                reason_text = f" ({', '.join(sorted(str(reason) for reason in reasons))})"
            symbols = item.get("symbols")
            symbol_text = ""
            if isinstance(symbols, list) and symbols:
                rendered = ", ".join(sorted(str(symbol) for symbol in symbols)[:3])
                symbol_text = f" [{rendered}]"
            lines.append(f"  - {file_path}{reason_text}{symbol_text}")
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
        "### status",
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
    return "\n".join(
        [
            "### index",
            f"- repo: {(payload.get('repo_id') or '?')!s}",
            f"- version: {index_version}",
            f"- counts: files={files_indexed}, symbols={symbols_indexed}, imports={imports_indexed}",
        ]
    )


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
        "### cache_status",
        f"- repo: {(payload.get('repo_id') or '?')!s}",
        f"- index_version: {index_version}",
        f"- entries: {entry_count}",
        f"- bytes: {total_bytes}/{max_bytes}",
    ]
    if tool_summary:
        lines.append(f"- tools: {tool_summary}")
    return "\n".join(lines)


def _render_context(payload: Mapping[str, Any]) -> str:
    task = str(payload.get("task") or "")
    budget_tokens = int(payload.get("budget_tokens") or 0)
    token_count = int(payload.get("token_count") or 0)
    lines = ["### context", f"- task: {task}", f"- budget: {budget_tokens}", f"- packed_tokens: {token_count}"]
    entry_points = _normalize_context_symbols(payload.get("entry_points"), fallback=payload.get("symbols"))
    related_symbols = _normalize_context_symbols(payload.get("related_symbols"), fallback=[])
    code_blocks = _normalize_code_blocks(payload.get("code_blocks"))
    import_neighbors = payload.get("import_neighbors")

    lines.append("#### entry_points")
    if entry_points:
        for row in entry_points[:_CONTEXT_ENTRY_CAP]:
            lines.append(
                f"- {row['file_path']}:{row['start_line']} — {row['qualified_name']} [{row['kind']}]"
            )
    else:
        lines.append("- none")

    lines.append("#### related_symbols")
    if related_symbols:
        for row in related_symbols[:_CONTEXT_RELATED_CAP]:
            lines.append(
                f"- {row['file_path']}:{row['start_line']} — {row['qualified_name']} [{row['kind']}]"
            )
    elif isinstance(import_neighbors, list) and import_neighbors:
        for item in sorted(str(value) for value in import_neighbors[:_CONTEXT_RELATED_CAP]):
            lines.append(f"- {item}")
    else:
        lines.append("- none")

    lines.append("#### code_blocks")
    if code_blocks:
        for block in code_blocks[:_CONTEXT_CODE_BLOCK_CAP]:
            language = block["language"]
            lines.append(
                f"- {block['qualified_name']} ({block['file_path']}:{block['start_line']}-{block['end_line']})"
            )
            lines.append(f"```{language}")
            lines.append(str(block["source"]).rstrip())
            lines.append("```")
    else:
        lines.append("- none")
    return "\n".join(lines)


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
                "file_path": str(item.get("file_path") or "?"),
                "start_line": int(item.get("start_line") or 0),
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
                "file_path": str(item.get("file_path") or "?"),
                "start_line": int(item.get("start_line") or 0),
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
