"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from atelier.core.foundation.store import ContextStore
    from atelier.core.runtime import AtelierRuntimeCore


@pytest.fixture(autouse=True)
def _isolate_workspace_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Isolate tests from host workspace env vars and default runtime roots."""
    for env_var in (
        "ATELIER_WORKSPACE_ROOT",
        "CLAUDE_WORKSPACE_ROOT",
        "CURSOR_WORKSPACE_ROOT",
        "VSCODE_CWD",
        "ATELIER_LESSONS_ROOT",
        "ATELIER_STORE_ROOT",
        "ATELIER_MEM_ROOT",
    ):
        monkeypatch.delenv(env_var, raising=False)
    isolated_root = tmp_path / ".atelier"
    monkeypatch.setenv("ATELIER_ROOT", str(isolated_root))
    monkeypatch.setenv("ATELIER_STORE_ROOT", str(isolated_root))
    # Complete the isolation: point the workspace root at tmp_path too. Without
    # this, _workspace_root() falls through to os.getcwd() (the real repo), so
    # the new read/projection workspace-confinement rejects files tests create
    # under tmp_path. Tests that need a specific workspace set it themselves.
    monkeypatch.setenv("ATELIER_WORKSPACE_ROOT", str(tmp_path))
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
    """Block all outbound sync_usage calls so no test ever hits atelier.beseam.com."""
    with patch("atelier.core.service.sync.sync_usage", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _no_external_search_in_tests(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate tests from zoekt and semantic search backends.

    * ``ATELIER_ZOEKT_LOC_THRESHOLD=100000000`` — tiny test repos (< 10 k LOC)
      never route through zoekt (which requires a built index the test fixtures
      don't provide).  The real default is 1 (all repos).

    * ``ATELIER_CODE_EMBEDDER=null`` — suppress the local feature-hashing
      embedder so ``_build_symbol_embeddings`` is a no-op during index_repo()
      and ``_semantic_candidate_files`` returns empty.  Without this, the ANN
      search surfaces all sibling files, breaking tests whose intent is that
      ``complete_families=False`` keeps only the seed file.

    Tests that explicitly exercise zoekt or semantic behaviour should override
    these env vars via their own ``monkeypatch.setenv`` call.
    """
    from atelier.infra.embeddings.factory import make_code_embedder

    make_code_embedder.cache_clear()
    monkeypatch.setenv("ATELIER_ZOEKT_LOC_THRESHOLD", "100000000")
    monkeypatch.setenv("ATELIER_CODE_EMBEDDER", "null")
    yield
    make_code_embedder.cache_clear()


@pytest.fixture(autouse=True)
def _no_ollama() -> Iterator[None]:
    """Block real Ollama calls so no test waits on a local LLM.

    Patches _ollama_module() — the single gateway all ollama_client functions
    (summarize, chat) use — so the mock works even for callers that did
    ``from atelier.infra.internal_llm.ollama_client import summarize``.

    Tests that explicitly need LLM behaviour should override via monkeypatch.
    """
    from atelier.infra.internal_llm import OllamaUnavailable

    with patch(
        "atelier.infra.internal_llm.ollama_client._ollama_module",
        side_effect=OllamaUnavailable("ollama blocked in tests"),
    ):
        yield


@pytest.fixture(scope="session")
def retrieval_eval_runtime(tmp_path_factory: pytest.TempPathFactory) -> AtelierRuntimeCore:
    """Initialize atelier runtime and seed blocks once per session for retrieval evaluation."""
    from tests.core.test_retriever_eval import _ensure_eval_blocks_exist, _init_runtime

    # Note: Using tmp_path_factory to get a persistent session directory
    root = tmp_path_factory.mktemp("retrieval_eval_session")
    runtime = _init_runtime(root)
    _ensure_eval_blocks_exist(runtime)
    return runtime


@pytest.fixture()
def store(tmp_path: Path) -> ContextStore:
    from atelier.core.foundation.store import ContextStore

    root = tmp_path / "atelier"
    store = ContextStore(root)
    store.init()
    return store
