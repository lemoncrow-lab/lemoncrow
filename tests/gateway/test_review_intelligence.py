from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.infra.internal_llm import InternalLLMError
from lemoncrow.pro.capabilities.review.intelligence_runtime import (
    IntelligenceRunRequest,
    ModelCallBudget,
    ModelCallUsage,
    StructuredExecution,
    build_intelligence_identity,
    execute_structured,
    run_intelligence,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id


def _store(tmp_path: Path) -> tuple[ReviewStore, ReviewSession, ReviewRevision]:
    store = ReviewStore(tmp_path)
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="local_change",
            repo_root="/not/durable/identity",
            range_mode="working_tree",
            title="intelligence test",
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="tree-aaa",
            source_fingerprint="source-aaa",
            packet_schema_version=1,
        ),
        [],
    )
    return store, session, revision


def _request(
    session: ReviewSession,
    revision: ReviewRevision,
    *,
    context: Any | None = None,
    policy: Any | None = None,
    snapshot_identity: str = "source-aaa",
    driver: str = "openai",
    model: str = "test-model",
    budget: ModelCallBudget | None = None,
) -> IntelligenceRunRequest:
    return IntelligenceRunRequest(
        review_id=session.id,
        revision_id=revision.id,
        snapshot_identity=snapshot_identity,
        capability="ask",
        scope={"path": "src/a.py", "lines": [1, 3]},
        context={"question": "why", "input": [1, 2]} if context is None else context,
        policy={"read_only": True} if policy is None else policy,
        driver=driver,
        model=model,
        output_schema={
            "type": "object",
            "properties": {"body": {"type": "string"}},
            "required": ["body"],
            "additionalProperties": False,
        },
        prompt_version=1,
        schema_version=1,
        budget=budget or ModelCallBudget(max_calls=1, max_review_calls=20),
    )


def _execution(*, body: str = "ok", driver: str = "openai", model: str = "test-model") -> StructuredExecution:
    return StructuredExecution(
        result={"body": body},
        driver=driver,  # type: ignore[arg-type]
        model=model,
        usage=ModelCallUsage(calls=1, input_tokens=7, output_tokens=3, request_id="req-1"),
    )


def test_digest_is_stable_and_invalidates_policy_model_and_snapshot() -> None:
    kwargs = {
        "revision_id": "rev-1",
        "snapshot_identity": "snapshot-a",
        "capability": "ask",
        "scope": {"b": 2, "a": 1},
        "context": {"question": "why", "paths": ["a.py"]},
        "policy": {"rules": ["read-only"]},
        "driver": "openai",
        "model": "m1",
        "output_schema": {"type": "object", "properties": {"body": {"type": "string"}}},
        "prompt_version": 2,
        "schema_version": 3,
    }
    first = build_intelligence_identity(**kwargs)
    reordered = build_intelligence_identity(**{**kwargs, "scope": {"a": 1, "b": 2}})
    assert reordered == first
    assert build_intelligence_identity(**{**kwargs, "policy": {"rules": ["different"]}}).run_key != first.run_key
    assert build_intelligence_identity(**{**kwargs, "model": "m2"}).run_key != first.run_key
    assert build_intelligence_identity(**{**kwargs, "snapshot_identity": "snapshot-b"}).run_key != first.run_key


def test_cache_hit_reuses_durable_result_without_provider_call(tmp_path: Path) -> None:
    store, session, revision = _store(tmp_path)
    request = _request(session, revision)
    calls = 0

    def execute() -> StructuredExecution:
        nonlocal calls
        calls += 1
        return _execution()

    first = run_intelligence(store=store, request=request, executor=execute)
    second = run_intelligence(store=store, request=request, executor=execute)

    assert first.status == "ready" and first.provider_calls_this_request == 1
    assert second.status == "ready" and second.cache_hit and second.provider_calls_this_request == 0
    assert second.run_id == first.run_id
    assert calls == 1
    assert store.intelligence_calls(session.id) == 1
    assert store.synthesis_calls(session.id) == 1


