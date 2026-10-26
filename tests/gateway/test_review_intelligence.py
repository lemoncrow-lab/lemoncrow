from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.infra.internal_llm import InternalLLMError
from lemoncrow.pro.capabilities.review.intelligence_runtime import (
    IntelligenceRunOutcome,
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


# --------------------------------------------------------------------------- #
# the per-call cost ceiling: the provider's figure, else the exact local price
# --------------------------------------------------------------------------- #

#: model -> (input, output, cache hit, 5m cache write), USD per million tokens (Anthropic's pricing page, pricing.yaml).
RATES_5X: dict[str, tuple[float, float, float, float]] = {
    "claude-sonnet-5-5": (2.0, 10.0, 0.10, 2.50),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.0),
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.5),
}


def _ceiling_outcome(
    tmp_path: Path,
    *,
    model: str,
    usage: ModelCallUsage,
    max_cost: float,
    driver: str = "litellm",
    max_calls: int = 1,
    executor: Callable[[], StructuredExecution] | None = None,
) -> IntelligenceRunOutcome:
    store, session, revision = _store(tmp_path)

    def execute() -> StructuredExecution:
        return StructuredExecution(result={"body": "ok"}, driver=driver, model=model, usage=usage)  # type: ignore[arg-type]

    return run_intelligence(
        store=store,
        request=_request(
            session,
            revision,
            driver=driver,
            model=model,
            budget=ModelCallBudget(max_calls=max_calls, max_review_calls=5, max_cost=max_cost),
        ),
        executor=executor or execute,
    )


def _reported_tokens(prompt: int, output: int, *, read: int = 0, write: int = 0) -> ModelCallUsage:
    """What an API route reports when the provider gives no dollar figure (prompt tokens include the cached part)."""

    return ModelCallUsage(
        calls=1,
        input_tokens=prompt,
        output_tokens=output,
        cache_read_input_tokens=read,
        cache_write_input_tokens=write,
        request_id="req-1",
    )


