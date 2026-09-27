from __future__ import annotations

from experiments.retrieval_symbol_vote.training_split import (
    ranker_deployment_gate,
    select_validation_tasks,
)


def test_split_guarantees_minimum_and_leaves_training_tasks() -> None:
    tasks = [f"task-{index}" for index in range(20)]
    validation = select_validation_tasks(tasks, validation_fraction=0.2, min_tasks=4)

    assert len(validation) >= 4
    assert len(validation) < len(tasks)
    assert validation == select_validation_tasks(tasks, validation_fraction=0.2, min_tasks=4)


def test_split_handles_tiny_corpora() -> None:
    assert select_validation_tasks([], min_tasks=3) == frozenset()
    assert select_validation_tasks(["only"], min_tasks=3) == frozenset({"only"})
    assert len(select_validation_tasks(["a", "b"], min_tasks=3)) == 1


def test_split_is_order_independent() -> None:
    tasks = ["c", "a", "b", "d", "e"]
    assert select_validation_tasks(tasks, validation_fraction=0.4, min_tasks=2) == select_validation_tasks(
        list(reversed(tasks)), validation_fraction=0.4, min_tasks=2
    )


def test_deployment_gate_rejects_export_parity_mismatch() -> None:
    enabled, reasons = ranker_deployment_gate(
        mrr_gain=0.1,
        min_mrr_gain=0.003,
        learned_hit3=1.0,
        baseline_hit3=1.0,
        latency_p95_ms=0.1,
        max_latency_ms=1.0,
        parity_mismatch=1,
    )

    assert enabled is False
    assert reasons == ("export parity mismatch on 1 validation group(s)",)


def test_deployment_gate_accepts_clean_model() -> None:
    enabled, reasons = ranker_deployment_gate(
        mrr_gain=0.01,
        min_mrr_gain=0.003,
        learned_hit3=1.0,
        baseline_hit3=1.0,
        latency_p95_ms=0.1,
        max_latency_ms=1.0,
        parity_mismatch=0,
    )

    assert enabled is True
    assert reasons == ()


def test_conservative_policy_suppresses_weak_top_change() -> None:
    from experiments.retrieval_symbol_vote.training_split import rank_with_conservative_policy

    labels = [0, 0, 1]
    # Candidate 2 barely beats the original top; a margin preserves baseline rank 3.
    assert rank_with_conservative_policy([0.5, 0.4, 0.51], labels, margin=0.02) == 3
    # With no margin, the learned promotion moves the positive to rank one.
    assert rank_with_conservative_policy([0.5, 0.4, 0.51], labels, margin=0.0) == 1


def test_conservative_policy_preserves_lower_order_when_top_unchanged() -> None:
    from experiments.retrieval_symbol_vote.training_split import rank_with_conservative_policy

    labels = [0, 0, 1]
    assert rank_with_conservative_policy([1.0, 0.1, 0.9], labels) == 3


def test_collection_mp_context_uses_spawn_for_cuda_embedder(monkeypatch) -> None:
    from experiments.retrieval_symbol_vote.training_split import collection_mp_context_name

    monkeypatch.setenv("LEMONCROW_CODE_EMBEDDER", "bge")
    monkeypatch.delenv("LEMONCROW_TRAIN_MP_START_METHOD", raising=False)
    assert collection_mp_context_name() == "spawn"


def test_collection_mp_context_honors_override(monkeypatch) -> None:
    from experiments.retrieval_symbol_vote.training_split import collection_mp_context_name

    monkeypatch.setenv("LEMONCROW_CODE_EMBEDDER", "bge")
    monkeypatch.setenv("LEMONCROW_TRAIN_MP_START_METHOD", "fork")
    assert collection_mp_context_name() == "fork"


def test_deployment_gate_rejects_all_real_safety_regression() -> None:
    enabled, reasons = ranker_deployment_gate(
        mrr_gain=0.1,
        min_mrr_gain=0.005,
        learned_hit3=1.0,
        baseline_hit3=1.0,
        latency_p95_ms=0.5,
        max_latency_ms=5.0,
        parity_mismatch=0,
        safety_mrr_gain=-0.001,
        safety_learned_hit1=0.5,
        safety_baseline_hit1=0.5,
        safety_learned_hit3=0.75,
        safety_baseline_hit3=0.8,
    )
    assert enabled is False
    assert "all-real safety MRR regressed" in "; ".join(reasons)
    assert "all-real safety hit@3 regressed" in reasons


def test_deployment_gate_accepts_nonregressing_all_real_safety() -> None:
    enabled, reasons = ranker_deployment_gate(
        mrr_gain=0.1,
        min_mrr_gain=0.005,
        learned_hit3=1.0,
        baseline_hit3=1.0,
        latency_p95_ms=0.5,
        max_latency_ms=5.0,
        parity_mismatch=0,
        safety_mrr_gain=0.01,
        safety_learned_hit1=0.6,
        safety_baseline_hit1=0.5,
        safety_learned_hit3=0.8,
        safety_baseline_hit3=0.8,
    )
    assert enabled is True
    assert reasons == ()