def test_concurrent_identical_requests_converge_on_one_provider_call(tmp_path: Path) -> None:
    store, session, revision = _store(tmp_path)
    request = _request(session, revision, budget=ModelCallBudget(max_calls=1, max_review_calls=5, timeout_s=5))
    entered = threading.Event()
    release = threading.Event()
    calls = 0
    calls_lock = threading.Lock()

    def execute() -> StructuredExecution:
        nonlocal calls
        with calls_lock:
            calls += 1
        entered.set()
        assert release.wait(timeout=3)
        return _execution(body="shared")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_intelligence, store=store, request=request, executor=execute)
        assert entered.wait(timeout=2)
        second = pool.submit(run_intelligence, store=store, request=request, executor=execute)
        time.sleep(0.05)
        assert calls == 1
        release.set()
        outcomes = (first.result(timeout=3), second.result(timeout=3))

    assert calls == 1
    assert {item.run_id for item in outcomes} == {outcomes[0].run_id}
    assert sorted(item.provider_calls_this_request for item in outcomes) == [0, 1]
    assert all(item.status == "ready" and item.result["body"] == "shared" for item in outcomes)


def test_retry_stays_within_run_and_review_call_budgets(tmp_path: Path) -> None:
    store, session, revision = _store(tmp_path)
    request = _request(
        session,
        revision,
        budget=ModelCallBudget(max_calls=2, max_review_calls=2),
    )
    calls = 0

    def execute() -> StructuredExecution:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient")
        return _execution()

    outcome = run_intelligence(store=store, request=request, executor=execute)
    assert outcome.status == "ready"
    assert outcome.provider_calls_this_request == 2
    assert outcome.usage.calls == 2
    assert outcome.usage.input_tokens is None  # failed attempt made total token usage unknowable
    assert calls == 2
    assert store.intelligence_calls(session.id) == 2


def test_review_budget_exhaustion_is_hard_and_does_not_invoke_provider(tmp_path: Path) -> None:
    store, session, revision = _store(tmp_path)
    one_call = ModelCallBudget(max_calls=1, max_review_calls=1)
    first = run_intelligence(
        store=store,
        request=_request(session, revision, context={"q": "one"}, budget=one_call),
        executor=_execution,
    )
    invoked = False

    def should_not_run() -> StructuredExecution:
        nonlocal invoked
        invoked = True
        return _execution()

    second = run_intelligence(
        store=store,
        request=_request(session, revision, context={"q": "two"}, budget=one_call),
        executor=should_not_run,
    )
    assert first.status == "ready"
    assert second.status == "failed"
    assert "budget exhausted" in second.error.lower()
    assert not invoked
    assert store.intelligence_calls(session.id) == 1


def test_cancelled_and_failed_runs_are_explicit(tmp_path: Path) -> None:
    store, session, revision = _store(tmp_path)
    cancelled = run_intelligence(
        store=store,
        request=_request(session, revision, context={"q": "cancel"}),
        executor=lambda: _execution(),
        cancellation=lambda: True,
    )
    assert cancelled.status == "cancelled"
    assert cancelled.provider_calls_this_request == 0

    def fail() -> StructuredExecution:
        raise RuntimeError("provider down")

    failed = run_intelligence(
        store=store,
        request=_request(session, revision, context={"q": "fail"}),
        executor=fail,
    )
    assert failed.status == "failed"
    assert failed.provider_calls_this_request == 1
    assert "provider down" in failed.error
    statuses = {row.status for row in store.list_intelligence_runs(session.id)}
    assert {"cancelled", "failed"} <= statuses


def test_cli_review_execution_requires_immutable_snapshot_root() -> None:
    with pytest.raises(InternalLLMError, match="immutable Review snapshot"):
        execute_structured(
            system="system",
            prompt="prompt",
            output_schema={"type": "object"},
            driver="claude",
            model="sonnet",
            project_root=None,
            origin="machine",
        )


def test_runtime_does_not_mutate_human_state_or_persist_private_reasoning(tmp_path: Path) -> None:
    store, session, revision = _store(tmp_path)
    before_revisions = store.list_revisions(session.id)
    before_marks = store.list_marks(session.id)
    before_annotations = store.list_annotations(session.id)

    outcome = run_intelligence(
        store=store,
        request=_request(session, revision),
        executor=lambda: StructuredExecution(
            result={
                "body": "public",
                "evidence": [{"path": "src/a.py", "line": 2}],
                "chain_of_thought": "secret-cot",
                "nested": {"hidden_reasoning": "secret-hidden", "ok": True},
            },
            driver="openai",
            model="test-model",
            usage=ModelCallUsage(calls=1),
        ),
    )
    assert outcome.status == "ready"
    assert "chain_of_thought" not in outcome.result
    assert outcome.result["nested"] == {"ok": True}
    row = store.list_intelligence_runs(session.id)[0]
    assert "secret-cot" not in row.result_json
    assert "secret-hidden" not in row.result_json
    assert "/not/durable/identity" not in row.run_key
    assert json.loads(row.result_json)["evidence"][0]["path"] == "src/a.py"

    assert store.list_revisions(session.id) == before_revisions
    assert store.list_marks(session.id) == before_marks
    assert store.list_annotations(session.id) == before_annotations


