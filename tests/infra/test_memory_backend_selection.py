from __future__ import annotations

from pathlib import Path

import pytest

from atelier.infra.storage.factory import make_memory_store
from atelier.infra.storage.sqlite_memory_store import SqliteMemoryStore


def test_memory_store_defaults_to_sqlite(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_MEMORY_BACKEND", raising=False)
    store = make_memory_store(tmp_path / "atelier")
    assert isinstance(store, SqliteMemoryStore)


def test_memory_backend_env_overrides_preference(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_MEMORY_BACKEND", "sqlite")
    store = make_memory_store(tmp_path / "atelier", prefer="letta")
    assert isinstance(store, SqliteMemoryStore)


def test_memory_backend_config_selects_letta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "atelier"
    root.mkdir()
    (root / "config.toml").write_text('[memory]\nbackend = "letta"\n', encoding="utf-8")
    monkeypatch.delenv("ATELIER_MEMORY_BACKEND", raising=False)

    class FakeLettaMemoryStore:
        def __init__(self, root_arg: Path) -> None:
            self.root_arg = root_arg

    monkeypatch.setattr(
        "atelier.infra.memory_bridges.letta_adapter.LettaMemoryStore",
        FakeLettaMemoryStore,
    )

    store = make_memory_store(root)
    assert isinstance(store, FakeLettaMemoryStore)
    assert store.root_arg == root


def test_memory_backend_env_selects_openmemory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_MEMORY_BACKEND", "openmemory")

    class FakeOpenMemoryMemoryStore:
        def __init__(self, root_arg: Path) -> None:
            self.root_arg = root_arg

    monkeypatch.setattr(
        "atelier.infra.memory_bridges.openmemory.OpenMemoryMemoryStore",
        FakeOpenMemoryMemoryStore,
    )

    store = make_memory_store(tmp_path / "atelier")
    assert isinstance(store, FakeOpenMemoryMemoryStore)


def test_unknown_memory_backend_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_MEMORY_BACKEND", "bogus")
    with pytest.raises(ValueError, match=r"letta.*openmemory.*sqlite"):
        make_memory_store(tmp_path / "atelier")


def test_invalid_config_toml_falls_back_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Invalid config.toml must fall back to the default backend and emit a warning."""
    root = tmp_path / "atelier"
    root.mkdir()
    # Malformed TOML triggers tomllib.TOMLDecodeError inside resolve_memory_backend.
    (root / "config.toml").write_text("[memory]\nbackend = \n", encoding="utf-8")
    monkeypatch.delenv("ATELIER_MEMORY_BACKEND", raising=False)

    with caplog.at_level("WARNING", logger="atelier.core.environment"):
        store = make_memory_store(root)

    assert isinstance(store, SqliteMemoryStore)
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("falling back to defaults" in r.getMessage() for r in warnings)
    # Warning carries exception info for observability.
    assert any(r.exc_info is not None for r in warnings)
