"""Read-only code-map queries for the local product UI.

The full map exposes indexed symbol metadata, tracked file metadata, and only
uniquely resolved call relationships. Exact source crosses the API boundary
only when a user explicitly opens one symbol. Live activity is projected from
the existing run ledger; raw tool output, diffs, stdout, and stderr stay local.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.paths import default_store_root, is_recognized_workspace, workspace_key

_MAX_SEARCH_RESULTS = 40
_MAX_GRAPH_NODES = 160
_MAX_GRAPH_DEPTH = 2
_MAX_SOURCE_CHARS = 20_000
_MAX_FULL_GRAPH_SYMBOLS = 50_000
_MAX_FULL_GRAPH_FILES = 20_000
_COMMUNITY_PALETTE = (
    "#60a5fa",
    "#f59e0b",
    "#f87171",
    "#67e8f9",
    "#86efac",
    "#fde047",
    "#c084fc",
    "#fb7185",
    "#a78bfa",
    "#2dd4bf",
    "#f97316",
    "#84cc16",
)
_FILE_TYPE_COLORS = {
    "source": "#67e8f9",
    "test": "#a78bfa",
    "docs": "#fbbf24",
    "config": "#86efac",
    "data": "#fb7185",
    "asset": "#94a3b8",
    "other": "#737373",
}
_LANGUAGE_BY_SUFFIX = {
    ".astro": "Astro",
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".go": "Go",
    ".h": "C/C++ header",
    ".hpp": "C++ header",
    ".html": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript JSX",
    ".kt": "Kotlin",
    ".lua": "Lua",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".sh": "Shell",
    ".sql": "SQL",
    ".svelte": "Svelte",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript JSX",
    ".vue": "Vue",
}
_DOC_SUFFIXES = {".md", ".mdx", ".rst", ".txt", ".adoc"}
_CONFIG_SUFFIXES = {".json", ".jsonc", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".xml", ".env", ".lock"}
_DATA_SUFFIXES = {".csv", ".tsv", ".parquet", ".sqlite", ".sqlite3", ".db", ".jsonl"}
_ASSET_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".ttf",
    ".pdf",
    ".zip",
    ".gz",
}
_FULL_GRAPH_CACHE: dict[tuple[str, int, int, int, int], dict[str, Any]] = {}
_COMMON_SEED_NAMES = {
    "bool",
    "dict",
    "float",
    "get",
    "int",
    "isinstance",
    "len",
    "list",
    "max",
    "min",
    "open",
    "print",
    "query",
    "read",
    "run",
    "set",
    "str",
    "super",
    "tuple",
    "write",
}
_VERIFY_COMMAND_RE = re.compile(
    r"(?:^|\s)(?:pytest|vitest|jest|ruff|mypy|pyright|eslint|cargo\s+test|go\s+test|npm\s+(?:run\s+)?(?:test|build|lint|typecheck)|pnpm\s+(?:test|build|lint|typecheck)|yarn\s+(?:test|build|lint|typecheck)|uv\s+run\s+pytest)(?:\s|$)",
    re.IGNORECASE,
)


def _git_root(candidate: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return Path(result.stdout.strip()).resolve()


def resolve_project_root(requested: str | Path | None = None) -> Path:
    """Resolve one exact, recognized workspace root for map queries."""

    if requested:
        candidate = Path(requested).expanduser().resolve()
    else:
        configured = os.environ.get("LEMONCROW_WORKSPACE_ROOT", "").strip()
        if configured:
            candidate = Path(configured).expanduser().resolve()
        else:
            from lemoncrow.core.service.code_warm import discover_workspaces

            active = [path for path in discover_workspaces() if not _is_ephemeral(path)]
            candidate = active[0] if active else (_git_root(Path.cwd()) or Path.cwd().resolve())

    if not candidate.is_dir():
        raise ValueError(f"project root does not exist: {candidate}")
    git_root = _git_root(candidate)
    marker_root = candidate if (candidate / ".lemoncrow").is_dir() else None
    if git_root != candidate and marker_root != candidate:
        if not is_recognized_workspace(candidate):
            raise ValueError(f"project root is not a registered workspace: {candidate}")
        raise ValueError(f"project root must point at the repository root: {candidate}")
    return candidate


def _is_ephemeral(path: Path) -> bool:
    resolved = path.resolve()
    return any(
        root == resolved or root in resolved.parents for root in (Path("/tmp"), Path("/var/tmp"), Path("/dev/shm"))
    )


def list_projects() -> list[dict[str, Any]]:
    """List active, non-ephemeral workspaces plus the configured workspace."""

    candidates: list[Path] = []
    configured = os.environ.get("LEMONCROW_WORKSPACE_ROOT", "").strip()
    if configured:
        with contextlib.suppress(OSError):
            candidates.append(resolve_project_root(configured))
    from lemoncrow.core.service.code_warm import discover_workspaces

    active_workspaces = discover_workspaces()
    active_set = {path.resolve() for path in active_workspaces}
    candidates.extend(path for path in active_workspaces if not _is_ephemeral(path))
    if not candidates:
        with contextlib.suppress(ValueError):
            candidates.append(resolve_project_root())

    seen: set[Path] = set()
    projects: list[dict[str, Any]] = []
    for candidate in candidates:
        root = candidate.resolve()
        if root in seen:
            continue
        seen.add(root)
        db_path = default_store_root() / "workspaces" / workspace_key(root) / "code_context.sqlite"
        projects.append(
            {
                "root": str(root),
                "label": root.name,
                "indexed": db_path.is_file(),
                "active": root in active_set,
            }
        )
    return projects


@functools.lru_cache(maxsize=8)
def get_engine(project_root: str) -> Any:
    """Keep a small read-only engine set warm for the UI process."""

    from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

    return CodeContextEngine(project_root, autosync_enabled=False)


def _record_node(record: Any) -> dict[str, Any]:
    return {
        "id": str(record.symbol_id),
        "label": str(record.symbol_name),
        "qualified_name": str(record.qualified_name),
        "path": str(record.file_path),
        "kind": str(record.kind),
        "language": str(record.language),
        "line": int(record.start_line),
        "end_line": int(record.end_line),
        "score": round(float(record.score), 4) if record.score is not None else None,
    }


def _payload_node(payload: dict[str, Any], *, focus: bool = False) -> dict[str, Any]:
    return {
        "id": str(payload.get("id") or payload.get("symbol_id") or ""),
        "label": str(payload.get("name") or payload.get("symbol_name") or "unknown"),
        "qualified_name": str(payload.get("qualified_name") or payload.get("name") or "unknown"),
        "path": str(payload.get("path") or payload.get("file_path") or ""),
        "kind": str(payload.get("kind") or "symbol"),
        "language": str(payload.get("language") or ""),
        "line": int(payload.get("line") or payload.get("start_line") or 0),
        "end_line": int(payload.get("end_line") or payload.get("line") or payload.get("start_line") or 0),
        "provenance": str(payload.get("provenance") or "local"),
        "focus": focus,
    }


def _file_node_id(path: str) -> str:
    return "file::" + hashlib.sha1(path.encode()).hexdigest()[:20]


def _community_for_path(path: str) -> str:
    parts = Path(path).parts
    if len(parts) <= 1:
        return "root"
    if len(parts) >= 4 and parts[0] == "src" and parts[1] == "lemoncrow":
        return "/".join(parts[:3])
    if len(parts) >= 3 and parts[0] in {"tests", "frontend", "landing", "services", "integrations", "benchmarks"}:
        return "/".join(parts[:2])
    return parts[0]


def _file_metadata(path: str) -> tuple[str, str]:
    normalized = path.lower()
    suffix = Path(normalized).suffix
    name = Path(normalized).name
    language = _LANGUAGE_BY_SUFFIX.get(suffix, "")
    if suffix in _DOC_SUFFIXES:
        return "docs", "Markdown" if suffix in {".md", ".mdx"} else "Documentation"
    if (
        {"test", "tests"} & set(Path(normalized).parts)
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
    ):
        return "test", language or "Test"
    if language:
        return "source", language
    if suffix in _CONFIG_SUFFIXES or name in {"dockerfile", "makefile", "procfile"}:
        return "config", suffix.removeprefix(".").upper() or "Configuration"
    if suffix in _DATA_SUFFIXES:
        return "data", suffix.removeprefix(".").upper() or "Data"
    if suffix in _ASSET_SUFFIXES:
        return "asset", suffix.removeprefix(".").upper() or "Asset"
    return "other", suffix.removeprefix(".").upper() or "Other"


def _tracked_files(project_root: Path) -> list[str]:
    # ``--recurse-submodules`` surfaces tracked files inside submodules, but git
    # rejects it combined with ``--others``, so untracked files come from a
    # separate top-level-only call.
    paths: list[str] = []
    for extra_args in (
        ("--cached", "--recurse-submodules"),
        ("--others", "--exclude-standard"),
    ):
        try:
            result = subprocess.run(
                ["git", "-C", str(project_root), "ls-files", "-z", *extra_args],
                capture_output=True,
                check=False,
                timeout=8,
            )
        except (OSError, subprocess.SubprocessError):
            return []
        if result.returncode != 0:
            return []
        paths.extend(
            path
            for raw in result.stdout.split(b"\0")
            if raw and (path := raw.decode("utf-8", errors="surrogateescape"))
        )
    return sorted(paths)


def search_symbols(engine: Any, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
    clean_query = query.strip()
    if not clean_query:
        return []
    bounded_limit = max(1, min(limit, _MAX_SEARCH_RESULTS))
    records = engine.search_symbols(
        clean_query,
        limit=bounded_limit,
        mode="lexical",
        snippet="none",
        auto_index=False,
    )
    return [_record_node(record) for record in records]


def build_neighborhood(engine: Any, symbol_id: str, *, depth: int = 1, limit: int = 80) -> dict[str, Any]:
    """Merge bounded caller and callee traversals around one symbol."""

    bounded_depth = max(1, min(depth, _MAX_GRAPH_DEPTH))
    bounded_limit = max(4, min(limit, _MAX_GRAPH_NODES))
    per_direction = max(2, bounded_limit // 2)
    callers = engine.tool_callers(
        symbol_id=symbol_id,
        depth=bounded_depth,
        limit=per_direction,
        auto_index=False,
        budget_tokens=16_000,
    )
    callees = engine.tool_callees(
        symbol_id=symbol_id,
        depth=bounded_depth,
        limit=per_direction,
        auto_index=False,
        budget_tokens=16_000,
    )
    target = callers.get("target") or callees.get("target")
    if not isinstance(target, dict) or not target.get("id"):
        raise LookupError(f"symbol not found: {symbol_id}")

    nodes: dict[str, dict[str, Any]] = {}
    focus_node = _payload_node(target, focus=True)
    nodes[focus_node["id"]] = focus_node
    for payload in [*(callers.get("related") or []), *(callees.get("related") or [])]:
        if not isinstance(payload, dict):
            continue
        node = _payload_node(payload)
        if node["id"] and node["id"] not in nodes and len(nodes) < bounded_limit:
            nodes[node["id"]] = node

    edges: dict[tuple[str, str], dict[str, Any]] = {}
    for payload in [*(callers.get("edges") or []), *(callees.get("edges") or [])]:
        if not isinstance(payload, dict):
            continue
        source = str(payload.get("caller_symbol_id") or "")
        target_id = str(payload.get("callee_symbol_id") or "")
        if not source or not target_id or source not in nodes or target_id not in nodes:
            continue
        edges[(source, target_id)] = {
            "id": f"{source}->{target_id}",
            "source": source,
            "target": target_id,
            "kind": "calls",
            "depth": int(payload.get("depth") or 1),
        }

    degrees: dict[str, int] = {node_id: 0 for node_id in nodes}
    for edge in edges.values():
        degrees[edge["source"]] += 1
        degrees[edge["target"]] += 1
    for node_id, node in nodes.items():
        node["degree"] = degrees[node_id]

    return {
        "repo_id": str(getattr(engine, "repo_id", "")),
        "focus": focus_node["id"],
        "nodes": list(nodes.values()),
        "edges": list(edges.values()),
        "truncated": bool(callers.get("truncated") or callees.get("truncated")),
        "depth": bounded_depth,
    }


def build_full_graph(
    engine: Any,
    project_root: Path,
    *,
    max_symbols: int = _MAX_FULL_GRAPH_SYMBOLS,
    max_files: int = _MAX_FULL_GRAPH_FILES,
) -> dict[str, Any]:
    """Build a full-repository metadata graph without returning source bodies."""

    bounded_symbols = max(1, min(max_symbols, _MAX_FULL_GRAPH_SYMBOLS))
    bounded_files = max(1, min(max_files, _MAX_FULL_GRAPH_FILES))
    db_path = Path(str(getattr(engine, "db_path", "")))
    db_stamp = db_path.stat().st_mtime_ns if db_path.is_file() else 0
    tracked_files = _tracked_files(project_root)
    tracked_stamp = hash("\0".join(tracked_files))
    cache_key = (str(project_root.resolve()), db_stamp, tracked_stamp, bounded_symbols, bounded_files)
    cached = _FULL_GRAPH_CACHE.get(cache_key)
    if cached is not None:
        return cached

    conn = engine.connection()
    total_symbols = int(conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0])
    symbol_rows = conn.execute(
        """
        SELECT symbol_id, file_path, language, symbol_name, qualified_name,
               kind, start_line, end_line
        FROM symbols
        ORDER BY file_path, start_line, end_line
        LIMIT ?
        """,
        (bounded_symbols,),
    ).fetchall()

    indexed_paths = {str(row["file_path"]) for row in symbol_rows}
    all_files = sorted(set(tracked_files) | indexed_paths)
    total_files = len(all_files)
    selected_files = all_files[:bounded_files]
    selected_file_set = set(selected_files)
    symbol_rows = [row for row in symbol_rows if str(row["file_path"]) in selected_file_set]

    community_keys = sorted({_community_for_path(path) for path in selected_files})
    community_colors = {
        key: _COMMUNITY_PALETTE[index % len(_COMMUNITY_PALETTE)] for index, key in enumerate(community_keys)
    }
    nodes: dict[str, dict[str, Any]] = {}
    file_type_counts: dict[str, int] = {}
    language_counts: dict[str, int] = {}
    max_end_by_file: dict[str, int] = {}
    for row in symbol_rows:
        path = str(row["file_path"])
        max_end_by_file[path] = max(max_end_by_file.get(path, 1), int(row["end_line"] or 1))

    for path in selected_files:
        file_type, language = _file_metadata(path)
        community = _community_for_path(path)
        file_type_counts[file_type] = file_type_counts.get(file_type, 0) + 1
        language_counts[language] = language_counts.get(language, 0) + 1
        node_id = _file_node_id(path)
        nodes[node_id] = {
            "id": node_id,
            "label": Path(path).name,
            "qualified_name": path,
            "path": path,
            "kind": file_type,
            "language": language,
            "file_type": file_type,
            "node_type": "file",
            "line": 1,
            "end_line": max_end_by_file.get(path, 1),
            "community": community,
            "color": _FILE_TYPE_COLORS[file_type],
            "degree": 0,
        }

    symbols_by_id: dict[str, dict[str, Any]] = {}
    by_location: dict[tuple[str, str, int], str] = {}
    by_file_name: dict[tuple[str, str], list[str]] = {}
    by_qualified: dict[str, list[str]] = {}
    by_name: dict[str, list[str]] = {}
    for row in symbol_rows:
        node_id = str(row["symbol_id"])
        path = str(row["file_path"])
        file_type, inferred_language = _file_metadata(path)
        community = _community_for_path(path)
        node: dict[str, Any] = {
            "id": node_id,
            "label": str(row["symbol_name"]),
            "qualified_name": str(row["qualified_name"]),
            "path": path,
            "kind": str(row["kind"]),
            "language": str(inferred_language or row["language"] or "Other"),
            "file_type": file_type,
            "node_type": "symbol",
            "line": int(row["start_line"]),
            "end_line": int(row["end_line"]),
            "community": community,
            "color": community_colors[community],
            "degree": 0,
        }
        nodes[node_id] = node
        symbols_by_id[node_id] = node
        by_location[(path, node["qualified_name"], node["line"])] = node_id
        by_file_name.setdefault((path, node["label"]), []).append(node_id)
        by_qualified.setdefault(node["qualified_name"], []).append(node_id)
        by_name.setdefault(node["label"], []).append(node_id)

    edges: dict[tuple[str, str, str], dict[str, Any]] = {}
    for node_id, node in symbols_by_id.items():
        file_id = _file_node_id(node["path"])
        if file_id in nodes:
            edges[(file_id, node_id, "contains")] = {
                "id": f"contains::{file_id}::{node_id}",
                "source": file_id,
                "target": node_id,
                "kind": "contains",
                "depth": 0,
                "weight": 1,
            }

    def unique(candidates: list[str]) -> str | None:
        distinct = list(dict.fromkeys(candidates))
        return distinct[0] if len(distinct) == 1 else None

    with contextlib.suppress(sqlite3.Error):
        call_rows = conn.execute("""
            SELECT caller_symbol_name, caller_qualified_name, caller_file_path,
                   caller_start_line, callee_name, callee_short_name
            FROM call_edges
            ORDER BY caller_file_path, caller_start_line
            """).fetchall()
        for row in call_rows:
            caller_path = str(row["caller_file_path"] or "")
            caller_qualified = str(row["caller_qualified_name"] or "")
            caller_name = str(row["caller_symbol_name"] or "")
            caller_line = int(row["caller_start_line"] or 0)
            source = by_location.get((caller_path, caller_qualified, caller_line))
            if source is None:
                source = unique(by_file_name.get((caller_path, caller_name), []))
            if source is None:
                continue

            callee_name = str(row["callee_name"] or "")
            callee_short = str(row["callee_short_name"] or callee_name.rsplit(".", 1)[-1])
            target = unique(by_qualified.get(callee_name, []))
            if target is None:
                target = unique(by_file_name.get((caller_path, callee_short), []))
            if target is None:
                candidates = by_name.get(callee_short, [])
                if len(candidates) > 1:
                    source_community = symbols_by_id[source]["community"]
                    candidates = [
                        candidate
                        for candidate in candidates
                        if symbols_by_id[candidate]["community"] == source_community
                    ]
                target = unique(candidates)
            if target is None:
                continue

            key = (source, target, "calls")
            existing = edges.get(key)
            if existing is not None:
                existing["weight"] += 1
                continue
            edge_hash = hashlib.sha1(f"{source}:{target}".encode()).hexdigest()[:16]
            edges[key] = {
                "id": f"call::{edge_hash}",
                "source": source,
                "target": target,
                "kind": "calls",
                "depth": 1,
                "weight": 1,
            }

    for edge in edges.values():
        nodes[edge["source"]]["degree"] += 1
        nodes[edge["target"]]["degree"] += 1
    focus = max(symbols_by_id, key=lambda node_id: nodes[node_id]["degree"], default=None)

    community_counts: dict[str, int] = {key: 0 for key in community_keys}
    for node in nodes.values():
        community_counts[node["community"]] += 1
    status = engine.tool_status(auto_index=False, budget_tokens=4_000)
    index = status.get("index") if isinstance(status.get("index"), dict) else {}
    truncated = total_symbols > len(symbol_rows) or total_files > len(selected_files)
    payload = {
        "project": {"root": str(project_root), "label": project_root.name},
        "index": index,
        "total_symbols": total_symbols,
        "total_files": total_files,
        "truncated": truncated,
        "communities": [
            {
                "id": key,
                "label": key,
                "color": community_colors[key],
                "count": community_counts[key],
            }
            for key in community_keys
        ],
        "file_types": [
            {"id": key, "label": key.title(), "color": _FILE_TYPE_COLORS[key], "count": count}
            for key, count in sorted(file_type_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "languages": [
            {"id": key, "label": key, "count": count}
            for key, count in sorted(language_counts.items(), key=lambda item: (-item[1], item[0]))
        ],
        "graph": {
            "repo_id": str(getattr(engine, "repo_id", "")),
            "focus": focus,
            "nodes": list(nodes.values()),
            "edges": list(edges.values()),
            "truncated": truncated,
            "depth": 0,
        },
    }
    _FULL_GRAPH_CACHE.clear()
    _FULL_GRAPH_CACHE[cache_key] = payload
    return payload


def _default_seed_symbol_id(engine: Any) -> str | None:
    """Pick a connected, navigable symbol without returning a full graph."""

    try:
        centrality = engine.call_graph_centrality(limit=200)
        ranking = list(centrality.get("ranking", []))
        # Eigenvector order is useful to ranking, but unresolved builtins and
        # library helpers dominate its head.  For the first visual cluster,
        # prefer a navigable symbol that both receives and makes calls, then
        # rank by total degree.  This usually lands on the repository's main
        # dispatcher/service boundary rather than ``str``/``len``/``read``.
        connected = sorted(
            (
                row
                for row in ranking
                if int(row.get("out_degree") or 0) >= 2
                and str(row.get("symbol") or "").rsplit(".", 1)[-1].lower() not in _COMMON_SEED_NAMES
            ),
            key=lambda row: int(row.get("in_degree") or 0) + int(row.get("out_degree") or 0),
            reverse=True,
        )
        for row in [*connected, *ranking]:
            name = str(row.get("symbol") or "").rsplit(".", 1)[-1]
            if not name or name.lower() in _COMMON_SEED_NAMES:
                continue
            matches = search_symbols(engine, name, limit=5)
            exact = next((match for match in matches if match["label"] == name), None)
            if exact:
                return str(exact["id"])
    except (OSError, LookupError, ValueError, sqlite3.Error):
        pass
    return None


def build_overview(engine: Any, project_root: Path) -> dict[str, Any]:
    status = engine.tool_status(auto_index=False, budget_tokens=4_000)
    index = status.get("index") if isinstance(status.get("index"), dict) else {}
    seed = _default_seed_symbol_id(engine) if int(index.get("symbols_indexed") or 0) else None
    graph = (
        build_neighborhood(engine, seed, depth=1, limit=80)
        if seed
        else {
            "repo_id": str(getattr(engine, "repo_id", "")),
            "focus": None,
            "nodes": [],
            "edges": [],
            "truncated": False,
            "depth": 1,
        }
    )
    return {
        "project": {"root": str(project_root), "label": project_root.name},
        "index": index,
        "graph": graph,
    }


def symbol_detail(engine: Any, symbol_id: str) -> dict[str, Any]:
    payload = engine.get_symbol(symbol_id=symbol_id, auto_index=False)
    source = str(payload.pop("source", ""))
    truncated = len(source) > _MAX_SOURCE_CHARS
    if truncated:
        source = source[:_MAX_SOURCE_CHARS] + "\n… source truncated"
    node = _payload_node(payload)
    return {**node, "signature": str(payload.get("signature") or ""), "source": source, "source_truncated": truncated}


def _parse_at(value: Any) -> datetime | None:
    if not value:
        return None
    with contextlib.suppress(TypeError, ValueError):
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _event_path(payload: dict[str, Any]) -> str | None:
    args_value = payload.get("args")
    args: dict[str, Any] = args_value if isinstance(args_value, dict) else {}
    for key in ("path", "file_path", "target_path"):
        value = payload.get(key) or args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("files", "edits"):
        values = args.get(key)
        if not isinstance(values, list):
            continue
        for value in values:
            candidate = value.get("path") if isinstance(value, dict) else value
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    return None


def _event_path_line(value: str | None) -> int | None:
    if not value:
        return None
    match = re.search(r":L?(\d+)(?:-L?\d+)?$", value)
    return int(match.group(1)) if match else None


def _strip_event_range(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r":(?:L?\d+(?:-L?\d+)?|full|outline|summary|head=\d+|tail=\d+)$", "", value)


def _relative_event_path(value: str | None, project_root: Path) -> str | None:
    if not value:
        return None
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        return candidate.as_posix()
    with contextlib.suppress(OSError, ValueError):
        return candidate.resolve().relative_to(project_root).as_posix()
    return None


def classify_activity_event(
    event: dict[str, Any],
    *,
    session_id: str,
    sequence: int,
    project_root: Path | None = None,
) -> dict[str, Any] | None:
    """Project a ledger event into the privacy-bounded map activity schema."""

    event_kind = str(event.get("kind") or "")
    payload_value = event.get("payload")
    payload: dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
    args_value = payload.get("args")
    args: dict[str, Any] = args_value if isinstance(args_value, dict) else {}
    tool = str(payload.get("tool") or "").lower()
    summary = str(event.get("summary") or "")
    activity_kind: str | None = None
    query: str | None = None
    raw_path = _event_path(payload)
    line = _event_path_line(raw_path)
    path = _strip_event_range(raw_path)
    status: str | None = None
    label = ""

    if event_kind == "file_edit" or "edit" in tool or "write" in tool:
        activity_kind = "edit"
        label = f"Edited {Path(path).name}" if path else "Edited code"
    elif event_kind == "test_result":
        activity_kind = "verify"
        passed = bool(payload.get("passed"))
        status = "passed" if passed else "failed"
        label = f"Verification {status}"
    elif event_kind == "command_result" or tool.endswith("bash") or tool.endswith("exec_command"):
        command = str(payload.get("command") or args.get("command") or summary)
        if _VERIFY_COMMAND_RE.search(command):
            activity_kind = "verify"
            ok = bool(payload.get("ok", payload.get("return_code") in (None, 0)))
            status = "passed" if ok else "failed"
            label = f"Verification {status}"
    elif any(marker in tool for marker in ("code_search", "explore", "callers", "callees", "usages")):
        activity_kind = "search"
        raw_query = args.get("query") or args.get("symbol_name") or args.get("symbol_id")
        query = str(raw_query).strip()[:160] if raw_query else None
        label = f"Searched {query}" if query else "Searched the code graph"
    elif tool.endswith("read") or event_kind == "file_read":
        activity_kind = "read"
        label = f"Read {Path(path).name}" if path else "Read exact source"

    if activity_kind is None:
        return None
    if project_root is not None:
        path = _relative_event_path(path, project_root)
    event_id = hashlib.sha1(f"{session_id}:{event.get('at')}:{sequence}:{activity_kind}".encode()).hexdigest()[:16]
    result: dict[str, Any] = {
        "id": event_id,
        "session_id": session_id,
        "kind": activity_kind,
        "at": str(event.get("at") or ""),
        "label": label,
    }
    if path:
        result["path"] = path
    if query:
        result["query"] = query
    if line is not None:
        result["line"] = line
    if status:
        result["status"] = status
    return result


def _session_relates_to_project(snapshot: dict[str, Any], project_root: Path) -> bool:
    root_text = str(project_root)
    workspace_path = str(snapshot.get("workspace_path") or "").strip()
    if workspace_path:
        with contextlib.suppress(OSError):
            if Path(workspace_path).expanduser().resolve() == project_root.resolve():
                return True
    for value in snapshot.get("files_touched") or []:
        if isinstance(value, str) and (value == root_text or value.startswith(root_text + os.sep)):
            return True
    encoded_root = root_text.replace(os.sep, "-")
    if any(encoded_root in str(value) for value in snapshot.get("raw_artifact_ids") or []):
        return True
    for event in reversed(snapshot.get("events") or []):
        if not isinstance(event, dict):
            continue
        payload_value = event.get("payload")
        payload: dict[str, Any] = payload_value if isinstance(payload_value, dict) else {}
        path = _event_path(payload)
        if path and (path == root_text or path.startswith(root_text + os.sep)):
            return True
    return False


def _attach_activity_targets(engine: Any, events: list[dict[str, Any]]) -> None:
    """Resolve small event hints to exact graph node IDs without exposing outputs."""

    for event in events:
        targets: list[str] = []
        path = str(event.get("path") or "")
        line = int(event.get("line") or 0)
        if path and line:
            with contextlib.suppress(OSError, ValueError, sqlite3.Error):
                rows = (
                    engine.connection()
                    .execute(
                        """
                    SELECT symbol_id
                    FROM symbols
                    WHERE file_path = ? AND start_line <= ? AND end_line >= ?
                    ORDER BY (end_line - start_line) ASC
                    LIMIT 3
                    """,
                        (path, line, line),
                    )
                    .fetchall()
                )
                targets.extend(str(row["symbol_id"]) for row in rows)
        if path:
            targets.append(_file_node_id(path))
        query = str(event.get("query") or "").strip()
        if query:
            with contextlib.suppress(OSError, LookupError, ValueError, sqlite3.Error):
                targets.extend(match["id"] for match in search_symbols(engine, query, limit=5))
        if targets:
            event["symbol_ids"] = list(dict.fromkeys(targets))[:6]


def recent_activity(
    runtime_root: Path,
    project_root: Path,
    *,
    after: str | None = None,
    limit: int = 60,
    engine: Any | None = None,
) -> dict[str, Any]:
    """Return the newest relevant session's small map activity projection."""

    run_files = sorted(
        (runtime_root / "sessions").glob("*/*/*/*/*/run.json"),
        key=lambda path: path.stat().st_mtime if path.exists() else 0,
        reverse=True,
    )
    snapshot: dict[str, Any] | None = None
    for path in run_files[:40]:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(candidate, dict) and _session_relates_to_project(candidate, project_root):
            snapshot = candidate
            break
    if snapshot is None:
        return {"session_id": None, "status": "idle", "events": [], "cursor": after}

    session_id = str(snapshot.get("session_id") or "")
    after_dt = _parse_at(after)
    events: list[dict[str, Any]] = []
    for sequence, raw in enumerate(snapshot.get("events") or []):
        if not isinstance(raw, dict):
            continue
        event_dt = _parse_at(raw.get("at"))
        if after_dt is not None and (event_dt is None or event_dt <= after_dt):
            continue
        projected = classify_activity_event(
            raw,
            session_id=session_id,
            sequence=sequence,
            project_root=project_root,
        )
        if projected is not None:
            events.append(projected)
    bounded_limit = max(1, min(limit, 200))
    events = events[-bounded_limit:]
    if engine is not None:
        _attach_activity_targets(engine, events)
    cursor = events[-1]["at"] if events else after
    return {
        "session_id": session_id,
        "status": str(snapshot.get("status") or "running"),
        "events": events,
        "cursor": cursor,
    }


__all__ = [
    "build_full_graph",
    "build_neighborhood",
    "build_overview",
    "classify_activity_event",
    "get_engine",
    "list_projects",
    "recent_activity",
    "resolve_project_root",
    "search_symbols",
    "symbol_detail",
]
