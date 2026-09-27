"""A call queued behind the process-wide workspace lock is told who holds it."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from lemoncrow_server_core.dispatch import ToolInvocation
from lemoncrow_server_core.errors import ErrorCode, ServerError
from lemoncrow_server_core.registry_dispatch import PublicRegistryDispatcher, _workspace_binding, _WorkspaceBusy


def test_workspace_binding_gives_up_after_its_wait_budget(tmp_path: Path) -> None:
    lock = threading.Lock()
    with lock:
        started = time.monotonic()
        with pytest.raises(_WorkspaceBusy), _workspace_binding(tmp_path, lock, wait_s=0.2):
            pass
        assert 0.15 <= time.monotonic() - started < 2.0
    with _workspace_binding(tmp_path, lock, wait_s=0.2):  # free again: enters and releases
        pass
    assert not lock.locked()


def test_a_call_behind_a_slow_tool_gets_a_retryable_answer_naming_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LEMONCROW_SERVER_DISPATCH_WAIT_S", "0.3")
    entered = threading.Event()

    def slow(_request_id: object, _params: object) -> object:
        entered.set()
        time.sleep(2.0)
        raise RuntimeError("finished; the result does not matter to this test")

    module = SimpleNamespace(TOOLS={"verify": {}, "memory": {}}, _handle_tool_payload=slow)
    dispatcher = PublicRegistryDispatcher(module)

    def invocation(tool: str) -> ToolInvocation:
        tenant = SimpleNamespace(session_id="s", request_id="r")
        return ToolInvocation(tool=tool, arguments={}, tenant=tenant, workspace_root=str(tmp_path))  # type: ignore[arg-type]

    def hold() -> None:
        try:
            dispatcher.dispatch(invocation("verify"))
        except ServerError:
            pass

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(5)

    started = time.monotonic()
    with pytest.raises(ServerError) as caught:
        dispatcher.dispatch(invocation("memory"))
    assert time.monotonic() - started < 1.5
    assert caught.value.code is ErrorCode.CONCURRENCY_LIMIT and caught.value.retryable
    assert "'verify' has held it" in caught.value.message
    assert caught.value.details["busy_tool"] == "verify"
    holder.join(timeout=10)
