from __future__ import annotations

import importlib
import sqlite3
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]


def test_phase4_declares_pygit2_as_pinned_dependency() -> None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pygit2==1.19.2"' in pyproject


def test_git_history_bootstrap_requires_pygit2_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    module = importlib.import_module("atelier.infra.code_intel.git_history")
    monkeypatch.setattr(module, "_PYGIT2", None)
    monkeypatch.setattr(module, "_PYGIT2_IMPORT_ERROR", ImportError("boom"))

    with pytest.raises(module.GitHistoryBootstrapError) as excinfo:
        module.require_pygit2()

    assert "pygit2" in str(excinfo.value)
    assert "GitPython" in str(excinfo.value)
    assert "subprocess" in str(excinfo.value)


def _git(args: list[str], repo_root: Path) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _commit_all(repo_root: Path, message: str) -> str:
    _git(["add", "-A"], repo_root)
    _git(["commit", "-m", message], repo_root)
    return _git(["rev-parse", "HEAD"], repo_root)


def _create_history_fixture(tmp_path: Path) -> tuple[Path, str, str]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(["init"], repo_root)
    _git(["config", "user.name", "Fixture Tester"], repo_root)
    _git(["config", "user.email", "fixture@example.com"], repo_root)
    (repo_root / "legacy.py").write_text(
        "class LegacyCheckout:\n" "    def process(self) -> int:\n" "        return 1\n",
        encoding="utf-8",
    )
    _commit_all(repo_root, "add legacy symbol")
    _git(["mv", "legacy.py", "renamed.py"], repo_root)
    rename_sha = _commit_all(repo_root, "rename legacy symbol")
    (repo_root / "renamed.py").unlink()
    delete_sha = _commit_all(repo_root, "delete legacy symbol")
    return repo_root, rename_sha, delete_sha


def _create_delete_fixture(tmp_path: Path) -> tuple[Path, str]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(["init"], repo_root)
    _git(["config", "user.name", "Fixture Tester"], repo_root)
    _git(["config", "user.email", "fixture@example.com"], repo_root)
    (repo_root / "legacy.py").write_text(
        "class LegacyCheckout:\n" "    def process(self) -> int:\n" "        return 1\n",
        encoding="utf-8",
    )
    _commit_all(repo_root, "add legacy symbol")
    (repo_root / "legacy.py").unlink()
    delete_sha = _commit_all(repo_root, "delete legacy symbol")
    return repo_root, delete_sha


def _create_rename_fixture(tmp_path: Path) -> tuple[Path, str]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    _git(["init"], repo_root)
    _git(["config", "user.name", "Fixture Tester"], repo_root)
    _git(["config", "user.email", "fixture@example.com"], repo_root)
    (repo_root / "legacy.py").write_text(
        "class LegacyCheckout:\n" "    def process(self) -> int:\n" "        return 1\n",
        encoding="utf-8",
    )
    _commit_all(repo_root, "add legacy symbol")
    _git(["mv", "legacy.py", "renamed.py"], repo_root)
    rename_sha = _commit_all(repo_root, "rename legacy symbol")
    return repo_root, rename_sha


def test_walk_history_records_deleted_symbol_metadata(tmp_path: Path) -> None:
    repo_root, delete_sha = _create_delete_fixture(tmp_path)
    graveyard_module = importlib.import_module("atelier.infra.code_intel.git_history.graveyard")
    walker_module = importlib.import_module("atelier.infra.code_intel.git_history.walker")
    graveyard = graveyard_module.SymbolGraveyard(sqlite3.connect(":memory:"))

    walker_module.walk_history(repo_root, graveyard)
    entries = graveyard.find_deleted("LegacyCheckout", since_ts=None, language="python")

    assert len(entries) == 1
    entry = entries[0]
    assert entry.symbol_name == "LegacyCheckout"
    assert entry.deleted_at_sha == delete_sha
    assert entry.last_author == "fixture@example.com"
    assert entry.deleted_at_ts > 0


def test_walk_history_records_rename_target_instead_of_bare_deletion(tmp_path: Path) -> None:
    repo_root, rename_sha = _create_rename_fixture(tmp_path)
    graveyard_module = importlib.import_module("atelier.infra.code_intel.git_history.graveyard")
    walker_module = importlib.import_module("atelier.infra.code_intel.git_history.walker")
    graveyard = graveyard_module.SymbolGraveyard(sqlite3.connect(":memory:"))

    walker_module.walk_history(repo_root, graveyard)
    entries = graveyard.find_deleted("LegacyCheckout", since_ts=None, language="python")

    assert any(entry.deleted_at_sha == rename_sha and entry.rename_target == "renamed.py" for entry in entries)


def test_extract_tags_from_text_supports_deleted_blob_paths() -> None:
    tags_module = importlib.import_module("atelier.infra.tree_sitter.tags")

    tags = tags_module.extract_tags_from_text(
        "def deleted_only() -> int:\n    return 7\n",
        "deleted/history.py",
    )

    assert {tag.name for tag in tags if tag.kind == "definition"} == {"deleted_only"}


def test_walk_history_is_idempotent_for_repeated_ingestion(tmp_path: Path) -> None:
    repo_root, _rename_sha, _delete_sha = _create_history_fixture(tmp_path)
    graveyard_module = importlib.import_module("atelier.infra.code_intel.git_history.graveyard")
    walker_module = importlib.import_module("atelier.infra.code_intel.git_history.walker")
    graveyard = graveyard_module.SymbolGraveyard(sqlite3.connect(":memory:"))

    walker_module.walk_history(repo_root, graveyard)
    walker_module.walk_history(repo_root, graveyard)
    entries = graveyard.find_deleted("LegacyCheckout", since_ts=None, language="python")

    assert len(entries) == 2
    assert {entry.rename_target for entry in entries} == {None, "renamed.py"}
