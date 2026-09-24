"""Warm MCP ``initialize`` at enterprise scale, over a realistic network.

``tests/test_mcp_startup_budget.py`` measures 2,001 files and 0.39 MB against
an in-process server on loopback. That is about 2% of an enterprise monorepo, on
a link with no round trip and no TLS, and a budget that only holds there is not a
budget -- it is a fixture.

This module measures the same MCP initialize bootstrap path under the conditions
the target was written for:

* a synthetic repository of at least 50,000 files with a realistic, heavy-tailed
  size distribution across several languages (see ``_synthrepo``);
* the real ``enterprise/server``, not a stub;
* four transports -- plain loopback, plain loopback plus a simulated round trip,
  TLS, and TLS plus the same round trip -- because this client opens a
  *connection per request*, so a round trip is paid several times per session and
  a TLS handshake with it;
* and a leg with the walk cache deliberately unavailable, so the number the cache
  is responsible for is separated from the number the walk restructuring is.

It is opt-in. The cold session that has to precede the warm ones uploads and
indexes hundreds of megabytes and takes many minutes, which is not something a
test suite should do by accident; set ``LEMONCROW_SCALE_BENCH=1`` to run it.
``LEMONCROW_SCALE_WORKDIR`` points at a directory where the generated tree and
the server's index are kept between runs, so the cold pass is paid once.

Run it::

    LEMONCROW_SCALE_BENCH=1 LEMONCROW_SCALE_WORKDIR=/var/tmp/lemoncrow-scale \\
        PYTHONPATH=client/src enterprise/server/.venv/bin/python -m pytest \\
        client/tests/test_enterprise_scale_budget.py -q -s
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from _netsim import LatencyProxy, TlsMaterial, openssl_available, self_signed
from _serverpkg import REASON, server_available
from _synthrepo import generate_repository

BENCH_ENV = "LEMONCROW_SCALE_BENCH"
WORKDIR_ENV = "LEMONCROW_SCALE_WORKDIR"
FILES_ENV = "LEMONCROW_SCALE_FILES"
RTT_ENV = "LEMONCROW_SCALE_RTT_MS"

#: The plan's target for a warm start, read strictly: the slowest warm run.
BUDGET_S = 1.0
#: "Enterprise scale" as this module defines it.
MINIMUM_FILES = 50_000
WARM_RUNS = 7

TOKEN = "scale-token-0123456789abcdef0123"
ORG = "org_scale"

pytestmark = [
    pytest.mark.enterprise_server,
    pytest.mark.skipif(not server_available(), reason=REASON),
    pytest.mark.skipif(
        os.environ.get(BENCH_ENV, "") not in {"1", "true", "yes"},
        reason=f"set {BENCH_ENV}=1 to run the enterprise-scale measurement (its cold pass takes minutes)",
    ),
]


def _files() -> int:
    raw = os.environ.get(FILES_ENV, "").strip()
    return max(MINIMUM_FILES, int(raw)) if raw.isdigit() else MINIMUM_FILES


def _rtt_s() -> float:
    raw = os.environ.get(RTT_ENV, "").strip()
    try:
        return float(raw) / 1000.0 if raw else 0.020
    except ValueError:
        return 0.020


# --------------------------------------------------------------------------- #
# The server under measurement                                                #
# --------------------------------------------------------------------------- #


@dataclass
class Deployment:
    """One backend, served plainly and over TLS at the same time."""

    backend: Any
    plain_url: str
    tls_url: str
    tls: TlsMaterial | None
    _servers: list[Any] = field(default_factory=list)


def _build(home: Path, tls: TlsMaterial | None) -> tuple[Any, Any]:
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
        tls_certfile=tls.certfile if tls is not None else None,
        tls_keyfile=tls.keyfile if tls is not None else None,
    )
    backend = build_sqlite_backend(
        home / "index.sqlite",
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
                token_id="tok_scale",
                token_sha256=token_digest(TOKEN),
                org_id=ORG,
                subject="user_scale",
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
    return EnterpriseServer(state, app, config), backend


# --------------------------------------------------------------------------- #
# One measured session                                                        #
# --------------------------------------------------------------------------- #


def _config(url: str, worktree: Path, state_dir: Path) -> Any:
    from lemoncrow_client.config import load_config

    return load_config(
        {
            "LEMONCROW_URL": url,
            "LEMONCROW_TOKEN": TOKEN,
            "LEMONCROW_HOME": str(state_dir),
            "HOME": str(state_dir.parent),
            "LEMONCROW_LOCAL_FS": "0",
            "LEMONCROW_REQUEST_TIMEOUT_S": "3600",
            "LEMONCROW_STARTUP_BUDGET_S": "3600",
        },
        cwd=worktree,
    )


def _session(config: Any, tls: TlsMaterial | None) -> Any:
    from lemoncrow_client.session import RemoteSession
    from lemoncrow_client.transport import HttpTransport

    if tls is None:
        return RemoteSession(config)
    transport = HttpTransport(config.url, timeout_s=config.request_timeout_s, ssl_context=tls.client_context())
    return RemoteSession(config, transport)


def _run(url: str, worktree: Path, state_dir: Path, tls: TlsMaterial | None) -> tuple[float, str]:
    """One MCP initialize, timed exactly as the developer experiences it."""
    from lemoncrow_client.mcpserver import McpServer

    config = _config(url, worktree, state_dir)
    server = McpServer(config, session=_session(config, tls))
    started = time.perf_counter()
    answer = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    elapsed = time.perf_counter() - started
    assert answer is not None
    assert answer["result"]["serverInfo"]["name"] == "lc"
    context = str(answer["result"].get("instructions", ""))
    assert "LemonCrow:" in context
    server.close()
    return elapsed, context


@dataclass(frozen=True, slots=True)
class Leg:
    """One transport configuration, measured."""

    name: str
    timings: tuple[float, ...]
    context: str

    @property
    def min_s(self) -> float:
        return min(self.timings)

    @property
    def median_s(self) -> float:
        return sorted(self.timings)[len(self.timings) // 2]

    @property
    def max_s(self) -> float:
        return max(self.timings)


def _measure(name: str, runner: Callable[[], tuple[float, str]], runs: int = WARM_RUNS) -> Leg:
    timings: list[float] = []
    context = ""
    for _ in range(runs):
        elapsed, context = runner()
        timings.append(elapsed)
    return Leg(name=name, timings=tuple(timings), context=context)


# --------------------------------------------------------------------------- #
# The measurement                                                             #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def workdir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    configured = os.environ.get(WORKDIR_ENV, "").strip()
    if configured:
        target = Path(configured)
        target.mkdir(parents=True, exist_ok=True)
        return target
    return tmp_path_factory.mktemp("scale")


@pytest.fixture(scope="module")
def repository(workdir: Path) -> dict[str, Any]:
    root = workdir / "worktree"
    marker = workdir / "worktree.json"
    wanted = _files()
    if marker.is_file():
        recorded = json.loads(marker.read_text(encoding="utf-8"))
        if recorded.get("files") == wanted + 1 and root.is_dir():
            return recorded
    started = time.perf_counter()
    synthetic = generate_repository(root, files=wanted)
    recorded = {
        "root": str(root),
        "files": synthetic.files,
        "bytes": synthetic.bytes,
        "directories": synthetic.directories,
        "mean_bytes": synthetic.mean_bytes,
        "generated_s": time.perf_counter() - started,
        "by_extension": synthetic.by_extension,
    }
    marker.write_text(json.dumps(recorded), encoding="utf-8")
    return recorded


@pytest.fixture(scope="module")
def loop() -> Iterator[Any]:
    created = asyncio.new_event_loop()
    thread = threading.Thread(target=created.run_forever, daemon=True, name="scale-loop")
    thread.start()
    yield created
    created.call_soon_threadsafe(created.stop)
    thread.join(timeout=15.0)
    created.close()


@pytest.fixture(scope="module")
def measurement(repository: dict[str, Any], workdir: Path, loop: Any) -> dict[str, Any]:
    worktree = Path(repository["root"])
    server_home = workdir / "server"
    server_home.mkdir(parents=True, exist_ok=True)
    state_dir = workdir / "home" / "lemoncrow"
    state_dir.mkdir(parents=True, exist_ok=True)
    tls = self_signed(workdir / "tls") if openssl_available() else None

    def run(coroutine: Any, timeout: float = 7200.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, loop).result(timeout)

    plain, backend = _build(server_home, None)
    plain_url = run(plain.start())
    tls_server = None
    tls_url = ""
    if tls is not None:
        tls_server, _ = _build(server_home, tls)
        tls_url = run(tls_server.start())

    rtt = _rtt_s()
    legs: list[Leg] = []
    try:
        first_started = time.perf_counter()
        first_elapsed, first_context = _run(plain_url, worktree, state_dir, None)
        first_total = time.perf_counter() - first_started

        legs.append(_measure("loopback", lambda: _run(plain_url, worktree, state_dir, None)))

        def without_cache() -> tuple[float, str]:
            """A state directory the cache is never in: the pre-fast-path cost."""
            import tempfile

            with tempfile.TemporaryDirectory() as fresh:
                return _run(plain_url, worktree, Path(fresh), None)

        legs.append(_measure("loopback, no walk cache", without_cache, runs=3))

        _, _, port = plain_url.rpartition(":")
        with LatencyProxy("127.0.0.1", int(port), rtt_s=rtt) as proxy:
            url = f"http://{proxy.address}"
            legs.append(_measure(f"loopback + {rtt * 1000:.0f} ms RTT", lambda: _run(url, worktree, state_dir, None)))

        if tls is not None and tls_url:
            legs.append(_measure("TLS", lambda: _run(tls_url, worktree, state_dir, tls)))
            _, _, tls_port = tls_url.rpartition(":")
            with LatencyProxy("127.0.0.1", int(tls_port), rtt_s=rtt) as proxy:
                url = f"https://{proxy.address}"
                legs.append(_measure(f"TLS + {rtt * 1000:.0f} ms RTT", lambda: _run(url, worktree, state_dir, tls)))
    finally:
        run(plain.stop())
        if tls_server is not None:
            run(tls_server.stop())

    return {
        "repository": repository,
        "first_s": first_total,
        "first_context": first_context,
        # A reused ``LEMONCROW_SCALE_WORKDIR`` starts with the server's index
        # already populated, so the first session of the run is not a cold one
        # and must not be reported as though it were.
        "first_was_cold": "cold" in first_context,
        "first_hook_s": first_elapsed,
        "rtt_s": rtt,
        "tls": tls is not None,
        "legs": legs,
        "backend": backend,
    }


def test_the_tree_really_is_at_enterprise_scale(measurement: dict[str, Any]) -> None:
    repository = measurement["repository"]
    assert repository["files"] > MINIMUM_FILES, repository["files"]
    assert repository["bytes"] > 100_000_000, repository["bytes"]
    assert len(repository["by_extension"]) >= 5, repository["by_extension"]


def test_the_first_run_really_synchronized_the_repository(measurement: dict[str, Any]) -> None:
    """Otherwise every warm number below measures an empty view."""
    context = measurement["first_context"]
    assert f"{measurement['repository']['files']} files" in context, context
    assert "unavailable" not in context, context


def test_every_measured_run_was_warm(measurement: dict[str, Any]) -> None:
    """A warm number measured on a cold session would be measuring an upload."""
    for leg in measurement["legs"]:
        assert "warm" in leg.context, f"{leg.name}: {leg.context}"
        assert "unavailable" not in leg.context, f"{leg.name}: {leg.context}"


def test_a_warm_session_re_hashes_nothing(measurement: dict[str, Any]) -> None:
    """The fast path, asserted where it matters rather than only in unit tests."""
    for leg in measurement["legs"]:
        if "no walk cache" in leg.name:
            assert "0 re-hashed" not in leg.context, leg.context
            continue
        assert "0 re-hashed this session" in leg.context, f"{leg.name}: {leg.context}"


def test_warm_mcp_initialize_at_enterprise_scale(
    measurement: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    """The gate number, printed so a run of this module is its own evidence."""
    repository = measurement["repository"]
    with capsys.disabled():
        print(
            f"\nwarm MCP initialize over {repository['files']} files "
            f"({repository['bytes'] / 1_000_000:.1f} MB, mean {repository['mean_bytes']:.0f} B, "
            f"{repository['directories']} directories, {len(repository['by_extension'])} languages)\n"
            + (
                f"  cold  {measurement['first_s']:10.1f} s   (one-time: upload and server-side indexing)"
                if measurement["first_was_cold"]
                else f"  first run was already warm ({measurement['first_s'] * 1000:.1f} ms): "
                f"the server index in {WORKDIR_ENV} was reused, so no cold number was measured"
            )
        )
        for leg in measurement["legs"]:
            print(
                f"  {leg.name:<26} min {leg.min_s * 1000:8.1f}  "
                f"median {leg.median_s * 1000:8.1f}  max {leg.max_s * 1000:8.1f} ms"
            )
        print(f"  budget {BUDGET_S * 1000:.0f} ms (slowest warm run, plain loopback)")

    loopback = next(leg for leg in measurement["legs"] if leg.name == "loopback")
    assert loopback.max_s < BUDGET_S, (
        f"slowest warm MCP initialize on plain loopback was {loopback.max_s * 1000:.1f} ms "
        f"over {repository['files']} files; the target is {BUDGET_S * 1000:.0f} ms"
    )


def test_the_walk_cache_is_what_makes_it_fast(measurement: dict[str, Any]) -> None:
    """The comparison that attributes the number to the change that produced it."""
    legs = {leg.name: leg for leg in measurement["legs"]}
    with_cache = legs["loopback"]
    without = legs["loopback, no walk cache"]
    assert without.median_s > with_cache.median_s * 1.5, (
        f"the cache saved only {(without.median_s - with_cache.median_s) * 1000:.1f} ms "
        f"({without.median_s * 1000:.1f} ms vs {with_cache.median_s * 1000:.1f} ms)"
    )
