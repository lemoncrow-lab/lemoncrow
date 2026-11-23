"""P8-4: the monthly AI budget replaces the AI switch (SPEC §7.1 OWNER-1 to OWNER-3).

A $0 or used-up budget refuses every model entry point before anything is sent; the route that would run stays a
label and never refuses; every call is metered once in one monthly total with an honest cost; a stored
``intelligence.enabled: false`` becomes a $0 budget exactly once.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import lemoncrow.infra.internal_llm as facade
from lemoncrow.core import settings
from lemoncrow.infra.internal_llm import exceptions as llm_exceptions
from lemoncrow.infra.internal_llm import litellm_client, ollama_client, openai_client
from lemoncrow.infra.internal_llm.result import InternalLLMChatResult
from lemoncrow.pro.capabilities.review import intelligence_drivers
from lemoncrow.pro.capabilities.review.ai_budget import (
    BUDGET_SETTING,
    LEGACY_SWITCH,
    AiBudgetRefused,
    ai_budget_state,
    budget_setting,
    call_cost_micros,
    migrate_legacy_switch,
    reset_label,
)
from lemoncrow.pro.capabilities.review.background_budget import BudgetLane, month_start, next_month_start
from lemoncrow.pro.capabilities.review.background_budget_store import LocalBackgroundBudgetStore
from lemoncrow.pro.capabilities.review.background_local import local_intelligence_settings
from lemoncrow.pro.capabilities.review.intelligence_policy import (
    ask_route_options,
    process_intelligence_policy,
    resolve_intelligence_route,
)
from lemoncrow.pro.capabilities.review.intelligence_runtime import (
    IntelligenceDisabled,
    IntelligenceRunRequest,
    ModelCallBudget,
    ModelCallUsage,
    StructuredExecution,
    execute_structured,
    resolve_driver_model,
    run_intelligence,
)
from lemoncrow.pro.capabilities.review.pr_bot import engine
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id

_SCHEMA = {"type": "object", "properties": {"body": {"type": "string"}}, "required": ["body"]}
_MESSAGES = [{"role": "user", "content": "a question that must not leave"}]


@pytest.fixture(autouse=True)
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    from lemoncrow.pro.capabilities.model_setup.transport import set_active_store_root

    for name in (
        "LEMONCROW_LLM_BACKEND",
        "LEMONCROW_OPENAI_MODEL",
        "LEMONCROW_LITELLM_MODEL",
        "LEMONCROW_OLLAMA_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "lemoncrow-root"
    path.mkdir()
    set_active_store_root(path)
    try:
        yield path
    finally:
        set_active_store_root(None)


def _budget(root: Path, value: object) -> None:
    (root / "plugin_settings.json").write_text(json.dumps({BUDGET_SETTING: value}), encoding="utf-8")


def _ledger(root: Path) -> dict[str, Any]:
    """This month's two lanes added up, read straight from the ledger file."""

    use = LocalBackgroundBudgetStore(root).ai_use()
    return {"calls": use.calls, "micros": use.micros, "unreported": use.unreported_calls}