def test_historical_run_reads_never_trigger_fresh_intelligence(tmp_path: Path) -> None:
    store, session, revision = _store(tmp_path)
    calls = 0

    def execute() -> StructuredExecution:
        nonlocal calls
        calls += 1
        return _execution()

    request = _request(session, revision)
    ready = run_intelligence(store=store, request=request, executor=execute)
    assert ready.status == "ready" and calls == 1

    rows = store.list_intelligence_runs(session.id, revision_id=revision.id, capability="ask")
    assert len(rows) == 1
    assert store.get_intelligence_run(session.id, rows[0].run_key) == rows[0]
    assert calls == 1


def test_policy_model_context_and_snapshot_changes_miss_the_cache(tmp_path: Path) -> None:
    store, session, revision = _store(tmp_path)
    calls = 0

    def execute_for(request: IntelligenceRunRequest) -> StructuredExecution:
        nonlocal calls
        calls += 1
        return _execution(driver=request.driver or "openai", model=request.model or "test-model")

    requests = [
        _request(session, revision),
        _request(session, revision, policy={"read_only": True, "rules": ["new"]}),
        _request(session, revision, context={"question": "different"}),
        _request(session, revision, model="other-model"),
        _request(session, revision, snapshot_identity="source-bbb"),
    ]
    outcomes = [
        run_intelligence(store=store, request=request, executor=lambda request=request: execute_for(request))
        for request in requests
    ]

    assert calls == len(requests)
    assert all(item.status == "ready" and not item.cache_hit for item in outcomes)
    assert len({item.run_id for item in outcomes}) == len(requests)

    cached = run_intelligence(
        store=store,
        request=requests[0],
        executor=lambda: pytest.fail("identical request should not invoke the provider"),
    )
    assert cached.cache_hit
    assert cached.provider_calls_this_request == 0


def test_concurrent_different_requests_cannot_overspend_review_budget(tmp_path: Path) -> None:
    store, session, revision = _store(tmp_path)
    budget = ModelCallBudget(max_calls=1, max_review_calls=1, timeout_s=5)
    requests = (
        _request(session, revision, context={"q": "one"}, budget=budget),
        _request(session, revision, context={"q": "two"}, budget=budget),
    )
    calls = 0
    calls_lock = threading.Lock()
    release = threading.Event()

    def execute() -> StructuredExecution:
        nonlocal calls
        with calls_lock:
            calls += 1
        assert release.wait(timeout=3)
        return _execution()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(run_intelligence, store=store, request=request, executor=execute) for request in requests
        ]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            with calls_lock:
                if calls:
                    break
            time.sleep(0.01)
        release.set()
        outcomes = [future.result(timeout=3) for future in futures]

    assert calls == 1
    assert store.intelligence_calls(session.id) == 1
    assert sorted(item.status for item in outcomes) == ["failed", "ready"]
    failed = next(item for item in outcomes if item.status == "failed")
    assert "budget exhausted" in failed.error.lower()


def test_local_subscription_cost_budget_never_invents_dollar_cost(tmp_path: Path) -> None:
    store, session, revision = _store(tmp_path)
    invoked = False

    def execute() -> StructuredExecution:
        nonlocal invoked
        invoked = True
        return _execution(driver="claude", model="sonnet")

    outcome = run_intelligence(
        store=store,
        request=_request(
            session,
            revision,
            driver="claude",
            model="sonnet",
            budget=ModelCallBudget(max_calls=1, max_review_calls=5, max_cost=0.50),
        ),
        executor=execute,
    )

    assert outcome.status == "failed"
    assert "does not expose enforceable provider cost metadata" in outcome.error
    assert outcome.usage.provider_cost is None
    assert not invoked
    assert store.intelligence_calls(session.id) == 0
