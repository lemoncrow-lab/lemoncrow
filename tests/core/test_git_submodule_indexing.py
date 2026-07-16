"""Regression tests: git-tracked file discovery must recurse into submodules.

Covers the four independent ``git ls-files`` call sites that previously
listed a submodule only as its bare gitlink entry (mode 160000) rather than
descending into its tracked files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lemoncrow.core.service.code_map import _tracked_files
from lemoncrow.gateway.cli.commands.project import _get_files
from lemoncrow.infra.code_intel.zoekt.indexer import ZoektIndexer
from lemoncrow.pro.capabilities.repo_map.graph import iter_source_files

_GIT_ENV_ARGS = ["-c", "user.email=test@example.com", "-c", "user.name=Test"]


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *_GIT_ENV_ARGS, *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo_with_submodule(tmp_path: Path) -> Path:
    sub_origin = tmp_path / "sub_origin"
    sub_origin.mkdir()
    _git(["init", "-q"], sub_origin)
    (sub_origin / "vendor.py").write_text("def vendored() -> None:\n    pass\n", encoding="utf-8")
    _git(["add", "vendor.py"], sub_origin)
    _git(["commit", "-q", "-m", "vendor init"], sub_origin)

    parent = tmp_path / "parent"
    parent.mkdir()
    _git(["init", "-q"], parent)
    (parent / "app.py").write_text("def main() -> None:\n    pass\n", encoding="utf-8")
    _git(["add", "app.py"], parent)
    _git(["commit", "-q", "-m", "app init"], parent)
    subprocess.run(
        ["git", *_GIT_ENV_ARGS, "-c", "protocol.file.allow=always", "submodule", "add", str(sub_origin), "vendor"],
        cwd=parent,
        check=True,
        capture_output=True,
    )
    _git(["commit", "-q", "-m", "add submodule"], parent)
    return parent


def test_iter_source_files_recurses_into_submodules(repo_with_submodule: Path) -> None:
    files = {p.relative_to(repo_with_submodule).as_posix() for p in iter_source_files(repo_with_submodule)}
    assert "vendor/vendor.py" in files
    assert "app.py" in files


def test_zoekt_indexer_recurses_into_submodules(repo_with_submodule: Path) -> None:
    indexer = ZoektIndexer(repo_with_submodule)
    tracked = indexer._git_tracked_text_files()
    assert "vendor/vendor.py" in tracked
    assert "app.py" in tracked


def test_code_map_tracked_files_recurses_into_submodules(repo_with_submodule: Path) -> None:
    tracked = _tracked_files(repo_with_submodule)
    assert "vendor/vendor.py" in tracked
    assert "app.py" in tracked


def test_cli_get_files_recurses_into_submodules(repo_with_submodule: Path) -> None:
    files = {p.relative_to(repo_with_submodule).as_posix() for p in _get_files(repo_with_submodule)}
    assert "vendor/vendor.py" in files
    assert "app.py" in files