class Transport:
    """Every place a byte could leave: each provider client, the Ollama client and the agent-CLI runner."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, result: InternalLLMChatResult | None = None) -> None:
        self.sent: list[str] = []
        self.result = result or InternalLLMChatResult(content='{"body": "ok"}', parsed_json={"body": "ok"})
        for module in (openai_client, litellm_client):
            monkeypatch.setattr(module, "chat_with_result", self._with_result(module.__name__))
            monkeypatch.setattr(module, "chat", self._text(module.__name__ + ".chat"))
            monkeypatch.setattr(module, "chat_stream", self._stream(module.__name__ + ".chat_stream"))
            monkeypatch.setattr(module, "summarize", self._text(module.__name__ + ".summarize"))
        monkeypatch.setattr(openai_client, "_resolve_client", self._raise("openai._resolve_client"))
        monkeypatch.setattr(litellm_client, "_litellm_module", self._raise("litellm._litellm_module"))
        monkeypatch.setattr(ollama_client, "chat", self._text("ollama.chat"))
        monkeypatch.setattr(ollama_client, "chat_stream", self._stream("ollama.chat_stream"))
        monkeypatch.setattr(ollama_client, "summarize", self._text("ollama.summarize"))
        monkeypatch.setattr(intelligence_drivers, "_execute_cli_structured", self._cli)
        monkeypatch.setattr("lemoncrow.pro.capabilities.review.intelligence_runtime._execute_cli_structured", self._cli)

    def _with_result(self, name: str) -> Callable[..., InternalLLMChatResult]:
        def call(*args: Any, **kwargs: Any) -> InternalLLMChatResult:
            self.sent.append(name)
            return self.result

        return call

    def _text(self, name: str) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.sent.append(name)
            return {"body": "ok"} if kwargs.get("json_schema") else "ok"

        return call

    def _stream(self, name: str) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.sent.append(name)
            kwargs["on_delta"]("ok")
            return {"body": "ok"} if kwargs.get("json_schema") else "ok"

        return call

    def _raise(self, name: str) -> Callable[..., Any]:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.sent.append(name)
            raise llm_exceptions.InternalLLMError("nothing may be built")

        return call

    def _cli(self, **kwargs: Any) -> dict[str, Any]:
        self.sent.append("cli:" + str(kwargs.get("driver")))
        return {"body": "ok"}


def _structured(driver: str, **extra: Any) -> StructuredExecution:
    return execute_structured(
        system="s",
        prompt="p",
        output_schema=_SCHEMA,
        driver=driver,
        origin="machine",
        project_root=Path("."),
        **extra,
    )


#: Every function that sends something to a model, by how a caller reaches it. A new one is added here or it is
#: not a model entry point (``test_every_model_call_entry_point_consults_the_intelligence_policy`` lists the direct
#: provider callers that are not Review intelligence).
ENTRY_POINTS: dict[str, Callable[[], Any]] = {
    "facade.chat openai": lambda: facade.chat(_MESSAGES, backend="openai"),
    "facade.chat litellm": lambda: facade.chat(_MESSAGES, backend="litellm"),
    "facade.chat ollama": lambda: facade.chat(_MESSAGES, backend="ollama"),
    "facade.chat_stream openai": lambda: facade.chat_stream(_MESSAGES, on_delta=lambda _: None, backend="openai"),
    "facade.chat_stream ollama": lambda: facade.chat_stream(_MESSAGES, on_delta=lambda _: None, backend="ollama"),
    "facade.summarize": lambda: facade.summarize("text that must not leave"),
    "facade.logprobs": lambda: facade.logprobs("text that must not leave"),
    "execute_structured openai": lambda: _structured("openai"),
    "execute_structured litellm": lambda: _structured("litellm"),
    "execute_structured ollama": lambda: _structured("ollama"),
    "execute_structured streamed": lambda: _structured("openai", on_raw_delta=lambda _: None),
    "execute_structured claude cli": lambda: _structured("claude"),
    "execute_structured codex cli": lambda: _structured("codex"),
    "execute_structured opencode cli": lambda: _structured("opencode"),
    "execute_structured cli_executor seam": lambda: _structured(
        "claude", cli_executor=lambda **_: (_ for _ in ()).throw(AssertionError("sent"))
    ),
}


def test_the_entry_point_table_covers_every_function_the_facade_exports() -> None:
    called = {name.split()[0].removeprefix("facade.") for name in ENTRY_POINTS if name.startswith("facade.")}
    sends = {"chat", "chat_stream", "summarize", "logprobs"}
    exported = set(facade.__all__) - {"InternalLLMError", "LiteLLMUnavailable", "OllamaUnavailable"}
    exported -= {"chunk_entropy", "configured_model", "token_surprisals"}  # no model: structure and labels only
    assert exported == sends == called


@pytest.mark.parametrize("name", sorted(ENTRY_POINTS))
def test_a_zero_budget_refuses_every_model_entry_point_before_anything_is_sent(
    name: str, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    transport = Transport(monkeypatch)
    _budget(root, 0)
    if name == "facade.logprobs":
        # logprobs never raises: it answers None and sends nothing.
        assert ENTRY_POINTS[name]() is None
    else:
        with pytest.raises(AiBudgetRefused, match=r"AI budget is \$0") as caught:
            ENTRY_POINTS[name]()
        assert caught.value.code == "budget_zero" and isinstance(caught.value, IntelligenceDisabled)
    assert transport.sent == []
    assert _ledger(root) == {"calls": 0, "micros": 0, "unreported": 0}


@pytest.mark.parametrize("name", sorted(set(ENTRY_POINTS) - {"facade.logprobs"}))
def test_the_same_entry_points_send_once_the_budget_has_room(
    name: str, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: with no limit each reaches its transport, so the refusal above is the budget and nothing else."""

    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    transport = Transport(monkeypatch)
    if name == "execute_structured cli_executor seam":
        return  # the seam is a stand-in that raises on purpose
    ENTRY_POINTS[name]()
    assert len(transport.sent) >= 1
    assert _ledger(root)["calls"] == 1, "one call is metered once, even when it streams through the facade"