@pytest.fixture
def price_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A LemonCrow store root with its own (empty) ``pricing.yaml`` slot, and the price table reloaded around it."""

    from lemoncrow.core.capabilities import pricing

    root = tmp_path / "lemoncrow-root"
    root.mkdir()
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.delenv("LEMONCROW_PRICING_LIVE", raising=False)

    def reload() -> None:
        pricing._load_pricing_table.cache_clear()
        pricing._nearest_table_match.cache_clear()

    reload()
    yield root.resolve()
    monkeypatch.undo()
    reload()


def test_a_provider_reported_cost_is_compared_as_before_and_the_table_is_not_consulted(tmp_path: Path) -> None:
    # An unlisted model: only the provider's figure can price this call.
    used = ModelCallUsage(calls=1, input_tokens=10, output_tokens=5, provider_cost=0.02, currency="USD")

    allowed = _ceiling_outcome(tmp_path / "a", model="vendor-model-x", usage=used, max_cost=0.05)
    refused = _ceiling_outcome(tmp_path / "b", model="vendor-model-x", usage=used, max_cost=0.01)
    # A priced model whose own table figure would be far over the ceiling: the provider's word wins.
    cheap = ModelCallUsage(calls=1, input_tokens=100_000, output_tokens=2_000, provider_cost=0.001, currency="USD")
    provider_wins = _ceiling_outcome(tmp_path / "c", model="claude-sonnet-5-5", usage=cheap, max_cost=0.01)

    assert allowed.status == "ready" and allowed.usage.provider_cost == 0.02
    assert refused.status == "failed" and refused.error == "Review Intelligence provider cost budget exceeded"
    assert provider_wins.status == "ready"


@pytest.mark.parametrize("model", sorted(RATES_5X))
def test_a_call_with_no_provider_cost_is_priced_from_the_exact_local_table(tmp_path: Path, model: str) -> None:
    rate_in, rate_out, rate_read, rate_write = RATES_5X[model]
    # 100,000 prompt tokens, 40,000 of them read from the cache and 10,000 written to it: 50,000 are plain input. A
    # prompt of exactly 100,000 is still under Haiku 5.5's prompt-length tier.
    used = _reported_tokens(100_000, 2_000, read=40_000, write=10_000)
    cost = (50_000 * rate_in + 2_000 * rate_out + 40_000 * rate_read + 10_000 * rate_write) / 1_000_000

    under = _ceiling_outcome(tmp_path / "under", model=model, usage=used, max_cost=cost * 1.01)
    over = _ceiling_outcome(tmp_path / "over", model=model, usage=used, max_cost=cost * 0.99)

    assert under.status == "ready", under.error
    assert over.status == "failed"
    assert "cost budget exceeded" in over.error and "local price table" in over.error
    for outcome in (under, over):
        assert outcome.usage.provider_cost is None  # the table's figure is a price, never reported as the provider's


@pytest.mark.parametrize("model", sorted(RATES_5X))
def test_the_litellm_route_without_a_response_cost_is_priced_not_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: str
) -> None:
    """LiteLLM 1.91.0 returns ``response_cost=None`` for these ids: the whole chain, from its response to the ceiling."""

    from lemoncrow.infra.internal_llm import litellm_client

    class Message:
        content = '{"body": "ok"}'

    class Choice:
        message = Message()

    class Response:
        def __init__(self) -> None:
            self.choices = [Choice()]
            self.id = "litellm-response"
            self._hidden_params: dict[str, Any] = {}  # no response_cost: LiteLLM's bundled price map lacks the model
            self.usage = {
                "prompt_tokens": 20_000,
                "completion_tokens": 500,
                "prompt_tokens_details": {"cached_tokens": 15_000},
                "cache_creation_input_tokens": 0,
            }

    class FakeLiteLLM:
        def completion(self, **_: Any) -> Response:
            return Response()

    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: FakeLiteLLM())
    rate_in, rate_out, rate_read, _ = RATES_5X[model]
    cost = (5_000 * rate_in + 500 * rate_out + 15_000 * rate_read) / 1_000_000

    def execute() -> StructuredExecution:
        return execute_structured(
            system="s",
            prompt="p",
            output_schema={"type": "object"},
            driver="litellm",
            model=model,
            origin="machine",
        )

    allowed = _ceiling_outcome(
        tmp_path / "a", model=model, usage=ModelCallUsage(), max_cost=cost * 1.01, executor=execute
    )
    refused = _ceiling_outcome(
        tmp_path / "b", model=model, usage=ModelCallUsage(), max_cost=cost * 0.99, executor=execute
    )

    assert allowed.status == "ready", allowed.error
    assert allowed.usage.provider_cost is None and allowed.usage.input_tokens == 20_000
    assert refused.status == "failed" and "cost budget exceeded" in refused.error


def test_haiku_5_5_is_priced_at_the_higher_tier_once_the_prompt_passes_100k(tmp_path: Path) -> None:
    ceiling = 0.03  # a 100,000-token prompt is $0.0105 at the base rates; one more token reprices the request at 5x
    at_the_edge = _ceiling_outcome(
        tmp_path / "edge", model="claude-haiku-5-5", usage=_reported_tokens(100_000, 1_000), max_cost=ceiling
    )
    past_it = _ceiling_outcome(
        tmp_path / "past", model="claude-haiku-5-5", usage=_reported_tokens(100_001, 1_000), max_cost=ceiling
    )

    assert at_the_edge.status == "ready", at_the_edge.error
    assert past_it.status == "failed" and "$0.0525" in past_it.error  # (100,001 x 0.50 + 1,000 x 2.50) / 1M


def test_haiku_5_5_counts_cached_tokens_toward_the_prompt_length(tmp_path: Path) -> None:
    # 100,001 prompt tokens, 90,001 of them cache reads: $0.0024 at the base rates, $0.0120 at the higher tier.
    used = _reported_tokens(100_001, 1_000, read=90_001)

    outcome = _ceiling_outcome(tmp_path, model="claude-haiku-5-5", usage=used, max_cost=0.005)

    assert outcome.status == "failed" and "$0.012000" in outcome.error


def test_an_unlisted_model_with_no_provider_cost_is_refused_naming_the_model_and_the_way_out(
    tmp_path: Path, price_store: Path
) -> None:
    # An id the table does not list; a neighbour's rate (Sonnet 5.5, 4.6) is never used in its place.
    outcome = _ceiling_outcome(tmp_path, model="claude-sonnet-9-9", usage=_reported_tokens(10_000, 500), max_cost=100.0)

    assert outcome.status == "failed"
    assert "claude-sonnet-9-9" in outcome.error
    assert "no exact price" in outcome.error
    assert str(price_store / "pricing.yaml") in outcome.error  # where a price override goes
    assert "remove the cost ceiling" in outcome.error and "per_review_model_cost_usd" in outcome.error
    assert outcome.usage.provider_cost is None


def test_a_price_override_lets_the_ceiling_hold_a_model_the_table_does_not_list(
    tmp_path: Path, price_store: Path
) -> None:
    from lemoncrow.core.capabilities import pricing

    (price_store / "pricing.yaml").write_text("claude-sonnet-9-9:\n  input: 1.0\n  output: 2.0\n", encoding="utf-8")
    pricing._load_pricing_table.cache_clear()
    pricing._nearest_table_match.cache_clear()
    used = _reported_tokens(10_000, 500)  # (10,000 x 1 + 500 x 2) / 1M = $0.011

    under = _ceiling_outcome(tmp_path / "under", model="claude-sonnet-9-9", usage=used, max_cost=0.012)
    over = _ceiling_outcome(tmp_path / "over", model="claude-sonnet-9-9", usage=used, max_cost=0.010)

    assert under.status == "ready", under.error
    assert over.status == "failed" and "$0.011000" in over.error


def test_a_call_that_reports_no_token_counts_cannot_be_priced_and_says_so(tmp_path: Path) -> None:
    # A streamed answer and Ollama report neither a cost nor tokens: not even a listed model can be priced.
    outcome = _ceiling_outcome(
        tmp_path, model="claude-sonnet-5-5", usage=ModelCallUsage(calls=1), max_cost=100.0, driver="litellm"
    )

    assert outcome.status == "failed"
    assert "neither a cost nor token counts" in outcome.error and "claude-sonnet-5-5" in outcome.error


def test_a_retried_call_cannot_be_priced_from_its_last_attempt_alone(tmp_path: Path) -> None:
    attempts = 0

    def execute() -> StructuredExecution:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise InternalLLMError("the first attempt failed after the provider had answered")
        return StructuredExecution(
            result={"body": "ok"},
            driver="litellm",
            model="claude-sonnet-5-5",
            usage=_reported_tokens(1_000, 100),
        )

    outcome = _ceiling_outcome(
        tmp_path,
        model="claude-sonnet-5-5",
        usage=ModelCallUsage(),
        max_cost=100.0,
        max_calls=2,
        executor=execute,
    )

    assert attempts == 2
    assert outcome.status == "failed"
    assert "earlier attempt failed with no usage" in outcome.error  # its cost is unknown, so the ceiling cannot hold it
