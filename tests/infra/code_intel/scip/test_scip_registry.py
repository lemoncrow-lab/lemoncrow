from __future__ import annotations

import subprocess
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from atelier.infra.code_intel.languages import language_by_name
from atelier.infra.code_intel.scip import bootstrap
from atelier.infra.code_intel.scip.binaries import (
    discover_scip_binaries,
    discover_scip_binary,
    managed_scip_binary_dirs,
    scip_binary_spec,
    scip_binary_specs,
)
from atelier.infra.code_intel.scip.bootstrap import ScipLazyFetchSpec, ensure_scip_binary
from atelier.infra.code_intel.scip.indexer import ScipIndexer, default_scip_cache_root
from atelier.infra.code_intel.scip.watcher import resolve_git_repo_state

REPO_ROOT = Path(__file__).resolve().parents[4]


def _fake_executable(path: Path) -> Path:
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize(
    ("language", "env_var", "fallback"),
    [
        ("python", "ATELIER_SCIP_PYTHON_BIN", "scip-python"),
        ("typescript", "ATELIER_SCIP_TYPESCRIPT_BIN", "scip-typescript"),
        ("javascript", "ATELIER_SCIP_TYPESCRIPT_BIN", "scip-typescript"),
        ("go", "ATELIER_SCIP_GO_BIN", "scip-go"),
        ("rust", "ATELIER_SCIP_RUST_BIN", "rust-analyzer"),
        ("java", "ATELIER_SCIP_JAVA_BIN", "scip-java"),
        ("ruby", "ATELIER_SCIP_RUBY_BIN", "scip-ruby"),
        ("c", "ATELIER_SCIP_CLANG_BIN", "scip-clang"),
        ("cpp", "ATELIER_SCIP_CLANG_BIN", "scip-clang"),
    ],
)
def test_scip_registry_env_vars_and_fallbacks(language: str, env_var: str, fallback: str) -> None:
    spec = scip_binary_spec(language)

    assert spec is not None
    assert spec.env_var == env_var
    assert spec.fallback_command == fallback
    assert language_by_name(language).scip_indexer == fallback


def test_rust_uses_rust_analyzer_binary_with_scip_subcommand(tmp_path: Path) -> None:
    spec = scip_binary_spec("rust")
    assert spec is not None

    command = spec.command(tmp_path / "rust-analyzer", tmp_path / "rust.scip", tmp_path)

    assert command == [str(tmp_path / "rust-analyzer"), "scip", str(tmp_path)]


def test_discover_scip_binary_prefers_explicit_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = _fake_executable(tmp_path / "custom-scip-go")
    monkeypatch.setenv("ATELIER_SCIP_GO_BIN", str(fake_bin))

    assert discover_scip_binary("go") == fake_bin.resolve()


def test_discover_scip_binary_prefers_managed_dir_before_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    managed = tmp_path / "node" / "bin"
    system = tmp_path / "system"
    managed.mkdir(parents=True)
    system.mkdir()
    managed_bin = _fake_executable(managed / "scip-python")
    _fake_executable(system / "scip-python")
    monkeypatch.setenv("ATELIER_NODE_DIR", str(tmp_path / "node"))
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path / "atelier"))
    monkeypatch.setenv("PATH", str(system))
    monkeypatch.delenv("ATELIER_SCIP_PYTHON_BIN", raising=False)

    assert managed_scip_binary_dirs()[0] == managed.resolve()
    assert discover_scip_binary("python") == managed_bin.resolve()


def test_discover_scip_binaries_iterates_supported_specs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for command in {
        "scip-python",
        "scip-typescript",
        "scip-go",
        "rust-analyzer",
        "scip-java",
        "scip-ruby",
        "scip-clang",
    }:
        _fake_executable(bin_dir / command)
    monkeypatch.setenv("PATH", str(bin_dir))
    for spec in scip_binary_specs().values():
        monkeypatch.delenv(spec.env_var, raising=False)

    discovered = discover_scip_binaries()

    assert set(discovered) == set(scip_binary_specs())
    assert discovered["c"] == discovered["cpp"]


def test_install_script_installs_tier1_scip_npm_packages() -> None:
    install_script = (REPO_ROOT / "scripts" / "install.sh").read_text(encoding="utf-8")
    npm_install_line = next(
        line for line in install_script.splitlines() if 'npm install -g --prefix "$ATELIER_NODE_DIR"' in line
    )

    assert "@sourcegraph/scip-python" in npm_install_line
    assert "@sourcegraph/scip-typescript" in npm_install_line


def test_tier2_bootstrap_fails_closed_without_checksum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("ATELIER_SCIP_GO_BIN", raising=False)

    result = ensure_scip_binary("go")

    assert result.status == "bootstrap_unavailable"
    assert result.binary is None
    assert "checksum" in result.message


def test_tier2_bootstrap_fetches_checksum_verified_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = tmp_path / "scip-go-download"
    payload.write_bytes(b"fake scip-go")
    monkeypatch.setenv("ATELIER_ROOT", str(tmp_path / "root"))
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("ATELIER_SCIP_GO_BIN", raising=False)
    monkeypatch.setitem(
        bootstrap._LAZY_BOOTSTRAP_FETCHES,
        "go",
        ScipLazyFetchSpec(url=payload.as_uri(), sha256=sha256(payload.read_bytes()).hexdigest()),
    )

    result = ensure_scip_binary("go")

    assert result.status == "ready"
    assert result.binary == (tmp_path / "root" / "bin" / "scip-go").resolve()
    assert result.binary.read_bytes() == b"fake scip-go"
    assert result.binary.stat().st_mode & 0o111


