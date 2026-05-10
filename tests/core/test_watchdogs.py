from __future__ import annotations

from atelier.core.foundation.models import ReasonBlock
from atelier.core.foundation.watchdogs import (
    ContextBloat,
    HighRiskAction,
    KnownDeadEnd,
    RepeatedCommandFailure,
    RepeatedToolCall,
    SessionState,
    SkippedVerification,
    args_signature,
    error_signature,
    run_watchdogs,
)


def _block(dead: tuple[str, ...] | list[str] = ()) -> ReasonBlock:
    return ReasonBlock(
        id="b",
        title="T",
        domain="coding",
        situation="s",
        procedure=["x"],
        dead_ends=list(dead),
    )


def test_error_signature_strips_volatile_parts() -> None:
    a = error_signature("Failed at 0xABCD123, retry 17")
    b = error_signature("Failed at 0x9999111, retry 42")
    assert a == b


def test_args_signature_stable_for_same_args() -> None:
    assert args_signature({"a": 1, "b": 2}) == args_signature({"b": 2, "a": 1})


def test_repeated_command_failure_fires_at_two() -> None:
    state = SessionState(command_results=[("ls", False, "sigA"), ("ls", False, "sigA")])
    alert = RepeatedCommandFailure().check(state, [])
    assert alert is not None and alert.severity == "high"


def test_repeated_tool_call_fires_at_three() -> None:
    state = SessionState(tool_calls=[("t", "s")] * 3)
    alert = RepeatedToolCall().check(state, [])
    assert alert is not None


def test_known_dead_end_uses_blocks() -> None:
    state = SessionState(plan=["Parse the product handle from url"])
    blocks = [_block(dead=["product handle from url"])]
    alert = KnownDeadEnd().check(state, blocks)
    assert alert is not None and alert.severity == "high"


def test_skipped_verification() -> None:
    state = SessionState(declared_success=True, validation_passed=False)
    assert SkippedVerification().check(state, []) is not None


def test_context_bloat_threshold() -> None:
    state = SessionState(tool_outputs_chars=60_000)
    assert ContextBloat().check(state, []) is not None


def test_high_risk_action_without_rubric() -> None:
    state = SessionState(tool_calls=[("shopify.publish", "sig")], rubric_run=False)
    assert HighRiskAction().check(state, []) is not None
    state.rubric_run = True
    assert HighRiskAction().check(state, []) is None


def test_run_watchdogs_returns_alerts_list() -> None:
    state = SessionState(
        command_results=[("ls", False, "sig"), ("ls", False, "sig")],
        tool_calls=[("shopify.publish", "x")],
    )
    alerts = run_watchdogs(state, [])
    names = {a.monitor for a in alerts}
    assert "repeated_command_failure" in names
    assert "high_risk_action" in names
