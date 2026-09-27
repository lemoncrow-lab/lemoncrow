"""One real call through the production stack: thin client -> loopback server -> index.

Proves the R2 contract on the shipped composition (``TracingDispatcher`` around
``IndexSearchDispatcher``), not on a stub: a single ``lt_`` id links the client
record and the server record, and the server record carries the ranked
candidate set that trace replay needs.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path

import pytest
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

try:  # the thin client is a workspace package; skip cleanly if absent
    from lemoncrow_client.config import load_config
    from lemoncrow_client.dispatcher import Dispatcher
    from lemoncrow_client.session import RemoteSession
except ImportError:  # pragma: no cover
    pytest.skip("lemoncrow_client not importable", allow_module_level=True)

from lemoncrow.core.capabilities.trace_replay import assemble_calls, build_cases
from lemoncrow.core.foundation.runtime_trace import TraceStore


class _Loop:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    def run(self, coro):  # type: ignore[no-untyped-def]
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout=60)

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(timeout=5)


def test_one_lt_id_links_client_and_server_records_and_captures_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "lemoncrow-root"  # the server's runtime trace store
    home = tmp_path / "client-home"  # the thin client's state dir
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.delenv("LEMONCROW_RUNTIME_TRACE", raising=False)
    worktree = tmp_path / "repo"
    (worktree / "pkg").mkdir(parents=True)
    (worktree / "pkg" / "review_auth.py").write_text(
        "def authorize_review(user, review):\n    return user.id == review.owner\n"
    )
    (worktree / "pkg" / "routes.py").write_text("from pkg.review_auth import authorize_review\n\nX = 1\n")
    (worktree / "pkg" / "other.py").write_text("def unrelated():\n    return 2\n")
    subprocess.run(["git", "init", "-q"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.email", "trace@example.test"], cwd=worktree, check=True)
    subprocess.run(["git", "config", "user.name", "Trace Test"], cwd=worktree, check=True)
    subprocess.run(["git", "add", "."], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=worktree, check=True)

    server, _state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            local_no_auth=True,
            allow_local_fs=False,
            ephemeral=True,
        )
    )
    loop = _Loop()
    url = loop.run(server.start())
    try:
        config = load_config(
            {
                "LEMONCROW_URL": url,
                "LEMONCROW_HOME": str(home),
                "HOME": str(tmp_path),
                "LEMONCROW_INSTALL_MODE": "local",
            },
            cwd=worktree,
        )
        session = RemoteSession(config)
        report = session.bootstrap(deadline_s=60.0)
        assert report.ok, report.reason
        dispatcher = Dispatcher(config, session)
        found = dispatcher.call("code_search", {"query": "authorize_review"})
        local = dispatcher.call("bash", {"command": "printf ok"})
        assert not found.is_error, found.content
    finally:
        loop.run(server.stop())
        loop.close()

    trace_id = found.trace_id
    assert trace_id.startswith("lt_")

    client_records = TraceStore(home).find(trace_id)
    server_records = TraceStore(root).find(trace_id)
    assert [r["writer"] for r in client_records] == ["client"]
    assert [r["writer"] for r in server_records] == ["server"]  # one server record, no duplicate from the runtime
    assert client_records[0]["execution"]["site"] == "server"

    # Local tools are traced by the client under their own id, never sent to the server.
    assert [r["writer"] for r in TraceStore(home).find(local.trace_id)] == ["client"]
    assert TraceStore(root).find(local.trace_id) == []

    record = server_records[0]
    retrieval = record["retrieval"]
    assert retrieval["query"] == "authorize_review" and retrieval["ranker"] == "heuristic"
    assert retrieval["candidates"][0]["path"] == "pkg/review_auth.py"
    assert retrieval["candidates"][0]["symbols"] == ["authorize_review"]
    assert retrieval["selected"][0] == "pkg/review_auth.py"
    # Native code_search is now authoritative from the revision-bound server
    # index and intentionally skips the filesystem context-engine shadow path.
    assert retrieval["channel"] == "server_index"
    assert record["decisions"] == []
    assert "context_engine" not in retrieval.get("extra", {})

    # The merged call is replayable: client + server layers, one case.
    calls = assemble_calls([*client_records, *server_records])
    assert len(calls) == 1 and sorted(calls[0].layers) == ["client", "server"]
    assert len(build_cases(calls)) == 1
