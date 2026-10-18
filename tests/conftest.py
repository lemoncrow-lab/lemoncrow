"""Shared pytest fixtures."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from lemoncrow.core.runtime import LemonCrowRuntimeCore
    from lemoncrow.infra.storage.bundle import StoreBundle

# Project rule for every agent and person: verify a change with the tests it
# touches, never the whole suite. The largest single test file is ~350 tests,
# so this passes any focused file list and refuses tests/, tests/core and
# tests/gateway. Counted after -k/-m/--lf/--deselect, so filters stay usable.
FOCUSED_TEST_LIMIT = 1000
FULL_SUITE_OPT_IN = "LEMONCROW_FULL_SUITE"


def pytest_collection_finish(session: pytest.Session) -> None:
    # xdist workers each collect everything; their controller refuses once, cleanly.
    if not hasattr(session.config, "workerinput"):
        enforce_focused_run(session.config, len(session.items))


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_node_collection_finished(node: Any, ids: list[str]) -> None:
    enforce_focused_run(node.config, len(ids))


def enforce_focused_run(config: pytest.Config, count: int) -> None:
    if count <= FOCUSED_TEST_LIMIT or config.option.collectonly or os.environ.get(FULL_SUITE_OPT_IN) == "1":
        return
    pytest.exit(
        f"Refusing to run {count} tests (limit {FOCUSED_TEST_LIMIT}). Run only the test files your change "
        f"touches. Coding agents must never opt in; a person runs the whole suite with {FULL_SUITE_OPT_IN}=1 "
        "on that one command.",
        returncode=pytest.ExitCode.USAGE_ERROR,
    )


# Longest a test waits for the worker tick it started before failing it as a leak.
WORKER_TICK_JOIN_S = 120.0
_MCP_SERVER = "lemoncrow.gateway.adapters.mcp_server"


def _worker_tick_thread() -> Any:
    return getattr(sys.modules.get(_MCP_SERVER), "_worker_tick_thread", None)


@pytest.fixture(autouse=True)
def _the_worker_tick_stays_in_the_test_that_started_it(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """An MCP tool call starts the worker tick (``mcp_server._spawn_worker_if_idle``): a daemon
    thread that drains jobs, then runs the maintenance duties, whose subprocesses import the host's
    real agent sessions for up to 300s. In a test that background tick skips the duties (their own
    tests call ``run_maintenance_tick`` on their own thread) and the test that started it joins it,
    so it never runs inside a later test, one that patches the process-global ``subprocess.Popen``
    for instance."""
    from lemoncrow.core.service import maintenance_tick

    real = maintenance_tick.run_maintenance_tick

    def run_unless_background_tick(*args: Any, **kwargs: Any) -> Any:
        if threading.current_thread() is _worker_tick_thread():
            return {"ran": False, "reason": "background worker tick in a test"}
        return real(*args, **kwargs)

    monkeypatch.setattr(maintenance_tick, "run_maintenance_tick", run_unless_background_tick)
    before = _worker_tick_thread()
    yield
    thread = _worker_tick_thread()
    if thread is None or thread is before:
        return
    thread.join(WORKER_TICK_JOIN_S)
    if thread.is_alive():
        pytest.fail(f"the MCP worker tick this test started is still running after {WORKER_TICK_JOIN_S:.0f}s")


@pytest.fixture(autouse=True)
def _no_real_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never launch a desktop browser from tests; print the URL instead."""
    import webbrowser

    monkeypatch.setenv("LEMONCROW_NO_BROWSER", "1")

    def _print_url(url: str, *args: object, **kwargs: object) -> bool:
        print(f"[test] browser open suppressed: {url}")
        return True

    monkeypatch.setattr(webbrowser, "open", _print_url)