def test_tier3_bootstrap_reports_user_toolchain_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("ATELIER_SCIP_RUST_BIN", raising=False)

    result = ensure_scip_binary("rust")

    assert result.status == "user_toolchain_required"
    assert "rust-analyzer" in result.install_hint


def test_index_language_reports_missing_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atelier.infra.code_intel.scip.indexer.ensure_scip_binary",
        lambda language: ensure_scip_binary("python").model_copy(update={"binary": None, "status": "missing_binary"}),
    )
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("ATELIER_SCIP_PYTHON_BIN", raising=False)

    result = ScipIndexer(tmp_path, "repo").index_language("python")

    assert result.status == "missing_binary"
    assert result.artifact_path is None


def test_index_language_reports_tier2_bootstrap_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("ATELIER_SCIP_GO_BIN", raising=False)

    result = ScipIndexer(tmp_path, "repo").index_language("go")

    assert result.status == "bootstrap_unavailable"
    assert "checksum" in result.message


def test_index_language_skips_missing_clang_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = _fake_executable(tmp_path / "scip-clang")
    monkeypatch.setattr(
        "atelier.infra.code_intel.scip.indexer.ensure_scip_binary",
        lambda language: ensure_scip_binary("python").model_copy(update={"binary": fake_bin, "status": "ready"}),
    )

    result = ScipIndexer(tmp_path, "repo").index_language("c")

    assert result.status == "missing_context"
    assert "compile_commands.json" in result.message


def test_index_language_success_is_discoverable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = _fake_executable(tmp_path / "scip-python")
    monkeypatch.setattr(
        "atelier.infra.code_intel.scip.indexer.ensure_scip_binary",
        lambda language: ensure_scip_binary("python").model_copy(update={"binary": fake_bin, "status": "ready"}),
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        output_path = Path(command[-1])
        output_path.write_text("fake scip", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("atelier.infra.code_intel.scip.indexer.subprocess.run", fake_run)

    indexer = ScipIndexer(tmp_path, "repo")
    result = indexer.index_language("python")

    assert result.status == "indexed"
    assert result.artifact_path == (default_scip_cache_root(tmp_path, "repo") / "python.scip").resolve()
    assert [artifact.path for artifact in indexer.discover_artifacts()] == [result.artifact_path]


def test_index_language_normalizes_rust_directory_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = _fake_executable(tmp_path / "rust-analyzer")
    monkeypatch.setattr(
        "atelier.infra.code_intel.scip.indexer.ensure_scip_binary",
        lambda language: ensure_scip_binary("rust").model_copy(update={"binary": fake_bin, "status": "ready"}),
    )

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        output_dir = Path(command[-1])
        (output_dir / "index.scip").write_text("fake scip", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("atelier.infra.code_intel.scip.indexer.subprocess.run", fake_run)

    result = ScipIndexer(tmp_path, "repo").index_language("rust")

    assert result.status == "indexed"
    assert result.artifact_path == (default_scip_cache_root(tmp_path, "repo") / "rust.scip").resolve()


def test_default_scip_cache_root_uses_branch_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atelier.infra.code_intel.scip.indexer.resolve_git_repo_state",
        lambda repo_root: SimpleNamespace(branch_key="refs-heads-main-abc123"),
    )

    assert default_scip_cache_root(tmp_path, "repo") == (
        tmp_path / ".atelier" / "cache" / "scip" / "repo" / "refs-heads-main-abc123"
    )


def test_resolve_git_repo_state_handles_worktree_gitdir_files(tmp_path: Path) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    git_dir = tmp_path / "worktrees" / "repo"
    common_dir = tmp_path / "common.git"
    (git_dir / "refs" / "heads").mkdir(parents=True)
    (common_dir / "refs" / "heads").mkdir(parents=True)
    (repo_root / ".git").write_text(f"gitdir: {git_dir}\n", encoding="utf-8")
    (git_dir / "commondir").write_text(str(common_dir), encoding="utf-8")
    (git_dir / "HEAD").write_text("ref: refs/heads/feature\n", encoding="utf-8")
    (common_dir / "refs" / "heads" / "feature").write_text("f" * 40 + "\n", encoding="utf-8")

    state = resolve_git_repo_state(repo_root)

    assert state.git_dir == git_dir.resolve()
    assert state.common_dir == common_dir.resolve()
    assert state.head_ref == "refs/heads/feature"
    assert state.head_sha == "f" * 40
    assert state.branch_key.startswith("refs-heads-feature-")


def test_index_language_reports_subprocess_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_bin = _fake_executable(tmp_path / "scip-python")
    monkeypatch.setattr(
        "atelier.infra.code_intel.scip.indexer.ensure_scip_binary",
        lambda language: ensure_scip_binary("python").model_copy(update={"binary": fake_bin, "status": "ready"}),
    )
    monkeypatch.setattr(
        "atelier.infra.code_intel.scip.indexer.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 2, stdout="", stderr="boom"),
    )

    result = ScipIndexer(tmp_path, "repo").index_language("python")

    assert result.status == "failed"
    assert result.message == "boom"
