from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from aiohttp import ClientSession
from lemoncrow_server_core import cli as server_cli
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.cli import _selected_products, review_main
from lemoncrow_server_core.config import LocalServerConfig
from lemoncrow_server_core.dispatch import UnavailableDispatcher
from lemoncrow_server_core.service_protocol import SERVICE_REVIEW


def test_product_selection_defaults_to_review_and_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LEMONCROW_LOCAL_SERVER_PRODUCTS", raising=False)
    assert _selected_products(None) == frozenset({"review", "dashboard"})
    assert _selected_products(["review"]) == frozenset({"review"})
    assert _selected_products(["dashboard", "review"]) == frozenset({"review", "dashboard"})


def test_legacy_runtime_product_maps_to_dashboard(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # Installs written before Runtime Lite still pass `--product runtime`; the
    # local server keeps only the Dashboard that name used to carry.
    monkeypatch.delenv("LEMONCROW_LOCAL_SERVER_PRODUCTS", raising=False)
    assert _selected_products(["runtime"]) == frozenset({"dashboard"})
    assert _selected_products(["runtime", "review"]) == frozenset({"review", "dashboard"})
    config = LocalServerConfig(state_root=tmp_path, products=frozenset({"runtime"}))
    assert config.products == frozenset({"dashboard"})
    assert config.dashboard_enabled is True


def test_product_selection_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LOCAL_SERVER_PRODUCTS", "review")
    assert _selected_products(None) == frozenset({"review"})
    monkeypatch.setenv("LEMONCROW_LOCAL_SERVER_PRODUCTS", "runtime,review")
    assert _selected_products(None) == frozenset({"review", "dashboard"})
    monkeypatch.setenv("LEMONCROW_LOCAL_SERVER_PRODUCTS", "dashboard")
    assert _selected_products(None) == frozenset({"dashboard"})
    monkeypatch.setenv("LEMONCROW_LOCAL_SERVER_PRODUCTS", "review,banana")
    with pytest.raises(ValueError, match="unknown local server product"):
        _selected_products(None)


def test_config_rejects_unknown_product(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="products must be review and/or dashboard"):
        LocalServerConfig(state_root=tmp_path, products=frozenset({"banana"}))


def test_local_server_never_executes_runtime_tools(tmp_path: Path) -> None:
    asyncio.run(_exercise_no_tool_execution(tmp_path))


async def _exercise_no_tool_execution(tmp_path: Path) -> None:
    # Runtime tools run in the agent's own MCP process (Runtime Lite). The
    # local server, with every product enabled, has no tool route at all.
    server, state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=False)
    )
    assert isinstance(state.dispatcher, UnavailableDispatcher)
    url = await server.start()
    try:
        async with ClientSession() as client:
            assert (await client.get(f"{url}/v1/tools")).status == 404
            assert (await client.post(f"{url}/v1/tools/read", json={"arguments": {}})).status == 404
            identity = await (await client.get(f"{url}/.well-known/lemoncrow-server.json")).json()
            assert identity["products"]["dashboard"]["available"] is True
            assert (await client.get(f"{url}/readyz")).status == 200
    finally:
        await server.stop()


def test_review_only_loopback_exposes_no_runtime_execution_surface(tmp_path: Path) -> None:
    asyncio.run(_exercise_review_only(tmp_path))


async def _exercise_review_only(tmp_path: Path) -> None:
    server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            local_no_auth=True,
            allow_local_fs=False,
            products=frozenset({"review"}),
        )
    )
    assert isinstance(state.dispatcher, UnavailableDispatcher)
    url = await server.start()
    try:
        async with ClientSession() as client:
            discovery = await client.get(f"{url}/.well-known/lemoncrow-server.json")
            assert discovery.status == 200
            identity = await discovery.json()
            assert identity["products"]["review"]["available"] is True
            assert identity["products"]["runtime"]["available"] is False
            assert "review" in identity["product_capabilities"]
            assert identity["product_capabilities"]["review"]["ask"] is True

            ready = await client.get(f"{url}/readyz")
            assert ready.status == 200
            assert await ready.json() == {"status": "ready"}

            tools = await client.get(f"{url}/v1/tools")
            assert tools.status == 404
            integrations = await client.get(f"{url}/api/local/integrations")
            assert integrations.status == 404

            system = await client.get(f"{url}/api/local/system")
            assert system.status == 200
            system_payload = await system.json()
            assert system_payload["ready"] is True
            assert system_payload["dispatcher_available"] is False
            assert system_payload["tool_count"] == 0

            # Review capture is still a registered product route. The empty
            # request is invalid, but it must fail as a Review request, not 404.
            capture = await client.post(f"{url}/v1/repos/repo_fixture/reviews", json={})
            assert capture.status != 404
    finally:
        await server.stop()


