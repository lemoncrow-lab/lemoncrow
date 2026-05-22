from __future__ import annotations

from pathlib import Path

from atelier.infra.storage.sqlite_memory_store import SqliteMemoryStore

from atelier.core.service.bootstrap_context import (
    build_bootstrap_plan,
    expected_bootstrap_labels,
    list_bootstrap_blocks,
    persist_bootstrap_plan,
)


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "web").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "app.py").write_text(
        "from src.worker import run_worker\n\n"
        "def app() -> str:\n"
        "    return run_worker()\n\n"
        "def main() -> str:\n"
        "    return app()\n",
        encoding="utf-8",
    )
    (root / "src" / "worker.py").write_text(
        "def run_worker() -> str:\n"
        "    return 'worker'\n",
        encoding="utf-8",
    )
    (root / "scripts" / "cli.py").write_text(
        "from src.app import main\n\n"
        "def cli() -> str:\n"
        "    return main()\n",
        encoding="utf-8",
    )
    (root / "web" / "index.ts").write_text(
        "export function bootstrapApp(): string {\n"
        "  return 'ready';\n"
        "}\n",
        encoding="utf-8",
    )


def test_cold_repo_plans_and_persists_expected_bootstrap_blocks(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    store_root = tmp_path / ".atelier"
    _write_fixture_repo(repo_root)
    memory_store = SqliteMemoryStore(store_root)

    plan = build_bootstrap_plan(repo_root)
    persist_bootstrap_plan(repo_root, memory_store)
    blocks = list_bootstrap_blocks(memory_store, plan.repo_id)

    assert [block.label for block in blocks] == expected_bootstrap_labels(plan.repo_id)
    assert "src/app.py" in blocks[0].value
    assert "main" in blocks[1].value
    assert "run_worker" in blocks[2].value
    assert "python" in blocks[3].value


def test_bootstrap_plan_is_deterministic_and_does_not_embed_or_summarize(
    tmp_path: Path,
    monkeypatch,
) -> None:
    repo_root = tmp_path / "repo"
    _write_fixture_repo(repo_root)

    def _raise_if_called(_: object, __: list[str]) -> list[list[float]]:
        raise AssertionError("bootstrap planning must not invoke embeddings")

    monkeypatch.setattr(
        "atelier.core.capabilities.code_context.embedding.LocalEmbedder.embed",
        _raise_if_called,
    )

    first = build_bootstrap_plan(repo_root)
    second = build_bootstrap_plan(repo_root)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_partial_bootstrap_metadata_allows_retry_without_rewriting_completed_blocks(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    store_root = tmp_path / ".atelier"
    _write_fixture_repo(repo_root)
    memory_store = SqliteMemoryStore(store_root)

    plan = build_bootstrap_plan(repo_root)
    first_label = expected_bootstrap_labels(plan.repo_id)[0]
    persist_bootstrap_plan(repo_root, memory_store, labels=[first_label])

    partial_block = memory_store.get_block(plan.agent_id, first_label)
    assert partial_block is not None
    assert partial_block.metadata["bootstrap"]["status"] == "partial"
    assert partial_block.metadata["bootstrap"]["pending_labels"]

    original_version = partial_block.version
    result = persist_bootstrap_plan(repo_root, memory_store)

    completed_block = memory_store.get_block(plan.agent_id, first_label)
    assert completed_block is not None
    assert completed_block.version == original_version
    assert first_label in result.reused_labels
    assert len(list_bootstrap_blocks(memory_store, plan.repo_id)) == 4