@pytest.fixture(autouse=True)
def _isolate_workspace_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Isolate tests from host workspace env vars and default runtime roots."""
    for env_var in (
        "LEMONCROW_WORKSPACE_ROOT",
        "CLAUDE_WORKSPACE_ROOT",
        "CURSOR_WORKSPACE_ROOT",
        "VSCODE_CWD",
        "LEMONCROW_LESSONS_ROOT",
        "LEMONCROW_STORE_ROOT",
        "LEMONCROW_MEM_ROOT",
    ):
        monkeypatch.delenv(env_var, raising=False)
    isolated_root = tmp_path / ".lemoncrow"
    monkeypatch.setenv("LEMONCROW_ROOT", str(isolated_root))
    monkeypatch.setenv("LEMONCROW_STORE_ROOT", str(isolated_root))
    # Complete the isolation: point the workspace root at tmp_path too. Without
    # this, _workspace_root() falls through to os.getcwd() (the real repo), so
    # the new read/projection workspace-confinement rejects files tests create
    # under tmp_path. Tests that need a specific workspace set it themselves.
    monkeypatch.setenv("LEMONCROW_WORKSPACE_ROOT", str(tmp_path))
    # The agent running pytest may itself be Codex/Cursor/etc. Pin ordinary
    # tests to the product's documented fallback host; host-detection tests
    # explicitly override or remove this value.
    monkeypatch.setenv("LEMONCROW_AGENT", "claude")
    # Ordinary tests are not running inside a coding agent's session even when
    # the developer's agent launched pytest; tests of agent-only behavior set
    # these explicitly.
    from lemoncrow.core.foundation.paths import AGENT_SESSION_ENVS

    for env_var, _host in AGENT_SESSION_ENVS:
        monkeypatch.delenv(env_var, raising=False)
    # Knobs a host session injects into every child process (Claude Code's own
    # CLAUDE_CODE_*, and the LemonCrow plugin's runtime tuning). Left in place
    # they narrow the tool surface, skip verification paths or change hook
    # output for whoever launched pytest; tests of those knobs set them.
    for env_var in list(os.environ):
        if env_var.startswith("CLAUDE_CODE_"):
            monkeypatch.delenv(env_var, raising=False)
    for env_var in (
        "CLAUDE_PROJECT_DIR",
        "LEMONCROW_MCP_TOOL_PROFILE",
        "LEMONCROW_TELEGRAPHIC",
        "LEMONCROW_HEADROOM_MCP_TAIL_MODE",
        "LEMONCROW_HEADROOM_TAIL_STATS",
        "LEMONCROW_VERIFY_SKIP_PATHS",
        "LEMONCROW_AUTO_UPDATE",
        "LEMONCROW_UPDATE_CHECK",
        "LEMONCROW_REVIEW_CARRY",
        "LEMONCROW_REVIEW_SHARED_BASE",
        "LEMONCROW_REVIEW_CI_EQUIVALENCE",
    ):
        monkeypatch.delenv(env_var, raising=False)
    # Point host-transcript discovery at an isolated, empty dir. Savings/recall/
    # statusline code falls back to the developer's real ~/.claude/projects when
    # CLAUDE_CONFIG_DIR is unset, so an in-process test would replay every real
    # host session -- tens of seconds and non-hermetic. The dir is left absent so
    # scans short-circuit on `projects.is_dir()`; tests needing transcripts set
    # CLAUDE_CONFIG_DIR to their own fixture dir, which overrides this.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "_isolated_claude_home"))
    yield


@pytest.fixture(autouse=True)
def _no_network_sync() -> Iterator[None]:
    """Block optional outbound sync so ordinary tests stay hermetic."""
    with patch("lemoncrow.core.service.sync.sync_usage", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _no_external_search_in_tests(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isolate tests from zoekt and semantic search backends.

    * ``LEMONCROW_ZOEKT_LOC_THRESHOLD=100000000`` — tiny test repos (< 10 k LOC)
      never route through zoekt (which requires a built index the test fixtures
      don't provide).  The real default is 1 (all repos).
    * ``LEMONCROW_CODE_AUTOSYNC=0`` — one-shot test engines should not start
      background autosync workers. Tests that exercise autosync opt in explicitly.

    Tests that explicitly exercise zoekt, autosync, or semantic behaviour should
    override these env vars via their own ``monkeypatch.setenv`` call or engine
    constructor arguments.
    """
    from lemoncrow.infra.embeddings.factory import make_code_embedder
    from lemoncrow.pro.capabilities.cross_vendor_routing.configuration import _detect_configured_vendors_cached

    make_code_embedder.cache_clear()
    # Vendor detection is cached per process because a real process's env is
    # fixed; tests set vendor keys per test, so an earlier test must not freeze it.
    _detect_configured_vendors_cached.cache_clear()
    monkeypatch.setenv("LEMONCROW_ZOEKT_LOC_THRESHOLD", "100000000")
    monkeypatch.setenv("LEMONCROW_CODE_AUTOSYNC", "0")
    monkeypatch.setenv("LEMONCROW_CODE_EMBEDDER", "null")
    yield
    make_code_embedder.cache_clear()


