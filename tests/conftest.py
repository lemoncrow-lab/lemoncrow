"""Shared pytest fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from atelier.core.foundation.store import ContextStore

if TYPE_CHECKING:
    from atelier.gateway.adapters.runtime import ContextRuntime


@pytest.fixture(autouse=True)
def _no_network_sync() -> Iterator[None]:
    """Block all outbound sync_usage calls so no test ever hits atelier.beseam.com."""
    with patch("atelier.core.service.usage_sync.sync_usage", return_value=True):
        yield


@pytest.fixture(autouse=True)
def _no_ollama() -> Iterator[None]:
    """Block real Ollama calls so no test waits on a local LLM.

    Patches _ollama_module() — the single gateway all ollama_client functions
    (summarize, chat) use — so the mock works even for callers that did
    ``from atelier.infra.internal_llm.ollama_client import summarize``.

    Tests that explicitly need LLM behaviour should override via monkeypatch.
    """
    from atelier.infra.internal_llm.ollama_client import OllamaUnavailable

    with patch(
        "atelier.infra.internal_llm.ollama_client._ollama_module",
        side_effect=OllamaUnavailable("ollama blocked in tests"),
    ):
        yield


@pytest.fixture()
def store(tmp_path: Path) -> ContextStore:
    s = ContextStore(tmp_path / "atelier")
    s.init()
    return s


@pytest.fixture()
def seeded_runtime(tmp_path: Path) -> Iterator[ContextRuntime]:
    """Runtime backed by the bundled seed blocks + rubrics."""
    from importlib import resources

    import yaml

    from atelier.core.foundation.models import ReasonBlock, Rubric
    from atelier.gateway.adapters.runtime import ContextRuntime

    rt = ContextRuntime(root=tmp_path / "atelier")
    blocks_dir = resources.files("atelier") / "infra" / "seed_blocks"
    rubrics_dir = resources.files("atelier") / "core" / "rubrics"
    for p in blocks_dir.iterdir():
        if not p.name.endswith(".yaml"):
            continue
        data = yaml.safe_load(Path(str(p)).read_text(encoding="utf-8"))
        rt.store.upsert_block(ReasonBlock.model_validate(data))
    for p in rubrics_dir.iterdir():
        if not p.name.endswith(".yaml"):
            continue
        data = yaml.safe_load(Path(str(p)).read_text(encoding="utf-8"))
        rt.store.upsert_rubric(Rubric.model_validate(data))
    yield rt
