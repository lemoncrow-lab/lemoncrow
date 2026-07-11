"""Tests for the tiered LLMReflector."""

from __future__ import annotations

import pytest

from lemoncrow.core.foundation import reflector
from lemoncrow.core.foundation.extractor import extract_candidate
from lemoncrow.core.foundation.models import Trace, ValidationResult
from lemoncrow.infra.internal_llm import InternalLLMError


def _trace() -> Trace:
    return Trace(
        id="t1",
        agent="tester",
        domain="coding",
        task="Fix flaky import in store module",
        status="success",
        errors_seen=["ImportError: cannot import name X"],
        diff_summary="reorder imports",
        validation_results=[ValidationResult(name="pytest", passed=True)],
    )


def test_local_tier_is_pure_heuristic(monkeypatch: pytest.MonkeyPatch) -> None:
    # Any LLM call would blow up; local tier must never reach it.
    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("LLM must not be called on the local tier")

    monkeypatch.setattr(reflector, "chat", _boom)
    monkeypatch.delenv("LEMONCROW_LLM_BACKEND", raising=False)

    trace = _trace()
    got = reflector.reflect(trace)
    expected = extract_candidate(trace)
    assert got.block.procedure == expected.block.procedure
    assert got.confidence == expected.confidence


def test_explicit_none_backend_is_heuristic() -> None:
    trace = _trace()
    got = reflector.reflect(trace, backend="none")
    assert got.block.procedure == extract_candidate(trace).block.procedure


def test_ollama_tier_enriches_candidate(monkeypatch: pytest.MonkeyPatch) -> None:
    def _fake_chat(messages: object, *, json_schema: object = None) -> dict[str, object]:
        return {
            "situation": "Sharper situation",
            "procedure": ["Move the cyclic import inside the function"],
            "dead_ends": ["Top-level import creates a cycle"],
            "verification": ["uv run pytest -q"],
            "failure_signals": ["ImportError at module load"],
            "when_not_to_apply": "When there is no import cycle",
        }

    monkeypatch.setattr(reflector, "chat", _fake_chat)
    trace = _trace()
    got = reflector.reflect(trace, backend="ollama")

    assert got.block.situation == "Sharper situation"
    assert "Move the cyclic import inside the function" in got.block.procedure
    assert "Top-level import creates a cycle" in got.block.dead_ends
    assert got.confidence > extract_candidate(trace).confidence
    # Heuristic procedure steps are preserved, not discarded.
    for step in extract_candidate(trace).block.procedure:
        assert step in got.block.procedure


def test_llm_error_falls_back_to_heuristic(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*_a: object, **_k: object) -> object:
        raise InternalLLMError("backend down")

    monkeypatch.setattr(reflector, "chat", _raise)
    trace = _trace()
    got = reflector.reflect(trace, backend="ollama")
    assert got.block.procedure == extract_candidate(trace).block.procedure


def test_non_dict_llm_response_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reflector, "chat", lambda *_a, **_k: "not-json")
    trace = _trace()
    got = reflector.reflect(trace, backend="openai")
    assert got.block.procedure == extract_candidate(trace).block.procedure
