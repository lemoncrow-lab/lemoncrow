"""The Review worker's collector settings are applied by the process that serves the Reader, in order."""

from __future__ import annotations

import asyncio
import gc
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web
from lemoncrow_server_core import service_host
from lemoncrow_server_core.service_protocol import CORE_URL_ENV, SERVICE_DASHBOARD, SERVICE_REVIEW, SERVICE_TOKEN_ENV


class _NoMaintenance:
    def run_once(self) -> None:
        return None


def _serve(name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, Any]]:
    """Run serve_service until its warm-up has finished, recording the collector calls it made."""

    events: list[tuple[str, Any]] = []
    stop: list[Callable[[], None]] = []

    def warm() -> None:
        events.append(("warm", gc.get_threshold()))

    def build(*_args: object, **_kwargs: object) -> tuple[web.Application, Any, Callable[[], None]]:
        return web.Application(), _NoMaintenance(), warm

    def freeze() -> None:
        events.append(("freeze", None))
        stop[0]()

    monkeypatch.setenv(SERVICE_TOKEN_ENV, "token")
    monkeypatch.setenv(CORE_URL_ENV, "http://127.0.0.1:9")
    monkeypatch.setattr(service_host, "build_service_app", build)
    monkeypatch.setattr(service_host, "_watch_stdin", stop.append)
    monkeypatch.setattr(service_host.gc, "freeze", freeze)
    # The worker hands its stdout to stderr once READY is written; the test process keeps its own.
    monkeypatch.setattr(service_host.os, "dup2", lambda *_args: None)

    async def run() -> int:
        task = asyncio.create_task(
            service_host.serve_service(
                name,
                state_root=tmp_path,
                review_root=tmp_path / "reviews",
                public_port=0,
                max_request_bytes=1024,
                maintenance_interval_s=0,
            )
        )
        if name != SERVICE_REVIEW:
            # Nothing freezes a non-Review worker, so its warm-up alone ends the run.
            while not events:
                await asyncio.sleep(0.01)
            stop[0]()
        return await asyncio.wait_for(task, timeout=30)

    before = gc.get_threshold()
    try:
        assert asyncio.run(run()) == 0
    finally:
        gc.set_threshold(*before)
    return events


def test_the_review_worker_collects_less_often_and_freezes_its_startup_heap_after_warm_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    events = _serve(SERVICE_REVIEW, tmp_path, monkeypatch)

    # Thresholds are in place before the warm-up imports anything, and the freeze follows it.
    assert events == [("warm", service_host._REVIEW_GC_THRESHOLD), ("freeze", None)]


def test_a_dashboard_worker_keeps_the_default_collector(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    before = gc.get_threshold()
    events = _serve(SERVICE_DASHBOARD, tmp_path, monkeypatch)

    assert events == [("warm", before)]
