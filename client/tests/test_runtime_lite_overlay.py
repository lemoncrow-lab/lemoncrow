"""A linked worktree's Runtime Lite index is an overlay on the main checkout's index."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.lite import index as lite_index
from lemoncrow_client.lite.index import LiteIndex, open_index_reader

_MODULES = 30


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True, capture_output=True
    )


def _module(number: int) -> str:
    return f"import zlib{number}\n\n# needle{number}marker\ndef func_{number}():\n    return {number}\n"


def _main_repo(tmp_path: Path) -> Path:
    root = tmp_path / "main"
    root.mkdir()
    for number in range(_MODULES):
        (root / f"mod_{number}.py").write_text(_module(number), encoding="utf-8")
    (root / "app.py").write_text(
        "import os\n\n# basecomment\ndef base_version():\n    return os.sep\n", encoding="utf-8"
    )
    _git(root, "init", "-q", "-b", "main")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root.resolve()


def _worktree(root: Path, name: str = "feature") -> Path:
    linked = root.parent / name
    _git(root, "worktree", "add", "-q", "-b", name, str(linked))
    return linked.resolve()


def _diverge(linked: Path) -> None:
    """Change app.py, delete mod_0.py and add new.py in the worktree."""

    (linked / "app.py").write_text(
        "import json\n\n# worktreecomment\ndef worktree_version():\n    return json.dumps(1)\n", encoding="utf-8"
    )
    (linked / "mod_0.py").unlink()
    (linked / "new.py").write_text("import fresh_dep\n\ndef fresh_feature():\n    return 1\n", encoding="utf-8")


def _own_and_inherited(index: LiteIndex) -> tuple[set[str], set[str]]:
    with sqlite3.connect(index.db_path) as raw:
        own = {str(row[0]) for row in raw.execute("SELECT path FROM files")}
        inherited = {str(row[0]) for row in raw.execute("SELECT path FROM inherit")}
    return own, inherited


def _meta(index: LiteIndex) -> dict[str, str]:
    with sqlite3.connect(index.db_path) as raw:
        return {str(key): str(value) for key, value in raw.execute("SELECT key, value FROM meta")}


def _paths(result: dict[str, Any]) -> set[str]:
    return {str(item["path"]) for item in result["files"]} | set(result["candidate_files"])


def _symbol_paths(index: LiteIndex, name: str) -> set[str]:
    return {str(row["path"]) for row in index.exact_symbols(name)}


@pytest.fixture
def overlay(tmp_path: Path) -> tuple[LiteIndex, LiteIndex]:
    root = _main_repo(tmp_path)
    main = LiteIndex(root)
    main.prepare()
    linked = _worktree(root)
    _diverge(linked)
    child = LiteIndex(linked)
    child.prepare()
    return main, child


def test_worktree_overlay_stores_only_divergent_rows(overlay: tuple[LiteIndex, LiteIndex]) -> None:
    main, child = overlay
    own, inherited = _own_and_inherited(child)
    assert own == {"app.py", "new.py"}
    assert inherited == {f"mod_{number}.py" for number in range(1, _MODULES)}
    with sqlite3.connect(child.db_path) as raw:
        assert raw.execute("SELECT COUNT(*) FROM file_fts").fetchone()[0] == 2
        assert {row[0] for row in raw.execute("SELECT DISTINCT path FROM symbols")} == {"app.py", "new.py"}
    meta = _meta(child)
    assert meta["schema_version"] == "4"
    assert meta["base_path"] == str(main.db_path)
    assert meta["base_generation"] == str(main.generation())
    # Readers see every worktree file once: own, inherited, never the deleted one.
    with open_index_reader(child.db_path) as reader:
        visible = [str(row[0]) for row in reader.execute("SELECT path FROM files ORDER BY path")]
    assert len(visible) == len(set(visible)) == _MODULES + 1
    assert "mod_0.py" not in visible and {"app.py", "new.py", "mod_1.py"} <= set(visible)


def test_overlay_reads_inherit_base_rows_and_shadow_changed_deleted_and_new_files(
    overlay: tuple[LiteIndex, LiteIndex],
) -> None:
    _main, child = overlay

    # Base-inherited content: symbols, FTS text, imports and the repo map.
    assert _symbol_paths(child, "func_5") == {"mod_5.py"}
    assert "mod_5.py" in _paths(child.search("func_5"))
    assert "mod_7.py" in _paths(child.search("needle7marker"))
    assert [item["path"] for item in child.relations("func_5", kind="self")["items"]] == ["mod_5.py"]
    assert [item["path"] for item in child.relations("zlib9", kind="imports")["items"]] == ["mod_9.py"]
    mapped = {item["path"]: item for item in child.repo_map(limit=200)["items"]}
    assert mapped["mod_5.py"]["symbols"] == 1 and mapped["mod_5.py"]["imports"] == 1

    # A file changed in the worktree answers with the worktree's version only.
    assert _symbol_paths(child, "worktree_version") == {"app.py"}
    assert not child.exact_symbols("base_version")
    assert "app.py" in _paths(child.search("worktreecomment"))
    assert "app.py" not in _paths(child.search("basecomment"))
    assert [item["path"] for item in child.relations("json", kind="imports")["items"]] == ["app.py"]
    assert not child.relations("os", kind="imports")["items"]
    assert mapped["app.py"]["symbols"] == 1 and mapped["app.py"]["imports"] == 1

    # A file deleted in the worktree is never served from the base.
    assert not child.exact_symbols("func_0")
    assert "mod_0.py" not in _paths(child.search("func_0"))
    assert "mod_0.py" not in _paths(child.search("needle0marker"))
    assert not child.relations("zlib0", kind="imports")["items"]
    assert "mod_0.py" not in mapped

    # A worktree-only file is found.
    assert _symbol_paths(child, "fresh_feature") == {"new.py"}
    assert child.search("fresh_feature")["files"][0]["path"] == "new.py"
    assert [item["path"] for item in child.relations("fresh_dep", kind="imports")["items"]] == ["new.py"]
    assert len(mapped) == _MODULES + 1
    assert [item["path"] for item in child.repo_map(kind="imports", path="new.py")["items"]] == ["new.py"]


def test_a_base_change_hides_the_path_until_the_overlay_refreshes(
    overlay: tuple[LiteIndex, LiteIndex], monkeypatch: pytest.MonkeyPatch
) -> None:
    main, child = overlay
    changed = "# basechangedmarker\ndef func_3_changed():\n    return 33\n"
    (main.repo_root / "mod_3.py").write_text(changed, encoding="utf-8")
    main.prepare()

    # Before the worktree refreshes: the base's new content is never served for
    # mod_3.py, and its old content is no longer provable, so the path is hidden.
    monkeypatch.setattr(child, "maybe_refresh", lambda: None)
    assert not child.exact_symbols("func_3_changed")
    assert "mod_3.py" not in _paths(child.search("basechangedmarker"))
    assert "mod_3.py" not in _paths(child.search("func_3_changed"))
    assert "mod_3.py" not in {item["path"] for item in child.repo_map(limit=200)["items"]}
    assert _symbol_paths(child, "func_5") == {"mod_5.py"}
    monkeypatch.undo()

    # The freshness check notices the base generation (the worktree signature
    # did not move) and re-checks only the inherited paths the base changed.
    def forbidden_walk(_root: Path) -> Any:
        raise AssertionError("a base-only refresh must not walk the worktree")

    monkeypatch.setattr(lite_index, "_iter_git_paths", forbidden_walk)
    child._last_refresh_check = 0.0
    assert _symbol_paths(child, "func_3") == {"mod_3.py"}
    assert not child.exact_symbols("func_3_changed")
    own, inherited = _own_and_inherited(child)
    assert "mod_3.py" in own and "mod_3.py" not in inherited
    assert _meta(child)["base_generation"] == str(main.generation())
    monkeypatch.undo()

    # The worktree now takes the base's content: equal again, so inherited and
    # served from the base.
    (child.repo_root / "mod_3.py").write_text(changed, encoding="utf-8")
    child.prepare()
    own, inherited = _own_and_inherited(child)
    assert "mod_3.py" in inherited and "mod_3.py" not in own
    assert _symbol_paths(child, "func_3_changed") == {"mod_3.py"}
    assert "mod_3.py" in _paths(child.search("basechangedmarker"))


def test_query_cache_is_keyed_by_the_base_generation(overlay: tuple[LiteIndex, LiteIndex]) -> None:
    main, child = overlay
    assert "mod_4.py" in _paths(child.search("needle4marker"))
    (main.repo_root / "mod_4.py").write_text("def other():\n    return 0\n", encoding="utf-8")
    main.prepare()
    child.maybe_refresh = lambda: None  # type: ignore[method-assign]
    assert "mod_4.py" not in _paths(child.search("needle4marker"))


def test_standalone_worktree_compacts_into_an_overlay_once_the_base_exists(tmp_path: Path) -> None:
    root = _main_repo(tmp_path)
    linked = _worktree(root)
    _diverge(linked)
    child = LiteIndex(linked)
    child.prepare()
    own, inherited = _own_and_inherited(child)
    assert len(own) == _MODULES + 1 and not inherited
    assert _meta(child)["base_path"] == ""

    main = LiteIndex(root)
    main.prepare()
    # The worktree itself did not change: the base appearing alone must convert it.
    report = child.prepare()
    assert report.generation == 2
    own, inherited = _own_and_inherited(child)
    assert own == {"app.py", "new.py"}
    assert len(inherited) == _MODULES - 1
    assert _meta(child)["base_path"] == str(main.db_path)
    assert _symbol_paths(child, "func_5") == {"mod_5.py"}
    with sqlite3.connect(child.db_path) as raw:
        assert raw.execute("SELECT COUNT(*) FROM file_fts").fetchone()[0] == 2


def test_a_schema_3_full_worktree_index_converts_to_an_overlay(tmp_path: Path) -> None:
    root = _main_repo(tmp_path)
    linked = _worktree(root)
    _diverge(linked)
    child = LiteIndex(linked)
    child.prepare()
    # Turn the standalone index into what Runtime Lite schema 3 wrote.
    with sqlite3.connect(child.db_path) as raw:
        raw.execute("DROP TABLE inherit")
        raw.execute("DELETE FROM meta WHERE key IN ('base_path', 'base_generation')")
        raw.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    LiteIndex(root).prepare()

    converted = LiteIndex(linked)
    converted.prepare()
    assert _meta(converted)["schema_version"] == "4"
    own, inherited = _own_and_inherited(converted)
    assert own == {"app.py", "new.py"}
    assert len(inherited) == _MODULES - 1
    assert _symbol_paths(converted, "func_5") == {"mod_5.py"}
    assert _symbol_paths(converted, "worktree_version") == {"app.py"}


def test_the_main_checkout_and_bare_repository_worktrees_stay_standalone(tmp_path: Path) -> None:
    root = _main_repo(tmp_path)
    linked = _worktree(root)
    LiteIndex(linked).prepare()  # The worktree first: there is no base yet.
    main = LiteIndex(root)
    main.prepare()
    LiteIndex(linked).prepare()
    main.prepare(force=True)
    own, inherited = _own_and_inherited(main)
    assert len(own) == _MODULES + 1 and not inherited
    assert _meta(main)["base_path"] == ""

    bare = tmp_path / "bare.git"
    _git(tmp_path, "clone", "-q", "--bare", str(root), str(bare))
    bare_worktree = tmp_path / "bare-worktree"
    _git(bare, "worktree", "add", "-q", str(bare_worktree), "main")
    from_bare = LiteIndex(bare_worktree)
    from_bare.prepare()
    own, inherited = _own_and_inherited(from_bare)
    assert len(own) == _MODULES + 1 and not inherited
    assert _meta(from_bare)["base_path"] == ""


@pytest.mark.parametrize("failure", ["deleted", "corrupt"])
def test_an_unattachable_base_serves_own_rows_then_the_index_goes_standalone(
    overlay: tuple[LiteIndex, LiteIndex], monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    main, child = overlay
    for suffix in ("", "-wal", "-shm"):
        main.db_path.with_name(main.db_path.name + suffix).unlink(missing_ok=True)
    if failure == "corrupt":
        main.db_path.write_bytes(b"not a sqlite database" * 64)

    monkeypatch.setattr(child, "maybe_refresh", lambda: None)
    child._dirty = False
    assert _symbol_paths(child, "worktree_version") == {"app.py"}
    assert not child.exact_symbols("func_5")
    assert "app.py" in _paths(child.search("worktreecomment"))
    assert "new.py" in {item["path"] for item in child.repo_map()["items"]}
    assert child._dirty, "serving own rows only must schedule a refresh"
    monkeypatch.undo()

    child.prepare()
    own, inherited = _own_and_inherited(child)
    assert len(own) == _MODULES + 1 and not inherited
    assert _meta(child)["base_path"] == ""
    assert _symbol_paths(child, "func_5") == {"mod_5.py"}


def test_symbol_lookup_through_the_view_uses_the_base_name_index(overlay: tuple[LiteIndex, LiteIndex]) -> None:
    _main, child = overlay
    with open_index_reader(child.db_path) as reader:
        plan = [
            str(row[3])
            for row in reader.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT name, kind, path, line, end_line, signature
                FROM symbols WHERE name=? COLLATE NOCASE
                ORDER BY path, line LIMIT 32
                """,
                ("func_5",),
            )
        ]
    assert any("symbols_name_idx" in detail and detail.startswith("SEARCH s ") for detail in plan), plan
    assert not any(detail.startswith("SCAN s") for detail in plan), plan