def test_run_intelligence_returns_a_ready_result_at_zero_and_refuses_a_new_run_with_the_reason(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    store = ReviewStore(tmp_path / "store")
    store.init()
    session = store.create_session(
        ReviewSession(
            id=new_session_id(), subject_type="local_change", repo_root="/x", range_mode="working_tree", title="t"
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint="tree",
            source_fingerprint="source",
            packet_schema_version=1,
        ),
        [],
    )
    ran: list[int] = []

    def request(question: str) -> IntelligenceRunRequest:
        return IntelligenceRunRequest(
            review_id=session.id,
            revision_id=revision.id,
            snapshot_identity="source",
            capability="ask",
            scope={"path": "a.py"},
            context={"question": question},
            policy={"read_only": True},
            driver=None,
            model=None,
            output_schema=_SCHEMA,
            prompt_version=1,
            schema_version=1,
            budget=ModelCallBudget(max_calls=1, max_review_calls=20),
        )

    def executor() -> StructuredExecution:
        ran.append(1)
        return StructuredExecution(
            result={"body": "ok"}, driver="openai", model="gpt-4o-mini", usage=ModelCallUsage(calls=1)
        )

    first = run_intelligence(store=store, request=request("why"), executor=executor)
    assert first.status == "ready" and ran == [1]
    _budget(root, 0)
    again = run_intelligence(store=store, request=request("why"), executor=executor)
    assert again.status == "ready" and again.cache_hit and ran == [1], "the result already made stays"
    rows = store.list_intelligence_runs(session.id)
    fresh = run_intelligence(store=store, request=request("another question"), executor=executor)
    assert fresh.status == "failed" and fresh.error == "AI budget is $0" and ran == [1]
    assert fresh.refused == "budget_zero" and fresh.run_id == ""
    assert store.list_intelligence_runs(session.id) == rows, "a refusal writes no run row"
    assert store.intelligence_calls(session.id) == 1, "a refused run claims no Review-wide call"


def test_route_resolution_for_a_label_never_refuses_at_zero_but_a_run_start_does(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    _budget(root, 0)
    assert resolve_driver_model(None, None).driver == "openai"
    assert resolve_intelligence_route().status == "ready"
    assert ask_route_options()["status"] == "ready"
    assert engine.route_problem() == ("", "")
    assert process_intelligence_policy().valid
    # ...while the question "may a run start" says no, with the reason.
    assert engine.run_problem() == ("disabled", "AI budget is $0")


# --------------------------------------------------------------------------- #
# the budget: used up, reset date, most restrictive
# --------------------------------------------------------------------------- #


def test_a_used_up_budget_refuses_until_the_first_day_of_next_utc_month(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    transport = Transport(
        monkeypatch, result=InternalLLMChatResult(content="x", parsed_json={"body": "ok"}, provider_cost=0.60)
    )
    _budget(root, 1)
    _structured("openai")
    assert ai_budget_state(root).may_run and _ledger(root) == {"calls": 1, "micros": 600_000, "unreported": 0}
    _structured("openai")  # the check is before the call: this one starts at $0.60 and ends at $1.20
    assert len(transport.sent) == 2
    state = ai_budget_state(root)
    assert not state.may_run and state.reason_code == "budget_used_up"
    resets = datetime.fromtimestamp(next_month_start(month_start(datetime.now(UTC).timestamp())), UTC)
    assert state.resets_on == resets.strftime("%Y-%m-%d")
    assert state.reason == f"AI budget used up until {reset_label(state.resets_on)}"
    with pytest.raises(AiBudgetRefused, match="AI budget used up until"):
        _structured("openai")
    assert len(transport.sent) == 2
    wire = local_intelligence_settings()["ai_budget"]
    assert wire["may_run"] is False and wire["used_usd"] == 1.2 and wire["monthly_budget_usd"] == 1.0
    assert wire["complete"] is True and wire["unreported_calls"] == 0


def test_reset_label_reads_as_a_short_date() -> None:
    assert reset_label("2026-12-01") == "Dec 1" and reset_label("2027-01-01") == "Jan 1"


def test_no_value_means_no_limit_and_negative_one_is_the_same(root: Path) -> None:
    assert ai_budget_state(root).limit_micros is None and ai_budget_state(root).may_run
    _budget(root, -1)
    assert ai_budget_state(root).limit_micros is None
    _budget(root, "")
    assert ai_budget_state(root).limit_micros is None


@pytest.mark.parametrize("stored", [-2, "abc", True, [1], {"a": 1}, 1e12, "nan", "inf"])
def test_an_unreadable_budget_fails_closed_and_says_which_setting(root: Path, stored: object) -> None:
    _budget(root, stored)
    state = ai_budget_state(root)
    assert not state.may_run and state.reason_code == "budget_invalid" and BUDGET_SETTING in state.reason


def test_a_ledger_that_cannot_be_read_fails_closed_under_a_limit_and_open_without_one(root: Path) -> None:
    (root / "background_budget.db").mkdir()
    _budget(root, 5)
    state = ai_budget_state(root)
    assert not state.may_run and state.reason_code == "budget_unreadable"
    (root / "plugin_settings.json").unlink()
    assert ai_budget_state(root).may_run  # no limit: nothing to compare, so the ledger is not needed to decide


def test_the_background_envelope_still_applies_on_top_the_most_restrictive_wins(root: Path) -> None:
    settings.write_setting(root, "intelligence.background.monthly_model_calls", 1)
    store = LocalBackgroundBudgetStore(root)
    from lemoncrow.pro.capabilities.review.background_budget import BackgroundWorkRequest, JobPriority

    def request(key: str) -> BackgroundWorkRequest:
        return BackgroundWorkRequest(
            key=key, capability="review.auto_ai", priority=JobPriority.REVIEW_AUTO, max_calls=1, locus="local"
        )

    _budget(root, 100)
    settings.write_setting(root, "intelligence.background.monthly_model_calls", 1)
    assert store.reserve(request("a")).admitted
    store.settle("a", calls=1, cost_micros=1_000)
    refused = store.reserve(request("b"))
    assert refused.reason.value == "background_exhausted", "the envelope binds although the dollar budget has room"
    # ...and a dollar budget that is spent refuses although the envelope has room.
    settings.write_setting(root, "intelligence.background.monthly_model_calls", 100)
    settings.write_setting(root, BUDGET_SETTING, 0.001)
    assert store.reserve(request("c")).reason.value == "ai_budget_exhausted"


# --------------------------------------------------------------------------- #
# metering: priced, estimated, local, not reported
# --------------------------------------------------------------------------- #


def test_a_provider_reported_cost_is_counted_exactly(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    Transport(monkeypatch, result=InternalLLMChatResult(content="x", parsed_json={"body": "ok"}, provider_cost=0.0123))
    _structured("litellm")
    assert _ledger(root) == {"calls": 1, "micros": 12_300, "unreported": 0}


def test_reported_tokens_are_priced_at_the_list_price_when_no_cost_is_reported(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.intelligence_runtime import exact_table_cost_usd

    Transport(
        monkeypatch,
        result=InternalLLMChatResult(content="x", parsed_json={"body": "ok"}, input_tokens=1_000_000, output_tokens=0),
    )
    monkeypatch.setenv("LEMONCROW_OPENAI_MODEL", "gpt-4o-mini")
    _structured("openai")
    expected = exact_table_cost_usd("gpt-4o-mini", ModelCallUsage(calls=1, input_tokens=1_000_000, output_tokens=0))
    assert expected == pytest.approx(0.15)
    assert _ledger(root) == {"calls": 1, "micros": 150_000, "unreported": 0}


def test_the_local_model_on_this_machine_is_free(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    Transport(monkeypatch)
    _structured("ollama")
    facade.chat(_MESSAGES, backend="ollama")
    assert _ledger(root) == {"calls": 2, "micros": 0, "unreported": 0}


@pytest.mark.parametrize(
    "result",
    [
        InternalLLMChatResult(content="x", parsed_json={"body": "ok"}),  # neither a cost nor tokens
        InternalLLMChatResult(content="x", parsed_json={"body": "ok"}, input_tokens=10, output_tokens=5, model="?"),
    ],
    ids=["no usage reported", "tokens but no price for the model"],
)
def test_a_call_with_neither_cost_nor_a_price_is_counted_as_not_reported_never_as_zero(
    result: InternalLLMChatResult, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Transport(monkeypatch, result=result)
    monkeypatch.setenv("LEMONCROW_OPENAI_MODEL", "model-with-no-list-price")
    _structured("openai")
    assert _ledger(root) == {"calls": 1, "micros": 0, "unreported": 1}
    wire = local_intelligence_settings()["ai_budget"]
    assert wire["unreported_calls"] == 1 and wire["complete"] is False and wire["used_usd"] == 0


def test_a_streamed_hosted_call_and_a_summary_report_no_usage_so_they_are_counted_as_not_reported(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    Transport(monkeypatch)
    facade.chat_stream(_MESSAGES, on_delta=lambda _: None)
    facade.summarize("some text to summarize")
    assert _ledger(root) == {"calls": 2, "micros": 0, "unreported": 2}


def test_not_reported_calls_are_not_refused_under_a_limit_only_the_known_cost_counts(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Transport(monkeypatch)
    _budget(root, 0.01)
    for _ in range(3):
        _structured("openai")
    assert ai_budget_state(root).may_run and _ledger(root)["unreported"] == 3


def test_a_failed_call_is_not_metered_and_a_refused_one_leaves_no_trace(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Transport(monkeypatch)
    monkeypatch.setattr(openai_client, "chat_with_result", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(RuntimeError):
        _structured("openai")
    assert _ledger(root)["calls"] == 0


def test_call_cost_never_turns_an_unknown_into_zero() -> None:
    assert call_cost_micros("ollama", "qwen", None) == 0
    assert call_cost_micros("openai", "gpt-4o-mini", None) is None
    assert call_cost_micros("openai", "gpt-4o-mini", ModelCallUsage(calls=1)) is None
    assert call_cost_micros("openai", "gpt-4o-mini", ModelCallUsage(calls=1, input_tokens=0, output_tokens=0)) is None
    assert call_cost_micros("claude", "", ModelCallUsage(calls=1, provider_cost=0.5, currency="USD")) == 500_000
    assert call_cost_micros("claude", "", ModelCallUsage(calls=1, provider_cost=0.5, currency="EUR")) is None
    assert call_cost_micros("claude", "", ModelCallUsage(calls=1, provider_cost=0.0, currency="USD")) == 0


def test_a_call_inside_an_admission_is_settled_with_it_and_metered_once(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.pro.capabilities.review.background_local import run_local_review_auto
    from lemoncrow.pro.capabilities.review.background_scheduler import BackgroundOutcome

    Transport(monkeypatch)
    store = LocalBackgroundBudgetStore(root)

    def execute(granted: int) -> BackgroundOutcome:
        _structured("ollama")  # a local model: free, and counted
        return BackgroundOutcome(1, None, "done")

    run = run_local_review_auto(budget=store, revision_id="rev-1", max_calls=1, current=lambda: True, execute=execute)
    assert run.status == "completed"
    assert _ledger(root) == {"calls": 1, "micros": 0, "unreported": 0}, "one call, free, and counted in one lane only"
    with sqlite3.connect(root / "background_budget.db") as conn:
        lanes = dict(
            conn.execute(
                "SELECT lane, SUM(calls_used) FROM background_budget_periods WHERE period_kind = 'month' GROUP BY lane"
            )
        )
    assert lanes == {BudgetLane.BACKGROUND.value: 1}


def test_the_meter_writes_the_interactive_lane_and_the_rollover_deletes_what_is_past_retention(root: Path) -> None:
    clock = [datetime(2026, 9, 15, 12, tzinfo=UTC).timestamp()]
    store = LocalBackgroundBudgetStore(root, clock=lambda: clock[0], retention_s=86_400.0)
    store.meter(calls=2, cost_micros=5)
    with sqlite3.connect(root / "background_budget.db") as conn:
        rows = conn.execute(
            "SELECT lane, period_kind, calls_used, reported_micros FROM background_budget_periods"
        ).fetchall()
    assert sorted(rows) == [("interactive", "day", 2, 5), ("interactive", "month", 2, 5)]
    clock[0] = datetime(2026, 11, 2, tzinfo=UTC).timestamp()
    store.meter(calls=1, cost_micros=None)
    assert store.ai_use().calls == 1 and store.ai_use().unreported_calls == 1, "a new month starts from nothing"
    with sqlite3.connect(root / "background_budget.db") as conn:
        starts = {row[0] for row in conn.execute("SELECT period_start FROM background_budget_periods")}
    assert datetime(2026, 9, 1, tzinfo=UTC).timestamp() not in starts, "the closer removed the old month"


# --------------------------------------------------------------------------- #
# the retired switch
# --------------------------------------------------------------------------- #


def _raw(root: Path) -> dict[str, Any]:
    return json.loads((root / "plugin_settings.json").read_text(encoding="utf-8"))


def test_a_stored_false_becomes_a_zero_budget_once(root: Path) -> None:
    (root / "plugin_settings.json").write_text(json.dumps({LEGACY_SWITCH: False, "cli.update_check": True}))
    assert migrate_legacy_switch(root) is True
    assert _raw(root) == {BUDGET_SETTING: 0.0, "cli.update_check": True}
    before = (root / "plugin_settings.json").read_bytes()
    assert migrate_legacy_switch(root) is False, "idempotent: nothing left to migrate"
    assert (root / "plugin_settings.json").read_bytes() == before
    assert not ai_budget_state(root).may_run and ai_budget_state(root).reason == "AI budget is $0"


def test_a_stored_true_is_simply_dropped_and_leaves_no_limit(root: Path) -> None:
    (root / "plugin_settings.json").write_text(json.dumps({LEGACY_SWITCH: True}))
    assert migrate_legacy_switch(root) is True
    assert _raw(root) == {}
    assert ai_budget_state(root).limit_micros is None


@pytest.mark.parametrize("stored", ["false", "off", "0", "no", None, "", "maybe", 0])
def test_anything_not_clearly_on_migrates_to_zero_never_to_on(root: Path, stored: object) -> None:
    (root / "plugin_settings.json").write_text(json.dumps({LEGACY_SWITCH: stored}))
    assert budget_setting(_raw(root)).limit_micros == 0, "read the same way before it is migrated"
    migrate_legacy_switch(root)
    assert _raw(root) == {BUDGET_SETTING: 0.0}


def test_a_stricter_old_switch_wins_over_a_budget_that_was_already_set(root: Path) -> None:
    (root / "plugin_settings.json").write_text(json.dumps({LEGACY_SWITCH: False, BUDGET_SETTING: 25}))
    migrate_legacy_switch(root)
    assert _raw(root) == {BUDGET_SETTING: 0.0}


def test_the_first_policy_read_migrates_and_a_missing_or_unwritable_file_is_not_an_error(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert migrate_legacy_switch(root) is False  # no file at all
    (root / "plugin_settings.json").write_text(json.dumps({LEGACY_SWITCH: False}))
    assert process_intelligence_policy().valid
    assert _raw(root) == {BUDGET_SETTING: 0.0}
    # A file that cannot be rewritten still reads as $0 (fail closed), and the attempt does not raise.
    (root / "plugin_settings.json").write_text(json.dumps({LEGACY_SWITCH: False}))
    monkeypatch.setattr(settings, "_write_json", lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")))
    assert migrate_legacy_switch(root) is False
    assert not ai_budget_state(root).may_run


def test_the_registry_has_the_budget_and_no_switch() -> None:
    assert settings.find_by_key("intelligence.enabled") is None
    spec = settings.find_by_key(BUDGET_SETTING)
    assert spec is not None and spec.type == "float" and spec.default == -1.0 and spec.settable


@pytest.mark.parametrize("value", [-0.5, -2, float("nan"), float("inf"), 2_000_000_000])
def test_lc_settings_refuses_a_budget_that_is_not_zero_or_more_or_minus_one(root: Path, value: float) -> None:
    with pytest.raises(ValueError):
        settings.write_setting(root, BUDGET_SETTING, value)


def test_lc_settings_takes_zero_a_positive_amount_and_minus_one(root: Path) -> None:
    for value, expected in (("0", 0.0), ("7.5", 7.5), ("-1", -1.0)):
        assert settings.write_setting(root, BUDGET_SETTING, value) == expected
    with pytest.raises(ValueError, match="unknown setting"):
        settings.write_setting(root, LEGACY_SWITCH, "true")


# --------------------------------------------------------------------------- #
# a positive budget stops hosted-provider calls that go through the facade, a stream or a summary
# --------------------------------------------------------------------------- #


def _ns(**kwargs: Any) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(**kwargs)


def _openai_world(monkeypatch: pytest.MonkeyPatch, *, input_tokens: int = 1_000_000) -> dict[str, Any]:
    """A fake OpenAI SDK client: it answers, and reports its use as the real one does (a stream: on the last chunk)."""

    seen: dict[str, Any] = {"calls": [], "stream_options": []}
    usage = _ns(prompt_tokens=input_tokens, completion_tokens=0, prompt_tokens_details=None)

    def create(**kwargs: Any) -> Any:
        seen["calls"].append("stream" if kwargs.get("stream") else "plain")
        seen["stream_options"].append(kwargs.get("stream_options"))
        message = _ns(content='{"body": "ok"}' if kwargs.get("response_format") else "ok")
        if kwargs.get("stream"):
            delta = _ns(choices=[_ns(delta=_ns(content=message.content))], usage=None)
            last = _ns(choices=[], usage=usage if kwargs.get("stream_options") else None)
            return iter([delta, last])
        return _ns(choices=[_ns(message=message)], usage=usage, model="gpt-4o-mini", id="r1")

    client = _ns(chat=_ns(completions=_ns(create=create)))
    monkeypatch.setattr(openai_client, "_resolve_client", lambda: client)
    monkeypatch.setattr(openai_client.importlib.util, "find_spec", lambda name, *a, **k: object())
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    monkeypatch.setenv("LEMONCROW_OPENAI_MODEL", "gpt-4o-mini")
    return seen


def test_a_positive_budget_is_used_up_by_facade_chat_stream_and_summarize_calls_then_refuses(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _openai_world(monkeypatch)  # each call reports 1,000,000 input tokens: $0.15 at gpt-4o-mini's list price
    _budget(root, 0.40)
    facade.chat(_MESSAGES)
    assert _ledger(root) == {"calls": 1, "micros": 150_000, "unreported": 0}
    facade.chat_stream(_MESSAGES, on_delta=lambda _: None)
    assert seen["stream_options"][-1] == {"include_usage": True}, "a stream asks for its use"
    assert _ledger(root) == {"calls": 2, "micros": 300_000, "unreported": 0}
    facade.summarize("text to summarize once")
    assert _ledger(root) == {"calls": 3, "micros": 450_000, "unreported": 0}
    assert not ai_budget_state(root).may_run
    sent = len(seen["calls"])
    for refused in (
        lambda: facade.chat(_MESSAGES),
        lambda: facade.chat_stream(_MESSAGES, on_delta=lambda _: None),
        lambda: facade.summarize("different text so the cache cannot answer"),
    ):
        with pytest.raises(AiBudgetRefused, match="AI budget used up until"):
            refused()
    assert len(seen["calls"]) == sent, "nothing more left the machine"


def test_a_streamed_ask_through_the_structured_executor_is_priced_from_the_stream(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _openai_world(monkeypatch)
    _budget(root, 0.20)
    _structured("openai", on_raw_delta=lambda _: None, provider_stream=facade.chat_stream)  # how Ask streams
    assert _ledger(root) == {"calls": 1, "micros": 150_000, "unreported": 0}
    _structured("openai", on_raw_delta=lambda _: None, provider_stream=facade.chat_stream)
    with pytest.raises(AiBudgetRefused):
        _structured("openai", on_raw_delta=lambda _: None, provider_stream=facade.chat_stream)


def test_a_server_that_rejects_the_usage_option_is_asked_again_without_it(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _openai_world(monkeypatch)
    real = openai_client._resolve_client().chat.completions.create

    def picky(**kwargs: Any) -> Any:
        if "stream_options" in kwargs:
            raise RuntimeError("unknown parameter: stream_options")
        return real(**kwargs)

    monkeypatch.setattr(openai_client, "_resolve_client", lambda: _ns(chat=_ns(completions=_ns(create=picky))))
    assert facade.chat_stream(_MESSAGES, on_delta=lambda _: None) == "ok"
    assert seen["calls"] == ["stream"]
    assert _ledger(root) == {"calls": 1, "micros": 0, "unreported": 1}, "answered, but it told us nothing: not reported"


def test_a_litellm_stream_summary_and_chat_report_their_use(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    usage = {"prompt_tokens": 1_000_000, "completion_tokens": 0}
    asked: list[dict[str, Any]] = []

    def completion(**kwargs: Any) -> Any:
        asked.append(kwargs)
        if kwargs.get("stream"):
            return iter([_ns(choices=[_ns(delta=_ns(content="ok"))], usage=None), _ns(choices=[], usage=usage)])
        return _ns(choices=[_ns(message=_ns(content="ok"))], usage=usage, model="m", id="r", _hidden_params={})

    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: _ns(completion=completion))
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "litellm")
    monkeypatch.setenv("LEMONCROW_LITELLM_MODEL", "gpt-4o-mini")
    facade.chat(_MESSAGES)
    facade.chat_stream(_MESSAGES, on_delta=lambda _: None)
    facade.summarize("summarize this once")
    assert any(call.get("stream_options") == {"include_usage": True} for call in asked)
    assert _ledger(root) == {"calls": 3, "micros": 450_000, "unreported": 0}


def test_a_billed_answer_that_then_fails_is_metered_and_one_that_never_came_is_not(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow.infra.internal_llm.usage import note_response

    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")

    def answered_then_malformed(*args: Any, **kwargs: Any) -> Any:
        note_response("gpt-4o-mini", {"prompt_tokens": 1_000_000, "completion_tokens": 0})
        return InternalLLMChatResult(content="x", parsed_json={"unrelated": 1}, input_tokens=1_000_000)

    monkeypatch.setattr(openai_client, "chat_with_result", answered_then_malformed)
    with pytest.raises(llm_exceptions.InternalLLMError, match="does not follow the output schema"):
        _structured("openai")
    assert _ledger(root) == {"calls": 1, "micros": 150_000, "unreported": 0}, "billed, then rejected: still counted"
    monkeypatch.setattr(
        openai_client, "chat_with_result", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("refused"))
    )
    with pytest.raises(RuntimeError):
        _structured("openai")
    assert _ledger(root)["calls"] == 1, "no answer, no bill"


def test_a_remote_ollama_host_is_not_free(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    Transport(monkeypatch)
    monkeypatch.setenv("OLLAMA_HOST", "http://gpu.example.com:11434")
    _structured("ollama")
    assert _ledger(root) == {"calls": 1, "micros": 0, "unreported": 1}
    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:11434")
    _structured("ollama")
    assert _ledger(root) == {"calls": 2, "micros": 0, "unreported": 1}
    monkeypatch.delenv("OLLAMA_HOST")
    assert call_cost_micros("ollama", "qwen:cloud", None) is None and call_cost_micros("ollama", "qwen3", None) == 0


def _cut_stream(delivered: str) -> Any:
    """A stream that delivers one chunk and then resets, as a proxy timeout or a dropped connection does."""

    def chunks() -> Any:
        yield _ns(choices=[_ns(delta=_ns(content=delivered))], usage=None)
        raise ConnectionResetError("connection reset by peer")

    return chunks()


def test_an_openai_stream_cut_midway_is_metered_as_not_reported(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    client = _ns(chat=_ns(completions=_ns(create=lambda **kwargs: _cut_stream("par"))))
    monkeypatch.setattr(openai_client, "_resolve_client", lambda: client)
    got: list[str] = []
    with pytest.raises(llm_exceptions.InternalLLMError):
        facade.chat_stream(_MESSAGES, on_delta=got.append)
    assert got == ["par"]
    assert _ledger(root) == {"calls": 1, "micros": 0, "unreported": 1}


def test_a_cancel_raised_by_the_delta_callback_is_metered_too(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    client = _ns(chat=_ns(completions=_ns(create=lambda **kwargs: _cut_stream("par"))))
    monkeypatch.setattr(openai_client, "_resolve_client", lambda: client)

    def cancel(_delta: str) -> None:
        raise RuntimeError("cancelled")

    with pytest.raises(llm_exceptions.InternalLLMError):
        facade.chat_stream(_MESSAGES, on_delta=cancel)
    assert _ledger(root)["unreported"] == 1


def test_a_litellm_stream_cut_midway_is_metered_as_not_reported(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "litellm")
    monkeypatch.setattr(litellm_client, "_litellm_module", lambda: _ns(completion=lambda **kwargs: _cut_stream("par")))
    with pytest.raises(llm_exceptions.InternalLLMError):
        facade.chat_stream(_MESSAGES, on_delta=lambda _: None)
    assert _ledger(root) == {"calls": 1, "micros": 0, "unreported": 1}


@pytest.mark.parametrize("failure", ["generic 400", "unprocessable 422", "named class"])
def test_a_proxy_that_answers_a_generic_error_to_the_usage_option_is_asked_again_without_it(
    failure: str, root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    asked: list[bool] = []

    class BadRequestError(Exception):
        pass

    def create(**kwargs: Any) -> Any:
        asked.append("stream_options" in kwargs)
        if "stream_options" in kwargs:
            if failure == "named class":
                raise BadRequestError("Error code: 400")
            error = RuntimeError("Error code: 400 - bad request" if failure == "generic 400" else "422 Unprocessable")
            error.status_code = 400 if failure == "generic 400" else 422  # type: ignore[attr-defined]
            raise error
        return iter([_ns(choices=[_ns(delta=_ns(content="ok"))], usage=None)])

    monkeypatch.setattr(openai_client, "_resolve_client", lambda: _ns(chat=_ns(completions=_ns(create=create))))
    assert facade.chat_stream(_MESSAGES, on_delta=lambda _: None) == "ok"
    assert asked == [True, False], "one retry, without the option"
    assert _ledger(root) == {"calls": 1, "micros": 0, "unreported": 1}, "that call reports no use"


def test_a_failure_that_is_not_about_the_request_shape_is_not_retried(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", "openai")
    asked: list[int] = []

    def create(**kwargs: Any) -> Any:
        asked.append(1)
        error = RuntimeError("Error code: 401")
        error.status_code = 401  # type: ignore[attr-defined]
        raise error

    monkeypatch.setattr(openai_client, "_resolve_client", lambda: _ns(chat=_ns(completions=_ns(create=create))))
    with pytest.raises(llm_exceptions.InternalLLMError):
        facade.chat_stream(_MESSAGES, on_delta=lambda _: None)
    assert asked == [1] and _ledger(root)["calls"] == 0, "never reached: nothing billed, nothing metered"
