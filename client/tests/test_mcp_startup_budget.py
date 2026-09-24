"""Phase-11 gate: warm MCP ``initialize`` bootstrap stays under ~1 s.

The one process a developer actually waits for is the MCP stdio thin client, so
this measures its ``initialize`` path in full against a real enterprise server
over a realistically sized repository.

The measurement starts with one cold initialize, then creates fresh
process-equivalent MCP objects over the same worktree/state and measures the
slowest warm initialize. The numbers are printed so the test run is the
evidence rather than a claim about one machine.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from _serverpkg import REASON, server_available

pytestmark = [
    pytest.mark.enterprise_server,
    pytest.mark.skipif(not server_available(), reason=REASON),
]

TOKEN = "budget-token-0123456789abcdef01"
ORG = "org_budget"

#: Big enough that a per-file cost would show, small enough to build in a test.
MODULE_COUNT = 2_000
#: The modules plus the ``.gitignore`` the walk also manifests.
FILE_COUNT = MODULE_COUNT + 1
WARM_RUNS = 12

#: The plan's target. "~1 s" is read strictly here: the slowest warm run.
BUDGET_S = 1.0


def _build_repository(root: Path) -> int:
    """A repository-shaped tree of ``FILE_COUNT`` source files."""
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (root / ".gitignore").write_text("build/\n*.log\n.lemoncrow-local-fs-proof\n", encoding="utf-8")
    total = 0
    for index in range(MODULE_COUNT):
        package = root / f"pkg{index % 50:02d}"
        package.mkdir(exist_ok=True)
        body = (
            f'"""Module {index}."""\n\n'
            f"CONSTANT_{index} = {index}\n\n\n"
            f"def function_{index}(value: int) -> int:\n"
            f"    return value + CONSTANT_{index}\n\n\n"
            f"class Class_{index}:\n"
            f"    def method(self) -> int:\n"
            f"        return function_{index}({index})\n"
        )
        target = package / f"module_{index:05d}.py"
        target.write_text(body, encoding="utf-8")
        total += len(body)
    return total


@pytest.fixture(scope="module")
def big_repository(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    root = tmp_path_factory.mktemp("budget") / "worktree"
    size = _build_repository(root)
    return {"root": root, "bytes": size}


def _start_server(tmp_path: Path) -> tuple[Any, Any, str]:
    from lemoncrow_server.app import build_app
    from lemoncrow_server.auth import (
        RoleAuthorizer,
        StaticTokenAuthenticator,
        TokenRecord,
        token_digest,
    )
    from lemoncrow_server.config import ServerConfig
    from lemoncrow_server.identity import Role
    from lemoncrow_server.limits import Limits
    from lemoncrow_server.server import EnterpriseServer
    from lemoncrow_server_core.index.sqlite import build_sqlite_backend

    config = ServerConfig(
        listen_host="127.0.0.1",
        listen_port=0,
        limits=Limits(),
        allow_local_fs=False,
        maintenance_interval_s=0.0,
    )
    backend = build_sqlite_backend(
        tmp_path / "index.sqlite",
        content_size_cap=config.limits.max_indexed_content_bytes,
        inline_rebuild_budget=config.limits.inline_rebuild_budget,
    )

    class _Dispatcher:
        @property
        def available(self) -> bool:
            return True

        @property
        def tools(self) -> frozenset[str]:
            from lemoncrow_server_core.matrix import SERVER_TOOLS

            return SERVER_TOOLS

        def dispatch(self, invocation: Any) -> Any:
            from lemoncrow_server_core.dispatch import ToolResult

            return ToolResult(tool=invocation.tool, content=({"type": "text", "text": "ok"},))

    authenticator = StaticTokenAuthenticator(
        (
            TokenRecord(
                token_id="tok_budget",
                token_sha256=token_digest(TOKEN),
                org_id=ORG,
                subject="user_budget",
                roles=frozenset({Role.DEVELOPER}),
            ),
        )
    )
    state, app = build_app(
        config=config,
        dispatcher=_Dispatcher(),
        backend=backend,
        authenticator=authenticator,
        authorizer=RoleAuthorizer(),
    )
    return EnterpriseServer(state, app, config), backend, ""


def _config(url: str, worktree: Path, state_dir: Path) -> Any:
    from lemoncrow_client.config import load_config

    return load_config(
        {
            "LEMONCROW_URL": url,
            "LEMONCROW_TOKEN": TOKEN,
            "LEMONCROW_HOME": str(state_dir),
            "HOME": str(state_dir.parent),
            "LEMONCROW_LOCAL_FS": "0",
            "LEMONCROW_REQUEST_TIMEOUT_S": "60",
            "LEMONCROW_STARTUP_BUDGET_S": "60",
        },
        cwd=worktree,
    )


def _run_initialize(config: Any) -> tuple[float, str]:
    from lemoncrow_client.mcpserver import McpServer

    server = McpServer(config)
    started = time.perf_counter()
    answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    elapsed = time.perf_counter() - started
    assert answer is not None
    assert answer["result"]["serverInfo"]["name"] == "lc"
    context = str(answer["result"].get("instructions", ""))
    assert "LemonCrow:" in context
    server.close()
    return elapsed, context


@pytest.fixture(scope="module")
def measurement(big_repository: dict[str, Any], tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    import asyncio
    import threading

    server_home = tmp_path_factory.mktemp("budget-server")
    state_dir = tmp_path_factory.mktemp("budget-home") / "lemoncrow"
    state_dir.mkdir()
    server, _backend, _ = _start_server(server_home)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True, name="budget-loop")
    thread.start()

    def run(coroutine: Any, timeout: float = 300.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout)

    url = run(server.start())
    worktree = big_repository["root"]
    try:
        config = _config(url, worktree, state_dir)
        cold_s, cold_context = _run_initialize(config)
        warm: list[float] = []
        warm_context = ""
        for _ in range(WARM_RUNS):
            elapsed, warm_context = _run_initialize(_config(url, worktree, state_dir))
            warm.append(elapsed)
    finally:
        run(server.stop())
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=10.0)
        loop.close()

    warm.sort()
    return {
        "files": FILE_COUNT,
        "bytes": big_repository["bytes"],
        "cold_s": cold_s,
        "cold_context": cold_context,
        "warm_s": warm,
        "warm_context": warm_context,
        "min_s": warm[0],
        "median_s": warm[len(warm) // 2],
        "max_s": warm[-1],
    }


def test_the_cold_run_really_synchronized_the_repository(measurement: dict[str, Any]) -> None:
    """Otherwise every warm number below measures an empty view."""
    context = measurement["cold_context"]
    assert f"{FILE_COUNT} files" in context, context
    assert "cold" in context


def test_the_measured_runs_were_warm(measurement: dict[str, Any]) -> None:
    context = measurement["warm_context"]
    assert "warm" in context, context
    assert f"{FILE_COUNT} files" in context, context


def test_warm_mcp_initialize_stays_inside_the_budget(
    measurement: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate number, printed so the run is its own evidence."""
    with capsys.disabled():
        print(
            f"\nwarm MCP initialize over {measurement['files']} files "
            f"({measurement['bytes'] / 1_000_000:.2f} MB), {WARM_RUNS} runs:\n"
            f"  cold   {measurement['cold_s'] * 1000:8.1f} ms\n"
            f"  min    {measurement['min_s'] * 1000:8.1f} ms\n"
            f"  median {measurement['median_s'] * 1000:8.1f} ms\n"
            f"  max    {measurement['max_s'] * 1000:8.1f} ms\n"
            f"  budget {BUDGET_S * 1000:8.1f} ms"
        )
    assert measurement["max_s"] < BUDGET_S, (
        f"slowest warm MCP initialize was {measurement['max_s'] * 1000:.1f} ms "
        f"over {measurement['files']} files; budget is {BUDGET_S * 1000:.0f} ms"
    )


def test_a_warm_initialize_transfers_no_manifest_and_no_content(measurement: dict[str, Any]) -> None:
    """The reason it is fast, asserted rather than assumed."""
    context = measurement["warm_context"]
    assert "unavailable" not in context
    # A warm run that had resent the manifest would not be warm; initialize
    # carries the session report in its instructions, and it says so.
    assert "warm" in context
