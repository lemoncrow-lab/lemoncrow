from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from atelier.infra.code_intel.zoekt.binary import discover_zoekt_binary
from atelier.infra.code_intel.zoekt.client import ZoektClient
from atelier.infra.code_intel.zoekt.server import get_zoekt_server, reset_zoekt_servers


@pytest.fixture(autouse=True)
def _reset_supervisors() -> None:
    reset_zoekt_servers()
    yield
    reset_zoekt_servers()


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
