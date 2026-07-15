from __future__ import annotations

import fcntl
import os

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


def test_index_pool_does_not_fork_live_parent_state(monkeypatch) -> None:
    captured = {}

    class FakePool:
        def shutdown(self, **kwargs) -> None:
            pass

    def fake_process_pool_executor(**kwargs):
        captured.update(kwargs)
        return FakePool()

    monkeypatch.delenv("LEMONCROW_INDEX_POOL_CONTEXT", raising=False)
    monkeypatch.setattr(engine.concurrent.futures, "ProcessPoolExecutor", fake_process_pool_executor)
    monkeypatch.setattr(engine, "_PROCESS_POOL", None)

    engine._get_index_process_pool()

    assert captured["mp_context"].get_start_method() == "forkserver"


def test_force_never_unlinks_a_live_index_lock(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "code_context.sqlite"
    lock_path = tmp_path / "code_context.sqlite.indexlock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    original_inode = lock_path.stat().st_ino
    context_engine = engine.CodeContextEngine(tmp_path, db_path=db_path, autosync_enabled=False)
    monkeypatch.setattr(engine, "_index_lock_timeout_s", lambda: 0.0)

    try:
        with context_engine._index_write_lock(block=True, steal=True) as acquired:
            assert not acquired
        assert lock_path.stat().st_ino == original_inode
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def test_parallel_index_completes_with_isolated_workers(tmp_path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    for index in range(65):
        (repo_root / f"module_{index}.py").write_text(
            f"def function_{index}():\n    return {index}\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("LEMONCROW_INDEX_MAX_WORKERS", "2")
    monkeypatch.setenv("LEMONCROW_INDEX_SERIAL_MAX_FILES", "0")
    monkeypatch.delenv("LEMONCROW_INDEX_POOL_CONTEXT", raising=False)
    monkeypatch.setattr(engine, "_PROCESS_POOL", None)
    context_engine = engine.CodeContextEngine(
        repo_root,
        db_path=tmp_path / "code_context.sqlite",
        autosync_enabled=False,
    )

    try:
        stats = context_engine.index_repo(force=True, require_lock=True)
    finally:
        engine._shutdown_index_process_pool()

    assert stats.files_indexed == 65
    assert stats.symbols_indexed == 65
