"""Smart search capability for the consolidated MCP surface."""

from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import sqlite3
from pathlib import Path
from typing import Any, Literal

from atelier.core.capabilities.repo_map import build_repo_map
from atelier.core.capabilities.repo_map.graph import build_reference_graph
from atelier.core.capabilities.repo_map.pagerank import personalized_pagerank
from atelier.core.capabilities.tool_supervision.search_read import search_read, search_read_to_dict
from atelier.infra.code_intel.zoekt.adapter import get_zoekt_supervisor
from atelier.infra.storage.vector import cosine_similarity, generate_embedding

SearchMode = Literal["chunks", "full", "map"]

_SHELL_METACHARS_RE = re.compile(r"[;&|`$<>()\n\r]")
_LEADING_DASH_RE = re.compile(r"^-")
_SKIP_PARTS = {".git", ".atelier", ".venv", "node_modules", "dist", "build", "__pycache__"}
_TEXT_SUFFIXES = {
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".md",
    ".txt",
    ".toml",
    ".yaml",
    ".yml",
    ".json",
    ".sql",
    ".sh",
    ".css",
    ".html",
}


def _assert_safe_query(query: str, path: str) -> None:
    if _SHELL_METACHARS_RE.search(query):
        raise ValueError("smart_search rejected: shell metacharacters not allowed in query")
    if _LEADING_DASH_RE.match(query):
        raise ValueError("smart_search rejected: query must not start with '-'")
    if _SHELL_METACHARS_RE.search(path):
        raise ValueError("smart_search rejected: shell metacharacters not allowed in path")


def _repo_root() -> Path:
    return Path(os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())).resolve()


def _resolve_path(repo_root: Path, path: str) -> Path:
    raw = Path(path)
    resolved = raw if raw.is_absolute() else repo_root / raw
    return resolved.resolve()


def _iter_text_files(root: Path, *, limit: int = 500) -> list[Path]:
    if root.is_file():
        return [root] if root.suffix.lower() in _TEXT_SUFFIXES else []
    if not root.exists():
        return []
    files: list[Path] = []
    for item in root.rglob("*"):
        if len(files) >= limit:
            break
        if not item.is_file():
            continue
        if any(part in _SKIP_PARTS for part in item.parts):
            continue
        if item.suffix.lower() not in _TEXT_SUFFIXES:
            continue
        try:
            if item.stat().st_size > 512_000:
                continue
        except OSError:
            continue
        files.append(item)
    return sorted(files)


def _safe_fts_query(query: str) -> str:
    terms = re.findall(r"[A-Za-z0-9_]+", query)
    return " OR ".join(terms[:8])


