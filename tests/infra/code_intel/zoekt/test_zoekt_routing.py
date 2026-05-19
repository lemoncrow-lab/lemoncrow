from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from atelier.core.capabilities.tool_supervision.smart_search import smart_search
from atelier.infra.code_intel.zoekt.adapter import reset_zoekt_supervisors
from atelier.infra.code_intel.zoekt.binary import discover_zoekt_binary
from atelier.infra.code_intel.zoekt.client import ZoektClient
from atelier.infra.code_intel.zoekt.server import get_zoekt_server


@pytest.fixture(autouse=True)
def _reset_supervisors() -> Iterator[None]:
    reset_zoekt_supervisors()
    yield
    reset_zoekt_supervisors()


def _write_fixture_repo(repo_root: Path) -> None:
    (repo_root / "src").mkdir(parents=True, exist_ok=True)
    (repo_root / "src" / "main.py").write_text(
        "def alpha() -> str:\n"
        "    return 'needle token'\n"
        "\n"
        "def beta() -> str:\n"
        "    return 'needle token again'\n",
        encoding="utf-8",
    )


def _write_large_repo(repo_root: Path, *, files: int = 24, lines_per_file: int = 24) -> None:
    (repo_root / "src").mkdir(parents=True, exist_ok=True)
    for index in range(files):
        payload = "".join(f"def item_{index}_{line}() -> str: return 'needle token {index}'\n" for line in range(lines_per_file))
        (repo_root / "src" / f"module_{index}.py").write_text(payload, encoding="utf-8")


def _write_fake_binary(root: Path) -> tuple[Path, str]:
    payload = b"#!/bin/sh\nexit 0\n"
    binary_path = root / "zoekt-webserver"
    binary_path.write_bytes(payload)
    binary_path.chmod(0o755)
    return binary_path, hashlib.sha256(payload).hexdigest()


def _configure_binary(monkeypatch: pytest.MonkeyPatch, binary_path: Path, sha256: str, repo_root: Path) -> None:
    monkeypatch.setenv("ATELIER_ZOEKT_BIN", str(binary_path))
    monkeypatch.setenv("ATELIER_ZOEKT_BIN_SHA256", sha256)
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(repo_root))


def test_zoekt_health_resolves_pinned_binary_and_serves_local_health(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _write_fixture_repo(repo_root)
    binary_path, sha256 = _write_fake_binary(tmp_path)
    _configure_binary(monkeypatch, binary_path, sha256, repo_root)

    resolution = discover_zoekt_binary(repo_root)

    assert resolution.available is True
    assert resolution.path == binary_path

    server = get_zoekt_server(repo_root, binary_path=binary_path)
    health = server.health()

    assert health.ok is True
    assert health.backend == "zoekt"
    assert health.binary_path == str(binary_path)


def test_zoekt_managed_bootstrap_provisions_manifest_when_env_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _write_fixture_repo(repo_root)
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(repo_root))
    monkeypatch.delenv("ATELIER_ZOEKT_BIN", raising=False)
    monkeypatch.delenv("ATELIER_ZOEKT_BIN_SHA256", raising=False)

    resolution = discover_zoekt_binary(repo_root)

    assert resolution.available is True
    assert resolution.source == "managed"
    assert resolution.path is not None
    assert resolution.path.exists()
    manifest = repo_root / ".atelier" / "bin" / "MANIFEST.json"
    assert manifest.exists()
    payload = manifest.read_text(encoding="utf-8")
    assert "zoekt-webserver" in payload


def test_zoekt_lifecycle_reuses_one_server_per_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    _write_fixture_repo(repo_root)
    binary_path, sha256 = _write_fake_binary(tmp_path)
    _configure_binary(monkeypatch, binary_path, sha256, repo_root)

    first = get_zoekt_server(repo_root, binary_path=binary_path)
    second = get_zoekt_server(repo_root, binary_path=binary_path)

    first.ensure_started()
    second.ensure_started()

    assert first is second
    assert first.start_count == 1


