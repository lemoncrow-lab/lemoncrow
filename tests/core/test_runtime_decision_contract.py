from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from lemoncrow.core.foundation.runtime_decisions import (
    NullRuntimeDecisionSink,
    RuntimeDecisionEvent,
    RuntimeDecisionSink,
)


def _event(**overrides) -> RuntimeDecisionEvent:
    values = {
        "id": "decision-1",
        "at": datetime(2026, 9, 23, 11, 0, tzinfo=UTC),
        "kind": "retrieval.expand",
        "phase": "retrieve",
        "policy": "evidence-state",
        "policy_version": "v1",
        "mode": "shadow",
        "session_id": "session-secret",
        "workspace_revision": "abc123",
        "evidence_refs": ("src/a.py:L10-L20",),
        "confidence": 0.72,
        "reason_codes": ("unresolved_symbol",),
        "proposed": {"action": "FOLLOW_SYMBOL", "query": "secret query"},
        "actual": {"action": "STOP", "path": "/private/repo/a.py"},
        "budget": {"rounds": 1, "context_tokens": 800},
        "metrics": {"eligible": True, "rank_margin": 0.12},
    }
    values.update(overrides)
    return RuntimeDecisionEvent(**values)


def test_runtime_decision_event_is_bounded_redacted_and_json_serializable() -> None:
    event = _event(
        proposed={
            "prompt": "do not persist me",
            "nested": {"command": "rm -rf /secret", "safe": "x" * 400},
        },
        reason_codes=tuple(f"reason-{i}" for i in range(100)),
        evidence_refs=tuple(f"src/file-{i}.py:L1-L2" for i in range(100)),
    )

    payload = event.to_payload()

    assert payload["proposed"]["prompt"] != "do not persist me"
    assert "redacted_sha256" in payload["proposed"]["prompt"]
    assert payload["proposed"]["nested"]["command"] != "rm -rf /secret"
    assert len(payload["proposed"]["nested"]["safe"]) == 256
    assert len(payload["reason_codes"]) == 64
    assert len(payload["evidence_refs"]) == 64
    assert payload["at"].startswith("2026-09-23T11:00:00")


def test_runtime_decision_event_validates_confidence_and_mode() -> None:
    with pytest.raises(ValidationError):
        _event(confidence=1.1)
    with pytest.raises(ValidationError):
        _event(mode="off")


def test_null_sink_satisfies_structural_protocol() -> None:
    sink = NullRuntimeDecisionSink()
    assert isinstance(sink, RuntimeDecisionSink)
    assert sink.record_runtime_decision(_event()) is None
