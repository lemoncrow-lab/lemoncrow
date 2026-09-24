from __future__ import annotations

import json

from lemoncrow_client.runtime_attribution import record_runtime_policy_attribution
from lemoncrow_client.session import _unwrap


def test_runtime_attribution_is_disabled_without_explicit_path(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("LEMONCROW_RUNTIME_ATTRIBUTION_PATH", raising=False)
    record_runtime_policy_attribution({"runtime_policy": {"policy": "bounded-evidence-resolution"}})
    assert list(tmp_path.iterdir()) == []


def test_runtime_attribution_keeps_only_allowlisted_facts(monkeypatch, tmp_path) -> None:
    target = tmp_path / "policy.jsonl"
    monkeypatch.setenv("LEMONCROW_RUNTIME_ATTRIBUTION_PATH", str(target))
    record_runtime_policy_attribution(
        {
            "runtime_policy": {
                "policy": "bounded-evidence-resolution",
                "policy_version": "1-experiment",
                "mode": "experiment",
                "evidence_status": "ambiguous",
                "proposed_action": "EXPAND_RELATIONS",
                "actual_action": "EXPAND_RELATIONS",
                "rounds": 1,
                "candidate_count_before": 8,
                "candidate_count_after": 8,
                "elapsed_ms": 4,
                "confidence": 0.58,
                "reason_codes": ["low_rank_margin", "benchmark_experiment"],
                "query": "secret query",
                "path": "/private/repo/a.py",
                "source": "secret source",
                "session_id": "secret session",
            }
        }
    )

    row = json.loads(target.read_text(encoding="utf-8"))
    assert row["actual_action"] == "EXPAND_RELATIONS"
    assert row["rounds"] == 1
    raw = target.read_text(encoding="utf-8")
    assert "secret query" not in raw
    assert "/private/repo" not in raw
    assert "secret source" not in raw
    assert "secret session" not in raw


def test_unwrap_records_policy_without_exposing_diagnostics_to_tool_answer(monkeypatch, tmp_path) -> None:
    target = tmp_path / "policy.jsonl"
    monkeypatch.setenv("LEMONCROW_RUNTIME_ATTRIBUTION_PATH", str(target))
    answer = _unwrap(
        "code_search",
        {
            "tool": "code_search",
            "content": [{"type": "text", "text": "bounded answer"}],
            "view_revision": 7,
            "diagnostics": {
                "runtime_policy": {
                    "policy": "bounded-evidence-resolution",
                    "mode": "shadow",
                    "actual_action": "STOP",
                    "rounds": 0,
                }
            },
        },
        6,
    )

    assert answer.content[0]["text"] == "bounded answer"
    assert not hasattr(answer, "diagnostics")
    row = json.loads(target.read_text(encoding="utf-8"))
    assert row["mode"] == "shadow"
    assert row["actual_action"] == "STOP"