def _fts_rank(repo_root: Path, search_path: Path, query: str, *, max_files: int) -> dict[str, float]:
    fts_query = _safe_fts_query(query)
    if not fts_query:
        return {}
    files = _iter_text_files(search_path)
    if not files:
        return {}
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE VIRTUAL TABLE docs USING fts5(path UNINDEXED, content)")
        for file_path in files:
            try:
                content = file_path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = str(file_path.relative_to(repo_root)) if file_path.is_relative_to(repo_root) else str(file_path)
            conn.execute("INSERT INTO docs(path, content) VALUES(?, ?)", (rel, content[:200_000]))
        rows = conn.execute(
            "SELECT path, bm25(docs) AS rank FROM docs WHERE docs MATCH ? ORDER BY rank LIMIT ?",
            (fts_query, max_files),
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        with contextlib.suppress(Exception):
            if conn is not None:
                conn.close()
    scores: dict[str, float] = {}
    for path, rank in rows:
        scores[str(path)] = 1.0 / (1.0 + abs(float(rank)))
    return scores


def _semantic_rank(repo_root: Path, paths: list[str], query: str) -> dict[str, float]:
    if not paths:
        return {}
    try:
        query_vector = generate_embedding(query)
    except Exception:
        return {}
    scores: dict[str, float] = {}
    for path in paths:
        file_path = repo_root / path
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")[:20_000]
            vector = generate_embedding(content)
        except Exception:
            continue
        scores[path] = max(0.0, cosine_similarity(query_vector, vector))
    return scores


def _graph_rank(repo_root: Path, seed_files: list[str]) -> dict[str, float]:
    try:
        graph, _tags = build_reference_graph(repo_root)
        return personalized_pagerank(graph, seed_files)
    except Exception:
        return {}


def _cache_key(payload: dict[str, Any], search_path: Path) -> str:
    stat_bits: list[str] = []
    with contextlib.suppress(Exception):
        if search_path.exists():
            stat = search_path.stat()
            stat_bits.append(f"{search_path}:{stat.st_size}:{stat.st_mtime_ns}")
    raw = json.dumps(payload, sort_keys=True) + "\n" + "\n".join(stat_bits)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def _state_path(repo_root: Path) -> Path:
    from hashlib import sha256

    from atelier.core.foundation.paths import default_store_root

    h = sha256(str(repo_root.resolve()).encode("utf-8")).hexdigest()[:12]
    return default_store_root() / "workspaces" / h / "smart_state.json"


def _load_cache(repo_root: Path) -> dict[str, Any]:
    path = _state_path(repo_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    cache = data.get("smart_search") if isinstance(data, dict) else None
    return cache if isinstance(cache, dict) else {}


def _save_cache(repo_root: Path, cache: dict[str, Any]) -> None:
    path = _state_path(repo_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if len(cache) > 100:
        for key in list(cache.keys())[: len(cache) - 100]:
            cache.pop(key, None)
    data["smart_search"] = cache
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")


def _normalize_scores(scores: dict[str, float]) -> dict[str, float]:
    if not scores:
        return {}
    highest = max(abs(value) for value in scores.values()) or 1.0
    return {key: value / highest for key, value in scores.items() if math.isfinite(value)}


def _search_with_backend(
    *,
    repo_root: Path,
    search_path: Path,
    query: str,
    max_files: int,
    max_chars_per_file: int,
    include_outline: bool,
) -> dict[str, Any] | None:
    supervisor = get_zoekt_supervisor(repo_root)
    if not supervisor.should_route(search_path):
        return None
    health = supervisor.health()
    if not health.ok:
        return None
    result = supervisor.search(
        query=query,
        search_path=search_path,
        max_files=max_files,
        max_chars_per_file=max_chars_per_file,
        include_outline=include_outline,
    )
    payload = search_read_to_dict(result, include_metadata=False)
    payload["backend"] = result.backend
    payload["index_age_seconds"] = result.index_age_seconds
    payload["total_tokens"] = result.total_tokens
    return payload


def smart_search(
    *,
    query: str,
    path: str = ".",
    mode: SearchMode = "chunks",
    max_files: int = 10,
    max_chars_per_file: int = 2000,
    include_outline: bool = True,
    seed_files: list[str] | None = None,
    budget_tokens: int = 2000,
) -> dict[str, Any]:
    """Search with lexical, semantic, and graph ranking signals."""
    _assert_safe_query(query, path)
    repo_root = _repo_root()
    search_path = _resolve_path(repo_root, path)
    seeds = seed_files or []

    if mode == "map":
        result = build_repo_map(repo_root, seed_files=seeds, budget_tokens=budget_tokens)
        map_result = result.model_dump(mode="json")
        map_result["mode"] = "map"
        return map_result

    cache_payload = {
        "query": query,
        "path": str(search_path),
        "mode": mode,
        "max_files": max_files,
        "max_chars_per_file": max_chars_per_file,
        "include_outline": include_outline,
        "seed_files": seeds,
        "budget_tokens": budget_tokens,
        "response_schema": 2,
    }
    cache_key = _cache_key(cache_payload, search_path)
    if os.environ.get("ATELIER_CACHE_DISABLED") != "1":
        cache = _load_cache(repo_root)
        cached = cache.get(cache_key)
        if isinstance(cached, dict):
            cached_matches = [match for match in cached.get("matches", []) if isinstance(match, dict)]
            return {
                "matches": cached_matches[:max_files],
                "mode": mode,
                "backend": str(cached.get("backend") or "ripgrep"),
                "index_age_seconds": cached.get("index_age_seconds"),
                "cache_hit": True,
            }
    else:
        cache = {}

    payload: dict[str, Any] | None = _search_with_backend(
        repo_root=repo_root,
        search_path=search_path,
        query=query,
        max_files=max_files,
        max_chars_per_file=max_chars_per_file,
        include_outline=include_outline,
    )
    if payload is None:
        chunk_result = search_read(
            query=query,
            path=str(search_path),
            max_files=max_files,
            max_chars_per_file=max_chars_per_file,
            include_outline=include_outline,
        )
        payload = search_read_to_dict(chunk_result, include_metadata=False)
    backend = str(payload.get("backend") or "ripgrep")
    matches = [match for match in payload.get("matches", []) if isinstance(match, dict)]
    if backend == "zoekt":
        if mode == "full":
            full_matches: list[dict[str, Any]] = []
            for match in matches[:max_files]:
                raw_path = str(match.get("path", ""))
                try:
                    content = Path(raw_path).read_text(encoding="utf-8", errors="replace")[:max_chars_per_file]
                except OSError:
                    content = ""
                full_matches.append({**match, "content": content, "snippets": []})
            matches = full_matches
        response = {
            "matches": matches[:max_files],
            "mode": mode,
            "backend": backend,
            "index_age_seconds": payload.get("index_age_seconds"),
            "total_tokens": payload.get("total_tokens", 0),
            "cache_hit": False,
        }
        if os.environ.get("ATELIER_CACHE_DISABLED") != "1":
            cache[cache_key] = response
            _save_cache(repo_root, cache)
        return response
    paths = [str(match.get("path", "")) for match in payload.get("matches", []) if isinstance(match, dict)]
    rel_paths = [
        str(Path(item).resolve().relative_to(repo_root)) if Path(item).resolve().is_relative_to(repo_root) else item
        for item in paths
    ]
    fts_scores = _normalize_scores(_fts_rank(repo_root, search_path, query, max_files=max_files * 2))
    semantic_scores = _normalize_scores(_semantic_rank(repo_root, rel_paths, query))
    graph_scores = _normalize_scores(_graph_rank(repo_root, seeds or rel_paths[:1]))

    def score(match: dict[str, Any]) -> float:
        raw_path = str(match.get("path", ""))
        try:
            rel = str(Path(raw_path).resolve().relative_to(repo_root))
        except ValueError:
            rel = raw_path
        snippet_score = 0.0
        snippets = match.get("snippets")
        if isinstance(snippets, list) and snippets:
            snippet_score = max(float(item.get("score", 0.0)) for item in snippets if isinstance(item, dict))
        return (
            snippet_score
            + 0.35 * fts_scores.get(rel, fts_scores.get(raw_path, 0.0))
            + 0.25 * semantic_scores.get(rel, 0.0)
            + 0.40 * graph_scores.get(rel, 0.0)
        )

    matches = [match for match in payload.get("matches", []) if isinstance(match, dict)]
    matches.sort(key=lambda item: (-score(item), str(item.get("path", ""))))
    if mode == "full":
        fm: list[dict[str, Any]] = []
        for match in matches[:max_files]:
            raw_path = str(match.get("path", ""))
            try:
                content = Path(raw_path).read_text(encoding="utf-8", errors="replace")[:max_chars_per_file]
            except OSError:
                content = ""
            fm.append({**match, "content": content, "snippets": []})
        matches = fm
    response = {
        "matches": matches[:max_files],
        "mode": mode,
        "backend": backend,
        "index_age_seconds": payload.get("index_age_seconds"),
        "cache_hit": False,
    }

    if os.environ.get("ATELIER_CACHE_DISABLED") != "1":
        cache[cache_key] = response
        _save_cache(repo_root, cache)
    return response


__all__ = ["SearchMode", "smart_search"]
