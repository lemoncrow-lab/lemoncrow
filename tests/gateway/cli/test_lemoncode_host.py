from __future__ import annotations

import os
from pathlib import Path

import pytest
from click import ClickException

from lemoncrow.gateway.cli.lemoncode_host import _asset_name, host_binary_path, resolve_host_binary


def test_release_asset_mapping() -> None:
    assert _asset_name("linux", "x86_64") == "lemoncode-linux-x64.tar.gz"
    assert _asset_name("darwin", "arm64") == "lemoncode-darwin-arm64.tar.gz"


def test_release_asset_rejects_unsupported_platform() -> None:
    with pytest.raises(ClickException):
        _asset_name("windows", "x86_64")


def test_explicit_host_binary_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "custom-host"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setenv("LEMONCODE_HOST_BIN", str(binary))
    assert resolve_host_binary(tmp_path / "store") == str(binary.resolve())


def test_managed_install_is_resolved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCODE_HOST_BIN", raising=False)
    binary = host_binary_path(tmp_path)
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    assert resolve_host_binary(tmp_path) == str(binary)
    assert os.access(binary, os.X_OK)
