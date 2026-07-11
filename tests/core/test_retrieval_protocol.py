"""Domain-neutral Retriever protocol: conformance + injection seam."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.capabilities.code_context import CodeContextEngine, CodeRetriever
from lemoncrow.core.capabilities.retrieval import Retriever, default_retriever_factory
from lemoncrow.core.runtime.engine import LemonCrowRuntimeCore


class FakeRetriever:
    """Minimal non-code retriever conforming to the protocol."""

    def __init__(self, corpus_id: str = "docs-corpus") -> None:
        self._corpus_id = corpus_id
        self.calls: list[dict[str, Any]] = []

    @property
    def source_id(self) -> str:
        return self._corpus_id

    def retrieve(
        self,
        query: str,
        *,
        budget_tokens: int = 2000,
        max_items: int = 8,
        seeds: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append({"query": query, "budget_tokens": budget_tokens})
        return {"matches": [], "query": query}


def test_code_context_engine_conforms_to_retriever(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def f():\n    return 1\n")
    engine = CodeContextEngine(tmp_path, autosync_enabled=False)
    assert isinstance(engine, Retriever)
    assert engine.source_id == engine.repo_id
    assert CodeRetriever is CodeContextEngine


def test_engine_retrieve_delegates_to_tool_explore(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    engine = CodeContextEngine(tmp_path, autosync_enabled=False)
    captured: dict[str, Any] = {}

    def fake_explore(query: str, **kwargs: Any) -> dict[str, Any]:
        captured["query"] = query
        captured.update(kwargs)
        return {"ok": True}

    monkeypatch.setattr(engine, "tool_explore", fake_explore)
    result = engine.retrieve("find the thing", budget_tokens=512, max_items=3, seeds=["a.py"])
    assert result == {"ok": True}
    assert captured["query"] == "find the thing"
    assert captured["budget_tokens"] == 512
    assert captured["max_files"] == 3
    assert captured["seed_files"] == ["a.py"]


def test_fake_retriever_conforms() -> None:
    assert isinstance(FakeRetriever(), Retriever)


def test_default_retriever_factory_is_code_vertical(tmp_path: Path) -> None:
    retriever = default_retriever_factory(tmp_path)
    assert isinstance(retriever, CodeContextEngine)


def test_runtime_core_accepts_injected_retriever_factory(tmp_path: Path) -> None:
    fake = FakeRetriever()
    core = LemonCrowRuntimeCore(tmp_path / "store", retriever_factory=lambda root: fake)
    assert core.retriever_factory(tmp_path) is fake


def test_runtime_core_defaults_to_code_retriever(tmp_path: Path) -> None:
    core = LemonCrowRuntimeCore(tmp_path / "store")
    assert core.retriever_factory is default_retriever_factory


def test_get_context_uses_injected_retriever_for_bootstrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeRetriever(corpus_id="docs-corpus")
    core = LemonCrowRuntimeCore(tmp_path / "store", retriever_factory=lambda root: fake)
    monkeypatch.setattr(
        "lemoncrow.core.runtime.engine.resolve_workspace_root",
        lambda root: tmp_path,
    )
    payload = core.get_context(task="summarize onboarding docs", agent_id="tester", recall=False)
    assert isinstance(payload, dict)
    assert payload["bootstrap"]["repo_id"] == "docs-corpus"