def test_zoekt_byte_range_client_preserves_offsets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    _write_fixture_repo(repo_root)
    binary_path, sha256 = _write_fake_binary(tmp_path)
    _configure_binary(monkeypatch, binary_path, sha256, repo_root)

    server = get_zoekt_server(repo_root, binary_path=binary_path)
    client = ZoektClient(server.ensure_started())

    matches = client.search("needle token")

    assert matches
    first_match = matches[0].matches[0]
    source = (repo_root / "src" / "main.py").read_bytes()
    assert first_match.byte_start < first_match.byte_end
    assert source[first_match.byte_start : first_match.byte_end] == b"needle token"


def test_zoekt_search_routes_large_repos_with_backend_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _write_large_repo(repo_root)
    binary_path, sha256 = _write_fake_binary(tmp_path)
    _configure_binary(monkeypatch, binary_path, sha256, repo_root)
    monkeypatch.setenv("ATELIER_ZOEKT_LOC_THRESHOLD", "20")

    payload = smart_search(query="needle token", path=str(repo_root), max_files=5, budget_tokens=4000)

    assert payload["backend"] == "zoekt"
    assert isinstance(payload["index_age_seconds"], int)
    assert payload["matches"]


def test_zoekt_search_falls_back_for_small_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    _write_fixture_repo(repo_root)
    binary_path, sha256 = _write_fake_binary(tmp_path)
    _configure_binary(monkeypatch, binary_path, sha256, repo_root)
    monkeypatch.setenv("ATELIER_ZOEKT_LOC_THRESHOLD", "10000")

    payload = smart_search(query="needle token", path=str(repo_root), max_files=5, budget_tokens=4000)

    assert payload["backend"] == "ripgrep"
    assert payload["index_age_seconds"] is None


def test_zoekt_search_falls_back_when_backend_is_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _write_large_repo(repo_root)
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(repo_root))
    monkeypatch.setenv("ATELIER_ZOEKT_LOC_THRESHOLD", "20")
    monkeypatch.setenv("ATELIER_ZOEKT_BIN", str(tmp_path / "missing-zoekt-webserver"))
    monkeypatch.setenv("ATELIER_ZOEKT_BIN_SHA256", "deadbeef")

    payload = smart_search(query="needle token", path=str(repo_root), max_files=5, budget_tokens=4000)

    assert payload["backend"] == "ripgrep"
    assert payload["matches"]


def test_zoekt_search_routes_large_repos_with_managed_bootstrap_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _write_large_repo(repo_root)
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(repo_root))
    monkeypatch.setenv("ATELIER_ZOEKT_LOC_THRESHOLD", "20")
    monkeypatch.delenv("ATELIER_ZOEKT_BIN", raising=False)
    monkeypatch.delenv("ATELIER_ZOEKT_BIN_SHA256", raising=False)

    payload = smart_search(query="needle token", path=str(repo_root), max_files=5, budget_tokens=4000)

    assert payload["backend"] == "zoekt"
    assert isinstance(payload["index_age_seconds"], int)
    assert payload["matches"]


def test_zoekt_search_keeps_backend_metadata_on_warm_repeat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    _write_large_repo(repo_root)
    binary_path, sha256 = _write_fake_binary(tmp_path)
    _configure_binary(monkeypatch, binary_path, sha256, repo_root)
    monkeypatch.setenv("ATELIER_ZOEKT_LOC_THRESHOLD", "20")

    first = smart_search(query="needle token", path=str(repo_root), max_files=5, budget_tokens=4000)
    second = smart_search(query="needle token", path=str(repo_root), max_files=5, budget_tokens=4000)

    assert first["backend"] == "zoekt"
    assert second["backend"] == "zoekt"
    assert second["cache_hit"] is True
    assert isinstance(second["index_age_seconds"], int)
