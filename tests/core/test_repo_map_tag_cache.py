"""Tests for the persistent, mtime-keyed SQLite tag cache (repo map T3).

These exercise the cache directly and through ``build_reference_graph``:
  * a warm hit on an unchanged file skips ``extract_tags``
  * an mtime *or* size change invalidates the entry (re-parse happens)
  * an unwritable DB path falls back to an in-memory store (never raises)
  * the ``LEMONCROW_REPOMAP_TAG_CACHE`` kill switch disables persistence

The in-process ``_REFERENCE_GRAPH_CACHE`` short-circuits repeated identical
calls within one process, so tests that want to exercise the *persistent*
layer clear it between calls to simulate a fresh process.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lemoncrow.infra.tree_sitter.tags import Tag, extract_tags
from lemoncrow.pro.capabilities.repo_map import graph as graph_mod
from lemoncrow.pro.capabilities.repo_map.graph import build_reference_graph
from lemoncrow.pro.capabilities.repo_map.tag_cache import (
    TagCache,
    default_tag_cache_path,
    tag_cache_enabled,
)


@pytest.fixture(autouse=True)
def _isolate_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the global store root at a throwaway dir and reset env + caches."""
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "_store"))
    monkeypatch.delenv("LEMONCROW_REPOMAP_TAG_CACHE", raising=False)
    graph_mod._REFERENCE_GRAPH_CACHE.clear()
    yield
    graph_mod._REFERENCE_GRAPH_CACHE.clear()


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


# --- direct TagCache behaviour ------------------------------------------------


