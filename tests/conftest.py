"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from lemoncrow.core.runtime import LemonCrowRuntimeCore
    from lemoncrow.infra.storage.bundle import StoreBundle


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
    """Block all outbound sync_usage calls so no test ever hits lemoncrow.beseam.com."""
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

    make_code_embedder.cache_clear()
    monkeypatch.setenv("LEMONCROW_ZOEKT_LOC_THRESHOLD", "100000000")
    monkeypatch.setenv("LEMONCROW_CODE_AUTOSYNC", "0")
    monkeypatch.setenv("LEMONCROW_CODE_EMBEDDER", "null")
    yield
    make_code_embedder.cache_clear()


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


# --- Open-core: exclude IP-behavior tests when the compiled IP modules are shimmed ---
#
# On the public mirror, scripts/mirror.py injects degraded open-core shims
# (marked ``__open_core_shim__``) at the compiled-only IP module paths denied in
# release/public-paths.txt. Those modules' behavior tests assert the real
# algorithm output, so they cannot pass against a shim. When a shim is active we
# exclude them so public CI stays green; the private repo (which has the real
# modules) runs every one of them normally, so this is a no-op in dev.
def _open_core_shim_active() -> bool:
    try:
        from lemoncrow.core.capabilities.code_context import ranking

        return bool(getattr(ranking, "__open_core_shim__", False))
    except Exception:  # noqa: BLE001 - best-effort probe, must never break collection
        return False


# Whole files whose purpose is to verify a closed module (excluded wholesale on the
# shimmed public tree; some import private IP helpers, so file-level ignore also
# avoids collection errors).
_IP_BEHAVIOR_TEST_FILES = (
    "core/test_minify_hardening.py",
    "core/test_minify_projection.py",
    "core/test_projection_mapping.py",
    "core/test_compact_projection.py",
    "core/test_compact_projection_structural.py",
    "core/test_rich_edit.py",
    "core/test_search_verdict.py",
    "core/test_code_context_rerank.py",
    "core/test_code_context_renderer.py",
    "core/test_code_context.py",
    "core/test_code_embedder_path.py",
    "core/test_embedding_batch.py",
    "core/test_fusion_weights_eval.py",
    "core/test_call_graph_centrality.py",
    "core/test_edge_synthesis.py",
    "core/test_edge_synthesis_frameworks.py",
    "core/test_edge_resolution.py",
    "core/test_lsp_resolver_bridge.py",
    "core/test_ann_symbol_index.py",
    "core/capabilities/prompt_compilation/test_compiler.py",
    "gateway/test_mcp_read_projection.py",
    "gateway/test_code_render_payload.py",
)

# Individual IP-behavior tests that live inside otherwise-public integration/
# end-to-end files (matched as a substring of the test node id). These assert
# render/projection/savings/semantic output that the shims degrade; they pass on
# the real modules (dev) and are excluded only when a shim is active.
_IP_BEHAVIOR_TEST_IDS = (
    "test_service_api.py::test_file_projection_endpoint_returns_compact_projection_metadata",
    "test_p0_mcp_surfaces.py::test_tool_code_cache_status_rendered_shape_is_compact",
    "test_p0_mcp_surfaces.py::test_tool_code_callers_rendered_shape_excludes_source",
    "test_p0_mcp_surfaces.py::test_tool_code_index_rendered_shape_is_compact",
    "test_p0_mcp_surfaces.py::test_tool_code_search_can_attach_compact_rendered_block",
    "test_p0_mcp_surfaces.py::test_tool_code_search_semantic_unavailable_without_embedder",
    "test_p0_mcp_surfaces.py::test_tool_code_symbol_rendered_shape_includes_numbered_body",
    "test_mcp_tool_handlers.py::test_code_context_cache_diagnostics_surface_is_additive",
    "test_mcp_tool_handlers.py::test_code_context_mcp_surfaces",
    "test_mcp_tool_handlers.py::test_code_context_pattern_search_surface_is_cached",
    "test_mcp_tool_handlers.py::test_code_context_usages_surface_groups_references",
    "test_runtime_hygiene_spill_compact.py::test_auto_compact_code_is_ast_aware",
    "test_smart_read_outline_first.py::test_smart_read_minified_projection_banner_for_safe_language",
    "test_scoped_context.py::test_pull_respects_budget",
    "test_line_skimmer.py::test_pull_skims_when_flag_on",
    "test_owned_execution_lanes.py::test_execute_owned_prompt_returns_structured_receipt",
    "test_pro_runtime_gates.py::test_read_uses_source_projection_without_pro",
)

if _open_core_shim_active():
    collect_ignore = list(_IP_BEHAVIOR_TEST_FILES)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """On the shimmed public tree, deselect IP-behavior tests embedded in public files."""
    if not _open_core_shim_active():
        return
    keep: list[pytest.Item] = []
    drop: list[pytest.Item] = []
    for item in items:
        (drop if any(tid in item.nodeid for tid in _IP_BEHAVIOR_TEST_IDS) else keep).append(item)
    if drop:
        config.hook.pytest_deselected(items=drop)
        items[:] = keep
