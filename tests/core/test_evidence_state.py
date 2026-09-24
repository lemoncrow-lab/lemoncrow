from __future__ import annotations

from copy import deepcopy

from lemoncrow.pro.capabilities.code_context.evidence_state import evaluate_explore_evidence


def test_exact_hydrated_evidence_is_decisive_without_mutating_payload() -> None:
    payload = {
        "exact_match": True,
        "total_tokens": 220,
        "entry_points": [
            {"file_path": "src/orders.py", "start_line": 10, "end_line": 30, "score": 9.0},
        ],
        "files": [
            {
                "file_path": "src/orders.py",
                "source_sections": [{"start_line": 10, "end_line": 30, "content": "secret source"}],
            }
        ],
    }
    before = deepcopy(payload)

    state = evaluate_explore_evidence(payload, budget_tokens=2000)

    assert payload == before
    assert state.status == "decisive"
    assert state.confidence == 1.0
    assert state.evidence_refs == ("src/orders.py:L10-L30",)
    assert state.source_file_count == 1
    assert state.source_section_count == 1
    assert state.tokens_used == 220
    assert state.budget_tokens == 2000


def test_close_ranked_candidates_are_ambiguous() -> None:
    state = evaluate_explore_evidence(
        {
            "entry_points": [
                {"file_path": "src/a.py", "score": 10.0},
                {"file_path": "src/b.py", "score": 9.2},
            ],
            "files": [
                {"file_path": "src/a.py", "source_sections": []},
                {"file_path": "src/b.py", "source_sections": []},
            ],
        }
    )

    assert state.status == "ambiguous"
    assert state.rank_margin == 0.08
    assert "low_rank_margin" in state.reason_codes


def test_empty_single_pass_absent_and_dark_states_are_distinct() -> None:
    single_pass = evaluate_explore_evidence({"entry_points": [], "files": []})
    absent = evaluate_explore_evidence(
        {"entry_points": [], "files": []},
        search_verdict="absent",
    )
    dark = evaluate_explore_evidence(
        {"entry_points": [], "files": []},
        channels_requested=("semantic",),
        channels_live=(),
        dark_channels=("semantic",),
    )

    assert single_pass.status == "ambiguous"
    assert single_pass.reason_codes == ("no_candidates_single_pass",)
    assert absent.status == "absent"
    assert absent.reason_codes == ("reformulations_exhausted",)
    assert dark.status == "dark"
    assert dark.confidence == 0.0
    assert dark.reason_codes == ("channel_dark:semantic",)


def test_evidence_state_event_contains_only_structured_refs_and_metrics() -> None:
    state = evaluate_explore_evidence(
        {
            "entry_points": [{"file_path": "src/a.py", "start_line": 4, "end_line": 9, "score": 4.0}],
            "files": [{"file_path": "src/a.py", "source_sections": []}],
            "truncated": True,
            "cache_hit": True,
        },
        budget_tokens=800,
    )

    event = state.to_runtime_event(session_id="session-1")

    assert event.kind == "retrieval.evidence_state"
    assert event.mode == "shadow"
    assert event.evidence_refs == ("src/a.py:L4-L9",)
    assert event.proposed == event.actual
    assert event.metrics["truncated"] is True
    assert event.metrics["cache_hit"] is True


def test_real_engine_line_keys_and_none_recall_tails_are_supported() -> None:
    state = evaluate_explore_evidence(
        {
            "exact_match": True,
            "entry_points": [{"path": "src/a.py", "line": 3, "end_line": 8, "score": 5.0}],
            "files": [{"path": "src/a.py", "source_sections": []}],
            "additional_relevant_files": None,
            "fused_recall": None,
            "deep_recall": None,
        }
    )

    assert state.evidence_refs == ("src/a.py:L3-L8",)
    assert state.candidate_count == 1


def test_dark_optional_channel_does_not_override_concrete_evidence() -> None:
    state = evaluate_explore_evidence(
        {
            "entry_points": [{"path": "src/a.py", "line": 3, "end_line": 8, "score": 5.0}],
            "files": [{"path": "src/a.py", "source_sections": []}],
        },
        channels_requested=("semantic",),
        channels_live=(),
        dark_channels=("semantic",),
    )

    assert state.status == "useful"
    assert "channel_dark:semantic" in state.reason_codes
