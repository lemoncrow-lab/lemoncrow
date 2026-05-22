from __future__ import annotations

from pathlib import Path

import pytest

from atelier.core.environment import resolve_memory_backend


def test_resolve_memory_backend_defaults_to_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_MEMORY_BACKEND", raising=False)
    assert resolve_memory_backend(root=tmp_path) == "sqlite"


def test_resolve_memory_backend_prefers_env_over_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "config.toml").write_text('[memory]\nbackend = "letta"\n', encoding="utf-8")
    monkeypatch.setenv("ATELIER_MEMORY_BACKEND", "openmemory")
    assert resolve_memory_backend(root=tmp_path) == "openmemory"


def test_resolve_memory_backend_reads_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_MEMORY_BACKEND", raising=False)
    (tmp_path / "config.toml").write_text('[memory]\nbackend = "letta"\n', encoding="utf-8")
    assert resolve_memory_backend(root=tmp_path) == "letta"


def test_resolve_memory_backend_rejects_invalid_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_MEMORY_BACKEND", "bogus")
    with pytest.raises(ValueError, match="memory backend must be one of"):
        resolve_memory_backend()
