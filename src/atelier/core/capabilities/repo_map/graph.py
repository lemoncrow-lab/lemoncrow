"""Reference graph construction for repo maps."""

from __future__ import annotations

import fnmatch
import subprocess
from collections.abc import Callable
from pathlib import Path

from atelier.core.capabilities.repo_map.tag_cache import TagCache
from atelier.core.foundation._graph import DiGraph
from atelier.infra.tree_sitter.tags import Tag, detect_language, extract_tags

# In-process cache: building the reference graph parses every source file with
# tree-sitter (~14-37 s for a mid-size repo). The result is pure-functional given
# the repo root + file list, so a single dict cache makes repeated calls free.
_REFERENCE_GRAPH_CACHE: dict[
    tuple[str, tuple[str, ...] | None],
    tuple[DiGraph, dict[str, list[Tag]]],
] = {}

_SKIP_PARTS = {
    ".git",
    ".atelier",
    ".bench-work",
    ".venv",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "node_modules",
    "dist",
    "build",
    "__pycache__",
    # Raw data/results dumps (fixtures, benchmark output, run logs) are never
    # source code, but a directory literally named this often holds thousands
    # of JSON/CSV files that DO match a data-language extension -- each one
    # gets symbol-extracted (every JSON key becomes a "variable" symbol),
    # ballooning index/embedding time and diluting search with noise.
    "results",
    "data",
}


def should_skip_relative_path(path: str) -> bool:
    return any(part in _SKIP_PARTS for part in Path(path).parts)


def should_skip_path(path: Path, *, repo_root: Path | None = None) -> bool:
    try:
        rel = path.relative_to(repo_root) if repo_root is not None else path
    except ValueError:
        rel = path
    return should_skip_relative_path(rel.as_posix())


def _source_file_patterns() -> list[str]:
    """Return glob patterns for all languages in the canonical registry."""
    from atelier.infra.code_intel.languages import LANGUAGES

    seen: set[str] = set()
    patterns: list[str] = []
    for lang in LANGUAGES:
        for filename in sorted(lang.filenames):
            for pattern in (filename, f"**/{filename}"):
                if pattern not in seen:
                    seen.add(pattern)
                    patterns.append(pattern)
        for ext in sorted(lang.extensions, key=lambda e: (-len(e), e)):
            pattern = f"**/*{ext}"
            if pattern not in seen:
                seen.add(pattern)
                patterns.append(pattern)
    return patterns


def iter_source_files(
    repo_root: Path,
    include_globs: list[str] | None = None,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> list[Path]:
    patterns = include_globs or _source_file_patterns()
    files = _iter_git_visible_source_files(repo_root, patterns, progress_callback=progress_callback)
    if files:
        return files
    files = _iter_glob_source_files(repo_root, patterns, progress_callback=progress_callback)
    return files


def _iter_git_visible_source_files(
    repo_root: Path, patterns: list[str], *, progress_callback: Callable[[int, int], None] | None = None
) -> list[Path]:
    try:
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
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
    total_raw = len(entries)
    for i, raw_entry in enumerate(entries):
        if progress_callback is not None:
            progress_callback(i, total_raw)
        rel = raw_entry.decode("utf-8", errors="replace")
        # Match against both the full repo-relative path and the bare basename:
        # the default patterns are recursive globs like ``**/*.py`` and
        # ``fnmatch`` (unlike pathlib) does not treat ``**`` as "zero or more
        # dirs", so ``fnmatch("run.py", "**/*.py")`` is False and root-level
        # source files would be silently dropped from the git-visible index.
        name = rel.rsplit("/", 1)[-1]
        if not any(
            fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(name, pattern.rsplit("/", 1)[-1]) for pattern in patterns
        ):
            continue
        path = (repo_root / rel).resolve()
        if not path.is_file():
            continue
        if should_skip_path(path, repo_root=repo_root):
            continue
        if detect_language(path) is None:
            continue
        files.append(path)
    return sorted(set(files))


def _iter_glob_source_files(
    repo_root: Path, patterns: list[str], *, progress_callback: Callable[[int, int], None] | None = None
) -> list[Path]:
    files: list[Path] = []
    seen_inode: set[int] = set()
    for pattern in patterns:
        for path in repo_root.glob(pattern):
            if progress_callback is not None:
                progress_callback(len(files), 0)  # type: ignore[arg-type]
            if not path.is_file():
                continue
            if should_skip_path(path, repo_root=repo_root):
                continue
            if detect_language(path) is None:
                continue
            # Deduplicate by inode to handle case-insensitive filesystems
            # (e.g. macOS APFS) where Makefile/makefile etc. refer to the same
            # file but pathlib treats them as distinct Path objects.
            try:
                ino = path.stat().st_ino
            except OSError:
                ino = 0
            if ino and ino in seen_inode:
                continue
            if ino:
                seen_inode.add(ino)
            files.append(path)
    return sorted(set(files))


def build_reference_graph(
    repo_root: str | Path, files: list[str] | None = None
) -> tuple[DiGraph, dict[str, list[Tag]]]:
    """Build a file graph from symbol references to definitions."""
    root = Path(repo_root)
    cache_key: tuple[str, tuple[str, ...] | None] = (
        str(root.resolve()),
        tuple(sorted(files)) if files is not None else None,
    )
    if cache_key in _REFERENCE_GRAPH_CACHE:
        return _REFERENCE_GRAPH_CACHE[cache_key]
    paths = [root / file for file in files] if files else iter_source_files(root)
    tags_by_file: dict[str, list[Tag]] = {}
    definitions: dict[str, set[str]] = {}
    # Persistent, mtime-keyed tag cache (default-on; ATELIER_REPOMAP_TAG_CACHE
    # disables). extract_tags() is the dominant cost; the cache lets fresh
    # processes skip re-parsing files whose (mtime, size) are unchanged. The
    # cache is correctness-preserving via mtime invalidation and degrades to
    # in-memory on any DB failure, so graph building behaves identically.
    cache = TagCache.for_repo(root)
    try:
        for path in paths:
            tags = cache.get(path)
            if tags is None:
                try:
                    tags = extract_tags(path)
                except OSError:
                    tags = []
                else:
                    cache.put(path, tags)
            rel = str(path.relative_to(root)) if path.is_absolute() or path.exists() else str(path)
            tags_by_file[rel] = tags
            for tag in tags:
                if tag.kind == "definition":
                    definitions.setdefault(tag.name, set()).add(rel)
    finally:
        cache.close()

    graph = DiGraph()
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
    _REFERENCE_GRAPH_CACHE[cache_key] = (graph, tags_by_file)
    return graph, tags_by_file


__all__ = [
    "build_reference_graph",
    "iter_source_files",
    "should_skip_path",
    "should_skip_relative_path",
]
