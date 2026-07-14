from __future__ import annotations

from lemoncrow.pro.capabilities.code_context import engine


def test_explicit_index_defaults_to_full_cpu(monkeypatch) -> None:
    monkeypatch.delenv("LEMONCROW_INDEX_MAX_WORKERS", raising=False)
    monkeypatch.setattr(engine.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(engine, "_available_memory_mb", lambda: None)

    assert engine._resolve_index_max_workers() == 8


def test_autosync_index_defaults_to_half_cpu(monkeypatch) -> None:
    monkeypatch.delenv("LEMONCROW_AUTOSYNC_INDEX_MAX_WORKERS", raising=False)
    monkeypatch.setattr(engine.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(engine, "_available_memory_mb", lambda: None)

    assert engine._resolve_autosync_index_max_workers() == 4


def test_index_worker_overrides_are_honored(monkeypatch) -> None:
    monkeypatch.setenv("LEMONCROW_INDEX_MAX_WORKERS", "3")
    monkeypatch.setenv("LEMONCROW_AUTOSYNC_INDEX_MAX_WORKERS", "2")
    monkeypatch.setattr(engine.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(engine, "_available_memory_mb", lambda: None)

    assert engine._resolve_index_max_workers() == 3
    assert engine._resolve_autosync_index_max_workers() == 2
