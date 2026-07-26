"""Baseline pipeline behavior.

These tests exercise the parts of the library that do NOT depend on the deadline
feature: ordering, output storage, retries, failure handling, and validation.
They must pass regardless of whether the deadline feature is implemented.
"""

from __future__ import annotations

import pytest
from jobrunner import Context, Pipeline, Step, StepFailed, run_pipeline


def test_step_ordering(fake_clock, make_step):
    steps = [make_step(name, 1, fake_clock) for name in ["a", "b", "c"]]
    ctx = Context(clock=fake_clock)
    report = Pipeline(steps).run(ctx)
    assert report.succeeded == ["a", "b", "c"]
    assert list(ctx.outputs) == ["a", "b", "c"]


def test_outputs_stored_in_context(fake_clock, make_step):
    steps = [make_step("a", 1, fake_clock), make_step("b", 1, fake_clock)]
    ctx = Context(clock=fake_clock)
    Pipeline(steps).run(ctx)
    assert ctx.outputs["a"] == "a"
    assert ctx.outputs["b"] == "b"


def test_step_returns_output(fake_clock):
    def func(ctx):
        return 42

    step = Step("answer", func)
    assert step.execute(Context(clock=fake_clock)) == 42


def test_context_get_set():
    ctx = Context(inputs={"x": 1})
    assert ctx.get("x") == 1
    assert ctx.get("missing", "default") == "default"
    ctx.set("y", 2)
    assert ctx.get("y") == 2


def test_retries_succeed_after_failures(fake_clock):
    calls = {"n": 0}

    def flaky(ctx):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise ValueError("boom")
        return "done"

    step = Step("flaky", flaky, retries=2)
    result = step.execute(Context(clock=fake_clock))
    assert result == "done"
    assert calls["n"] == 3


def test_step_failed_after_retries_exhausted():
    def always_fail(ctx):
        raise ValueError("nope")

    step = Step("bad", always_fail, retries=1)
    with pytest.raises(StepFailed) as excinfo:
        step.execute(Context())
    assert excinfo.value.step_name == "bad"
    assert isinstance(excinfo.value.cause, ValueError)


def test_pipeline_stops_on_step_failure(fake_clock, make_step):
    good = make_step("a", 1, fake_clock)

    def boom(ctx):
        raise RuntimeError("kaboom")

    bad = Step("b", boom, retries=0)
    never = make_step("c", 1, fake_clock)
    ctx = Context(clock=fake_clock)
    report = Pipeline([good, bad, never]).run(ctx)
    assert report.succeeded == ["a"]
    assert report.failed == ["b"]
    assert "c" not in ctx.outputs


def test_duplicate_step_names_rejected(fake_clock, make_step):
    with pytest.raises(ValueError):
        Pipeline([make_step("a", 1, fake_clock), make_step("a", 1, fake_clock)])


def test_empty_pipeline_rejected():
    with pytest.raises(ValueError):
        Pipeline([])


def test_run_pipeline_basic(fake_clock, make_step):
    steps = [make_step("a", 1, fake_clock), make_step("b", 1, fake_clock)]
    report = run_pipeline(steps, inputs={"x": 1})
    assert report.succeeded == ["a", "b"]
    assert report.ok is True


def test_run_pipeline_reports_failure():
    def boom(ctx):
        raise RuntimeError("x")

    report = run_pipeline([Step("a", boom)])
    assert report.failed == ["a"]
    assert report.ok is False
