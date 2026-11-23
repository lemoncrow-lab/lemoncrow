"""The production aiohttp worker: startup recovery, per-repository isolation, clean shutdown."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from lemoncrow_server_core import local_reviews, service_host
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.core_client import CoreClient
from lemoncrow_server_core.local_reviews import LocalReviewReader
from lemoncrow_server_core.service_protocol import SERVICE_REVIEW


class _Store:
    def __init__(self, due: tuple[str, ...] = ()) -> None:
        self.recovered: list[str] = []
        self.due = due

    def recover_stale_review_flow_sends(self, *, before: str) -> int:
        self.recovered.append(before)
        return 1

    def list_due_review_flow_ids(self, *, now: str) -> tuple[str, ...]:
        due, self.due = self.due, ()
        return due


class _Repository:
    def __init__(self, stores: dict[str, Any]) -> None:
        self.stores = stores

    def list_repository_ids(self) -> list[str]:
        return list(self.stores)

    def store(self, repo_id: str) -> Any:
        store = self.stores[repo_id]
        if isinstance(store, Exception):
            raise store
        return store

    def set_landing_revalidator(self, fn: Any) -> None: ...

    def set_merge_recorder(self, fn: Any) -> None: ...


def _reader(stores: dict[str, Any]) -> LocalReviewReader:
    return LocalReviewReader(_Repository(stores), listen_port=7420, ask_enabled=False)  # type: ignore[arg-type]


def test_worker_recovers_stale_rows_per_repository_even_when_one_fails() -> None:
    good = _Store()

    async def run() -> None:
        reader = _reader({"broken": RuntimeError("no store"), "good": good})
        await reader.start_flow_send_worker()
        await asyncio.sleep(0.2)
        await reader.stop_flow_send_worker()

    asyncio.run(run())
    assert len(good.recovered) == 1


def test_stop_waits_for_the_in_flight_dispatch(monkeypatch: Any) -> None:
    release = threading.Event()
    started = threading.Event()
    finished: list[bool] = []

    def dispatch(app: Any) -> None:
        started.set()
        release.wait(5)
        finished.append(True)

    store = _Store(due=("review-1",))
    reader = _reader({"repo": store})
    monkeypatch.setattr(LocalReviewReader, "_app", lambda self, resolved: SimpleNamespace())
    monkeypatch.setattr(LocalReviewReader, "_dispatch_flow_send_sync", staticmethod(dispatch))
    monkeypatch.setattr(local_reviews, "_ResolvedReader", lambda repo_id, st, rid: (repo_id, rid))

    async def run() -> bool:
        await reader.start_flow_send_worker()
        while not started.is_set():
            await asyncio.sleep(0.02)
        stopper = asyncio.create_task(reader.stop_flow_send_worker())
        await asyncio.sleep(0.2)
        waited = not stopper.done()  # still waiting on the running dispatch
        release.set()
        await stopper
        return waited

    assert asyncio.run(run()) is True
    assert finished == [True]


def _count_workers(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    async def start(self: LocalReviewReader) -> None:
        calls.append("start")

    async def stop(self: LocalReviewReader) -> None:
        calls.append("stop")

    monkeypatch.setattr(LocalReviewReader, "start_flow_send_worker", start)
    monkeypatch.setattr(LocalReviewReader, "stop_flow_send_worker", stop)
    return calls


def test_the_isolated_review_worker_dispatches_delayed_sends_beside_the_reader_api(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A send's signing state is per process, so the process answering Done's record read must be the dispatcher."""

    calls = _count_workers(monkeypatch)
    app, _maintenance, _warm = service_host.build_service_app(
        SERVICE_REVIEW,
        token="token",
        core=CoreClient("http://127.0.0.1:9", "token"),
        state_root=tmp_path,
        review_root=tmp_path / "reviews",
        public_port=7420,
        max_request_bytes=1024,
    )

    async def run() -> None:
        runner = web.AppRunner(app)
        await runner.setup()
        assert calls == ["start"]
        await runner.cleanup()

    asyncio.run(run())
    assert calls == ["start", "stop"]


@pytest.mark.parametrize("isolated", [True, False])
def test_the_core_dispatches_delayed_sends_only_when_it_serves_the_reader_api_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated: bool
) -> None:
    calls = _count_workers(monkeypatch)
    _server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            local_no_auth=True,
            allow_local_fs=False,
            isolate_services=isolated,
            products=frozenset({"review"}),
        )
    )
    try:
        asyncio.run(state._review_flow_startup(web.Application()))
        assert calls == ([] if isolated else ["start"])
    finally:
        if state.workspace is not None:
            state.workspace.close()
        state.backend.close_backend()