@pytest.fixture(autouse=True)
def _live_review_prewarm_includes_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fixture repos live under tmp_path, which live Review prewarm skips as scratch.

    ``always`` keeps prewarm testable there; tests of the temp skip unset it.
    """
    monkeypatch.setenv("LEMONCROW_LIVE_REVIEW_PREWARM", "always")


@pytest.fixture(autouse=True)
def _no_ollama() -> Iterator[None]:
    """Block real Ollama calls so no test waits on a local LLM.

    Patches _ollama_module() — the single gateway all ollama_client functions
    (summarize, chat) use — so the mock works even for callers that did
    ``from lemoncrow.infra.internal_llm.ollama_client import summarize``.

    Tests that explicitly need LLM behaviour should override via monkeypatch.
    """
    from lemoncrow.infra.internal_llm import OllamaUnavailable

    with patch(
        "lemoncrow.infra.internal_llm.ollama_client._ollama_module",
        side_effect=OllamaUnavailable("ollama blocked in tests"),
    ):
        yield


@pytest.fixture(scope="session")
def retrieval_eval_runtime(tmp_path_factory: pytest.TempPathFactory) -> LemonCrowRuntimeCore:
    """Initialize lemoncrow runtime and seed blocks once per session for retrieval evaluation."""
    from tests.core.test_retriever_eval import _ensure_eval_blocks_exist, _init_runtime

    # Note: Using tmp_path_factory to get a persistent session directory
    root = tmp_path_factory.mktemp("retrieval_eval_session")
    runtime = _init_runtime(root)
    _ensure_eval_blocks_exist(runtime)
    return runtime


@pytest.fixture()
def store(tmp_path: Path) -> StoreBundle:
    """A StoreBundle over six fresh, physically-split SQLite files.

    Access the store you need explicitly: ``store.history``, ``store.knowledge``,
    ``store.lessons``, ``store.jobs``, ``store.memory``, ``store.telemetry``.
    """
    from lemoncrow.infra.storage.bundle import build_sqlite_store_bundle

    root = tmp_path / "lemoncrow"
    store = build_sqlite_store_bundle(root)
    store.init()
    return store


# --- Private code-intelligence availability ---------------------------------
# Runtime Lite, local Review and the public contracts are OSS. The advanced
# CodeContextEngine is supplied only by the private enterprise/runtime
# distribution. Its regression tests live in tests/private/ (mirror-denied and
# skipped there when the provider is absent). A public-surface test that needs
# the engine for one case carries the explicit ``private_engine`` marker;
# every other public test must import and pass with enterprise packages absent,
# so a stray private import fails loudly instead of being skipped by content.
def _private_code_intelligence_importable() -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec("lemoncrow_enterprise_runtime.code_intelligence.provider") is not None
    except (ImportError, ValueError):
        return False


_PRIVATE_CODE_INTEL_AVAILABLE = _private_code_intelligence_importable()


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``private_engine`` tests when enterprise/runtime is absent."""

    if _PRIVATE_CODE_INTEL_AVAILABLE:
        return
    skip = pytest.mark.skip(reason="requires the private code-intelligence engine (enterprise/runtime)")
    for item in items:
        if item.get_closest_marker("private_engine") is not None:
            item.add_marker(skip)