_RUNTIME_DASHBOARD_PAGES = ("/home", "/runs", "/runs/run_1", "/code", "/knowledge", "/knowledge/memory", "/usage")


def _spa_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "dist"
    (bundle / "assets").mkdir(parents=True)
    (bundle / "index.html").write_text("<!doctype html><div id=root></div>", encoding="utf-8")
    return bundle


@pytest.mark.parametrize(
    ("products", "landing", "dashboard_pages"),
    [(frozenset({"review"}), "/reviews", 404), (frozenset({"review", "dashboard"}), "/home", 200)],
)
def test_runtime_dashboard_pages_are_served_only_with_the_dashboard(
    tmp_path: Path, products: frozenset[str], landing: str, dashboard_pages: int
) -> None:
    asyncio.run(_exercise_spa_routes(tmp_path, products, landing, dashboard_pages))


async def _exercise_spa_routes(tmp_path: Path, products: frozenset[str], landing: str, dashboard_pages: int) -> None:
    server, _state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            local_no_auth=True,
            allow_local_fs=False,
            frontend_dir=_spa_bundle(tmp_path),
            products=products,
            isolate_services=False,
        )
    )
    url = await server.start()
    try:
        async with ClientSession() as client:
            for path in _RUNTIME_DASHBOARD_PAGES:
                response = await client.get(f"{url}{path}", allow_redirects=False)
                assert response.status == dashboard_pages, path
            for path in ("/reviews", "/review", "/reviews/rev_1", "/settings"):
                assert (await client.get(f"{url}{path}", allow_redirects=False)).status == 200, path
            root = await client.get(f"{url}/", allow_redirects=False)
            assert root.status == 302
            assert root.headers["Location"] == landing
    finally:
        await server.stop()


def test_review_artifact_cli_has_no_index_migration_or_dashboard() -> None:
    for argv in (
        ["migrate-index", "--database", "index.sqlite3"],
        ["up", "--directory", "state", "--local-no-auth", "--product", "dashboard"],
        ["up", "--directory", "state", "--local-no-auth", "--product", "runtime"],
        ["service", "--name", "dashboard", "--state-root", "state"],
    ):
        with pytest.raises(SystemExit) as caught:
            review_main(argv)
        assert caught.value.code == 2, argv


def test_review_artifact_cli_serves_review_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[frozenset[str]] = []

    def fake_build(config: LocalServerConfig) -> object:
        seen.append(config.products)
        raise RuntimeError("stop after composition")

    monkeypatch.setattr(server_cli, "build_local_server", fake_build)
    monkeypatch.delenv("LEMONCROW_LOCAL_SERVER_PRODUCTS", raising=False)
    argv = ["up", "--directory", str(tmp_path / "state"), "--local-no-auth", "--check-only"]
    assert review_main(argv) == 1
    assert seen == [frozenset({"review"})]

    monkeypatch.setenv("LEMONCROW_LOCAL_SERVER_PRODUCTS", "review,dashboard")
    assert review_main(argv) == 2
    assert seen == [frozenset({"review"})]


def test_review_only_isolated_services_start_review_worker_only(tmp_path: Path) -> None:
    _server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            local_no_auth=True,
            allow_local_fs=False,
            isolate_services=True,
            products=frozenset({"review"}),
        )
    )
    try:
        assert state.services is not None
        assert state.services.names() == (SERVICE_REVIEW,)
    finally:
        if state.workspace is not None:
            state.workspace.close()
        state.backend.close_backend()


