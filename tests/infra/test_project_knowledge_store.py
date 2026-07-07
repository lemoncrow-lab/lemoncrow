from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from atelier.core.foundation.models import Playbook, Rubric, to_jsonable
from atelier.core.foundation.paths import resolve_workspace_store_dir
from atelier.core.foundation.renderer import render_playbook_markdown
from atelier.core.foundation.store import ContextStore
from atelier.infra.storage.factory import create_store


def _sample_block() -> Playbook:
    return Playbook(
        id="rb-project-lessons-sync",
        title="Project lessons sync",
        domain="coding",
        task_types=["implementation"],
        triggers=["workspace lessons present"],
        situation="Load a tracked Playbook from project lessons.",
        procedure=["Read markdown from the project lessons directory", "Index it into SQLite"],
        verification=["The block is retrievable by id after init"],
    )


def _sample_rubric() -> Rubric:
    return Rubric(
        id="rubric-project-lessons-sync",
        domain="coding",
        required_checks=["lessons_synced"],
        block_if_missing=["lessons_synced"],
    )


def test_reasoning_store_writes_lessons_to_project_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    monkeypatch.setenv("ATELIER_WORKSPACE_ROOT", str(workspace))

    store_root = tmp_path / "global" / ".atelier"
    store = ContextStore(store_root)
    store.init()

    block = _sample_block()
    rubric = _sample_rubric()
    store.upsert_block(block)
    store.upsert_rubric(rubric)

    proj_dir = resolve_workspace_store_dir(store_root)
    assert store.root == store_root.resolve()
    # Mirrors live per-project under the global store root, not in .atelier/lessons.
    assert store.blocks_dir == (proj_dir / "blocks").resolve()
    assert store.rubrics_dir == (proj_dir / "rubrics").resolve()
    assert ".atelier/lessons" not in store.blocks_dir.parts
    assert (store.blocks_dir / f"{block.id}.md").exists()
    assert (store.rubrics_dir / f"{rubric.id}.yaml").exists()


def test_store_init_syncs_project_lessons_into_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "repo"
    monkeypatch.setenv("ATELIER_WORKSPACE_ROOT", str(workspace))

    store_root = tmp_path / "global" / ".atelier"
    proj_dir = resolve_workspace_store_dir(store_root)
    blocks_dir = proj_dir / "blocks"
    rubrics_dir = proj_dir / "rubrics"
    blocks_dir.mkdir(parents=True)
    rubrics_dir.mkdir(parents=True)

    block = _sample_block()
    rubric = _sample_rubric()
    (blocks_dir / f"{block.id}.md").write_text(render_playbook_markdown(block), encoding="utf-8")
    (rubrics_dir / f"{rubric.id}.yaml").write_text(
        yaml.safe_dump(to_jsonable(rubric), sort_keys=False),
        encoding="utf-8",
    )

    store = create_store(store_root)
    store.init()

    stored_block = store.get_block(block.id)
    stored_rubric = store.get_rubric(rubric.id)

    assert stored_block is not None
    assert stored_block.title == block.title
    assert stored_block.procedure == block.procedure
    assert stored_rubric is not None
    assert stored_rubric.required_checks == ["lessons_synced"]


def test_mcp_runtime_is_cached_not_recreated_per_call() -> None:
    """_runtime() returns a singleton so init() runs once per process."""
    from atelier.gateway.adapters.mcp_server import (
        _reset_runtime_cache_for_testing,
        _runtime,
    )

    _reset_runtime_cache_for_testing()

    r1 = _runtime()
    r2 = _runtime()
    assert r1 is r2  # same object


def test_sync_lessons_skips_unchanged_files_on_repeat_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "repo"
    monkeypatch.setenv("ATELIER_WORKSPACE_ROOT", str(workspace))

    store_root = tmp_path / "global" / ".atelier"
    blocks_dir = resolve_workspace_store_dir(store_root) / "blocks"
    blocks_dir.mkdir(parents=True)

    block = _sample_block()
    (blocks_dir / f"{block.id}.md").write_text(render_playbook_markdown(block), encoding="utf-8")

    store = create_store(store_root)
    store.init()

    # First call: 1 block synced
    result1 = store.sync_lessons()
    assert result1["blocks"] == 0  # already imported by init()

    # mtime manifest was written
    manifest_path = store._sync_manifest_path("blocks")
    assert manifest_path.exists()

    # Second call with no file changes: 0 blocks synced
    result2 = store.sync_lessons()
    assert result2["blocks"] == 0

    # Touch the file to change its mtime
    (blocks_dir / f"{block.id}.md").write_text(render_playbook_markdown(block), encoding="utf-8")
    result3 = store.sync_lessons()
    assert result3["blocks"] == 1  # re-synced because mtime changed

    # New file is synced
    block2 = Playbook(
        id="rb-second-block",
        title="Second block",
        domain="coding",
        situation="A second test block.",
        procedure=["step 1"],
    )
    (blocks_dir / f"{block2.id}.md").write_text(render_playbook_markdown(block2), encoding="utf-8")
    result4 = store.sync_lessons()
    assert result4["blocks"] == 1  # only the new one

    # Verify both are in SQLite
    assert store.get_block(block.id) is not None
    assert store.get_block(block2.id) is not None