def test_default_path_follows_per_project_convention(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    db_path = default_tag_cache_path(repo)
    # Lives under <store_root>/workspaces/<hash>/repo_map_tags.sqlite
    assert db_path.name == "repo_map_tags.sqlite"
    assert db_path.parent.parent.name == "workspaces"
    assert str(tmp_path / "_store") in str(db_path)


def test_put_then_get_returns_cached_tags(tmp_path: Path) -> None:
    src = tmp_path / "service.py"
    _write(src, "def alpha():\n    return 1\n")
    tags = extract_tags(src)

    cache = TagCache.for_repo(tmp_path)
    try:
        assert cache.get(src) is None  # cold miss
        cache.put(src, tags)
        cached = cache.get(src)
    finally:
        cache.close()

    assert cached is not None
    assert {t.name for t in cached} == {t.name for t in tags}
    assert all(isinstance(t, Tag) for t in cached)


def test_get_invalidates_on_mtime_change(tmp_path: Path) -> None:
    src = tmp_path / "a.py"
    _write(src, "def alpha():\n    return 1\n")
    cache = TagCache.for_repo(tmp_path)
    try:
        cache.put(src, extract_tags(src))
        assert cache.get(src) is not None
        # Bump mtime but keep identical size -> invalidation by mtime alone.
        stat = src.stat()
        os.utime(src, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        assert cache.get(src) is None
    finally:
        cache.close()


def test_get_invalidates_on_size_change(tmp_path: Path) -> None:
    src = tmp_path / "a.py"
    _write(src, "def alpha():\n    return 1\n")
    cache = TagCache.for_repo(tmp_path)
    try:
        cache.put(src, extract_tags(src))
        assert cache.get(src) is not None
        # Force a different (mtime, size) by appending content.
        _write(src, "def alpha():\n    return 1\n\ndef beta():\n    return 2\n")
        assert cache.get(src) is None
    finally:
        cache.close()


def test_unwritable_db_falls_back_to_memory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Make the resolved DB path unusable: a *file* where a directory must be.
    blocker = tmp_path / "blocked"
    blocker.write_text("not a dir", encoding="utf-8")
    monkeypatch.setattr(
        "lemoncrow.pro.capabilities.repo_map.tag_cache.default_tag_cache_path",
        lambda _root: blocker / "workspaces" / "x" / "repo_map_tags.sqlite",
    )
    src = tmp_path / "a.py"
    _write(src, "def alpha():\n    return 1\n")

    cache = TagCache.for_repo(tmp_path)
    try:
        # Persistence is dead, but in-memory mirror still works and nothing raises.
        assert cache._conn is None
        tags = extract_tags(src)
        cache.put(src, tags)
        cached = cache.get(src)
    finally:
        cache.close()

    assert cached is not None
    assert {t.name for t in cached} == {t.name for t in tags}


def test_kill_switch_disables_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for value in ("0", "false", "off", "OFF", "No"):
        monkeypatch.setenv("LEMONCROW_REPOMAP_TAG_CACHE", value)
        assert tag_cache_enabled() is False
        cache = TagCache.for_repo(tmp_path)
        try:
            assert cache._conn is None  # no SQLite backing
        finally:
            cache.close()
    # No DB file was ever created on disk.
    db_path = default_tag_cache_path(tmp_path)
    assert not db_path.exists()


def test_enabled_by_default_and_for_truthy_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_REPOMAP_TAG_CACHE", raising=False)
    assert tag_cache_enabled() is True
    for value in ("1", "true", "on", "yes", "anything"):
        monkeypatch.setenv("LEMONCROW_REPOMAP_TAG_CACHE", value)
        assert tag_cache_enabled() is True


# --- integration through build_reference_graph --------------------------------


def _spy_extract(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Wrap graph.extract_tags to record which paths it actually parses."""
    calls: list[Path] = []
    real = graph_mod.extract_tags

    def spy(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(Path(path))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(graph_mod, "extract_tags", spy)
    return calls


def test_warm_start_skips_extract_tags_on_unchanged_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "a.py", "def alpha():\n    return Beta()\n")
    _write(tmp_path / "b.py", "class Beta:\n    pass\n")
    files = ["a.py", "b.py"]

    calls = _spy_extract(monkeypatch)

    # Cold build: every file is parsed and persisted.
    graph1, tags1 = build_reference_graph(tmp_path, files=files)
    assert len(calls) == 2

    # Simulate a fresh process: drop the in-process graph cache so the
    # persistent SQLite cache is the only thing that can serve the tags.
    graph_mod._REFERENCE_GRAPH_CACHE.clear()
    calls.clear()

    graph2, tags2 = build_reference_graph(tmp_path, files=files)
    # Warm start: extract_tags is NOT re-called for the unchanged files.
    assert calls == []
    # Behaviour is identical: same tags, same edges.
    assert {k: {t.name for t in v} for k, v in tags2.items()} == {k: {t.name for t in v} for k, v in tags1.items()}
    assert set(graph2.edges()) == set(graph1.edges())


def test_changed_file_is_reparsed_only_for_that_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path / "a.py", "def alpha():\n    return 1\n")
    _write(tmp_path / "b.py", "def beta():\n    return 2\n")
    files = ["a.py", "b.py"]

    calls = _spy_extract(monkeypatch)
    build_reference_graph(tmp_path, files=files)
    assert len(calls) == 2

    graph_mod._REFERENCE_GRAPH_CACHE.clear()
    calls.clear()

    # Change only a.py (different size); b.py stays warm.
    _write(tmp_path / "a.py", "def alpha():\n    return 1\n\ndef gamma():\n    return 3\n")
    _graph, tags = build_reference_graph(tmp_path, files=files)

    assert calls == [tmp_path / "a.py"]
    # The re-parsed file reflects the new definition.
    assert "gamma" in {t.name for t in tags["a.py"]}


def test_build_reference_graph_kill_switch_always_reparses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_REPOMAP_TAG_CACHE", "0")
    _write(tmp_path / "a.py", "def alpha():\n    return 1\n")
    files = ["a.py"]

    calls = _spy_extract(monkeypatch)
    build_reference_graph(tmp_path, files=files)
    graph_mod._REFERENCE_GRAPH_CACHE.clear()
    build_reference_graph(tmp_path, files=files)

    # With caching disabled, both builds parse the file.
    assert calls == [tmp_path / "a.py", tmp_path / "a.py"]
    # No DB written.
    assert not default_tag_cache_path(tmp_path).exists()