def test_local_build_does_not_import_runtime_tool_execution(tmp_path: Path) -> None:
    code = r"""
import json
import sys
from pathlib import Path

from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

root = Path(sys.argv[1])
server, state = build_local_server(
    LocalServerConfig(
        listen_port=0,
        state_root=root,
        local_no_auth=True,
        allow_local_fs=False,
    )
)
forbidden = {
    "lemoncrow_server_core.registry_dispatch",
    "lemoncrow_server_core.product_telemetry",
    "lemoncrow_server_core.tracing",
    "lemoncrow_server_core.index.search",
    "lemoncrow.gateway.tools.registry",
    "lemoncrow_server_core.runtime_product_surface",
}
print(json.dumps(sorted(name for name in forbidden if name in sys.modules)))
if state.workspace is not None:
    state.workspace.close()
state.backend.close_backend()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(str(item) for item in sys.path if item)
    completed = subprocess.run(
        [sys.executable, "-c", code, str(tmp_path / "state")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == []


#: Server-side tool execution is hosted-only (enterprise/server); the public
#: server core keeps the index store, Review and the basic Dashboard.
_HOSTED_TOOL_EXECUTION = (
    "lemoncrow_server_core.registry_dispatch",
    "lemoncrow_server_core.tracing",
    "lemoncrow_server_core.product_telemetry",
    "lemoncrow_server_core.storage_ops",
    "lemoncrow_server_core.index.search",
    "lemoncrow_server_core.index.search_markdown",
    "lemoncrow_server_core.index.graph",
    "lemoncrow_server_core.index.engine_adapter",
    "lemoncrow_server_core.index.runtime_policy",
    "lemoncrow_server_core.index.zoekt_accelerator",
)


@pytest.mark.parametrize("module", _HOSTED_TOOL_EXECUTION)
def test_public_server_core_ships_no_hosted_tool_execution(module: str) -> None:
    assert importlib.util.find_spec(module) is None


def test_public_wiring_cannot_build_a_tool_dispatcher() -> None:
    from lemoncrow_server_core import wiring

    assert not hasattr(wiring, "build_dispatcher")


def test_product_selection_rejects_empty_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LOCAL_SERVER_PRODUCTS", ",")
    with pytest.raises(ValueError, match="at least one product"):
        _selected_products(None)


def test_config_rejects_empty_product_set(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="at least one product"):
        LocalServerConfig(state_root=tmp_path, products=frozenset())


def test_dashboard_only_loopback_exposes_no_review_surface(tmp_path: Path) -> None:
    asyncio.run(_exercise_dashboard_only(tmp_path))


async def _exercise_dashboard_only(tmp_path: Path) -> None:
    server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            local_no_auth=True,
            allow_local_fs=False,
            products=frozenset({"dashboard"}),
        )
    )
    assert isinstance(state.dispatcher, UnavailableDispatcher)
    url = await server.start()
    try:
        async with ClientSession() as client:
            discovery = await client.get(f"{url}/.well-known/lemoncrow-server.json")
            assert discovery.status == 200
            identity = await discovery.json()
            assert identity["products"]["review"]["available"] is False
            assert identity["products"]["dashboard"]["available"] is True
            assert "review" not in identity["product_capabilities"]

            ready = await client.get(f"{url}/readyz")
            assert ready.status == 200

            tools = await client.get(f"{url}/v1/tools")
            assert tools.status == 404
            integrations = await client.get(f"{url}/api/local/integrations")
            assert integrations.status == 200

            capture = await client.post(f"{url}/v1/repos/repo_fixture/reviews", json={})
            assert capture.status == 404
            pair = await client.post(f"{url}/v1/local-browser/pair", json={})
            assert pair.status == 404
    finally:
        await server.stop()


def test_dashboard_only_isolated_services_start_dashboard_worker_only(tmp_path: Path) -> None:
    from lemoncrow_server_core.service_protocol import SERVICE_DASHBOARD

    _server, state = build_local_server(
        LocalServerConfig(
            listen_port=0,
            state_root=tmp_path / "state",
            local_no_auth=True,
            allow_local_fs=False,
            isolate_services=True,
            products=frozenset({"dashboard"}),
        )
    )
    try:
        assert state.services is not None
        assert state.services.names() == (SERVICE_DASHBOARD,)
    finally:
        if state.workspace is not None:
            state.workspace.close()
        state.backend.close_backend()
