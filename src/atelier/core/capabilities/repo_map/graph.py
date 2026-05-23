"""Reference graph construction for repo maps."""

from __future__ import annotations

import fnmatch
import subprocess
from pathlib import Path

import networkx as nx

from atelier.infra.tree_sitter.tags import Tag, detect_language, extract_tags

_SKIP_PARTS = {".git", "node_modules", "dist", "build", "__pycache__", ".venv"}


def iter_source_files(repo_root: Path, include_globs: list[str] | None = None) -> list[Path]:
    patterns = include_globs or [
        "**/*.py",
        "**/*.js",
        "**/*.jsx",
        "**/*.ts",
        "**/*.tsx",
        "**/*.go",
        "**/*.rs",
    ]
    files = _iter_git_visible_source_files(repo_root, patterns)
    if files:
        return files
    files = _iter_glob_source_files(repo_root, patterns)
    return files


def _iter_git_visible_source_files(repo_root: Path, patterns: list[str]) -> list[Path]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=False,
        )
    except OSError:
        return []
    if completed.returncode != 0:
        return []
    files: list[Path] = []
    entries = [entry for entry in completed.stdout.split(b"\x00") if entry]
    for raw_entry in entries:
        rel = raw_entry.decode("utf-8", errors="replace")
        if not any(fnmatch.fnmatch(rel, pattern) for pattern in patterns):
            continue
        path = (repo_root / rel).resolve()
        if not path.is_file():
            continue
        if any(part in _SKIP_PARTS for part in path.parts):
            continue
        if detect_language(path) is None:
            continue
        files.append(path)
    return sorted(set(files))


def _iter_glob_source_files(repo_root: Path, patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in patterns:
        for path in repo_root.glob(pattern):
            if not path.is_file():
                continue
            if any(part in _SKIP_PARTS for part in path.parts):
                continue
            if detect_language(path) is None:
                continue
            files.append(path)
    return sorted(set(files))


def build_reference_graph(
    repo_root: str | Path, files: list[str] | None = None
) -> tuple[nx.DiGraph, dict[str, list[Tag]]]:
    """Build a file graph from symbol references to definitions."""
    root = Path(repo_root)
    paths = [root / file for file in files] if files else iter_source_files(root)
    tags_by_file: dict[str, list[Tag]] = {}
    definitions: dict[str, set[str]] = {}
    for path in paths:
        try:
            tags = extract_tags(path)
        except OSError:
            tags = []
        rel = str(path.relative_to(root)) if path.is_absolute() or path.exists() else str(path)
        tags_by_file[rel] = tags
        for tag in tags:
            if tag.kind == "definition":
                definitions.setdefault(tag.name, set()).add(rel)

    graph = nx.DiGraph()
    for rel in tags_by_file:
        graph.add_node(rel)
    for rel, tags in tags_by_file.items():
        for tag in tags:
            if tag.kind != "reference":
                continue
            for def_file in definitions.get(tag.name, set()):
                if def_file == rel:
                    continue
                weight = float(graph.get_edge_data(rel, def_file, {}).get("weight", 0.0)) + 1.0
                graph.add_edge(rel, def_file, weight=weight)
    return graph, tags_by_file


__all__ = ["build_reference_graph", "iter_source_files"]
