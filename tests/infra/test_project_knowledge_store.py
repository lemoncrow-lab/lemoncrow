from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from atelier.core.foundation.models import ReasonBlock, Rubric, to_jsonable
from atelier.core.foundation.renderer import render_block_markdown
from atelier.core.foundation.store import ContextStore
from atelier.infra.storage.factory import create_store


def _sample_block() -> ReasonBlock:
    return ReasonBlock(
        id="rb-project-knowledge-sync",
        title="Project knowledge sync",
        domain="coding",
        task_types=["implementation"],
        triggers=["workspace knowledge present"],
        situation="Load a tracked ReasonBlock from project knowledge.",
        procedure=["Read markdown from the project knowledge directory", "Index it into SQLite"],
        verification=["The block is retrievable by id after init"],
    )


def _sample_rubric() -> Rubric:
    return Rubric(
        id="rubric-project-knowledge-sync",
        domain="coding",
        required_checks=["knowledge_synced"],
        block_if_missing=["knowledge_synced"],
    )


def test_reasoning_store_writes_knowledge_to_project_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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

    assert store.root == store_root.resolve()
    assert store.blocks_dir == (workspace / ".knowledge" / "blocks").resolve()
    assert store.rubrics_dir == (workspace / ".knowledge" / "rubrics").resolve()
    assert (store.blocks_dir / f"{block.id}.md").exists()
    assert (store.rubrics_dir / f"{rubric.id}.yaml").exists()


def test_store_init_syncs_project_knowledge_into_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "repo"
    knowledge_root = workspace / ".knowledge"
    blocks_dir = knowledge_root / "blocks"
    rubrics_dir = knowledge_root / "rubrics"
    blocks_dir.mkdir(parents=True)
    rubrics_dir.mkdir(parents=True)
    monkeypatch.setenv("ATELIER_WORKSPACE_ROOT", str(workspace))

    block = _sample_block()
    rubric = _sample_rubric()
    (blocks_dir / f"{block.id}.md").write_text(render_block_markdown(block), encoding="utf-8")
    (rubrics_dir / f"{rubric.id}.yaml").write_text(
        yaml.safe_dump(to_jsonable(rubric), sort_keys=False),
        encoding="utf-8",
    )

    store = create_store(tmp_path / "global" / ".atelier")
    store.init()

    stored_block = store.get_block(block.id)
    stored_rubric = store.get_rubric(rubric.id)

    assert stored_block is not None
    assert stored_block.title == block.title
    assert stored_block.procedure == block.procedure
    assert stored_rubric is not None
    assert stored_rubric.required_checks == ["knowledge_synced"]


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


def test_sync_knowledge_skips_unchanged_files_on_repeat_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    workspace = tmp_path / "repo"
    knowledge_root = workspace / ".knowledge"
    blocks_dir = knowledge_root / "blocks"
    blocks_dir.mkdir(parents=True)
    monkeypatch.setenv("ATELIER_WORKSPACE_ROOT", str(workspace))

    block = _sample_block()
    (blocks_dir / f"{block.id}.md").write_text(render_block_markdown(block), encoding="utf-8")

    store = create_store(tmp_path / "global" / ".atelier")
    store.init()

    # First call: 1 block synced
    result1 = store.sync_knowledge()
    assert result1["blocks"] == 0  # already imported by init()

    # mtime manifest was written
    manifest_path = store._sync_manifest_path("blocks")
    assert manifest_path.exists()

    # Second call with no file changes: 0 blocks synced
    result2 = store.sync_knowledge()
    assert result2["blocks"] == 0

    # Touch the file to change its mtime
    (blocks_dir / f"{block.id}.md").write_text(render_block_markdown(block), encoding="utf-8")
    result3 = store.sync_knowledge()
    assert result3["blocks"] == 1  # re-synced because mtime changed

    # New file is synced
    block2 = ReasonBlock(
        id="rb-second-block",
        title="Second block",
        domain="coding",
        situation="A second test block.",
        procedure=["step 1"],
    )
    (blocks_dir / f"{block2.id}.md").write_text(render_block_markdown(block2), encoding="utf-8")
    result4 = store.sync_knowledge()
    assert result4["blocks"] == 1  # only the new one

    # Verify both are in SQLite
    assert store.get_block(block.id) is not None
    assert store.get_block(block2.id) is not None
