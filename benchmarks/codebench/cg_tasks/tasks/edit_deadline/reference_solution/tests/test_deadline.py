"""The deadline feature.

These tests define the behavior of the cross-cutting deadline feature end to
end: timeout/deadline resolution, the expiry check before each step (inclusive
boundary), skipping the tripped step and everything after it, strict mode, and
the report/summary/context surface. They fail in the half-done version and pass
once the feature is implemented.

All timing is deterministic via the ``fake_clock`` fixture; nothing sleeps.
"""

from __future__ import annotations

import pytest
from jobrunner import Context, DeadlineExceeded, Pipeline, run_pipeline


def test_timeout_runs_all_when_ample(fake_clock, make_step):
    steps = [make_step(name, 1, fake_clock) for name in ["a", "b", "c"]]
    report = run_pipeline(steps, clock=fake_clock, timeout=100)
    assert report.succeeded == ["a", "b", "c"]
    assert report.deadline_exceeded is False
    assert report.tripped_step is None


def test_timeout_stops_midway(fake_clock, make_step):
    steps = [make_step(name, 5, fake_clock) for name in ["a", "b", "c", "d"]]
    report = run_pipeline(steps, clock=fake_clock, timeout=12)
    # started=0, deadline=12: a(0->5), b(5->10), c(10->15); before d clock=15>=12.
    assert report.succeeded == ["a", "b", "c"]
    assert report.skipped == ["d"]
    assert report.deadline_exceeded is True
    assert report.tripped_step == "d"


def test_absolute_deadline(fake_clock, make_step):
    steps = [make_step(name, 5, fake_clock) for name in ["a", "b", "c"]]
    report = run_pipeline(steps, clock=fake_clock, deadline=6)
    # started=0, deadline=6: a(0->5), b(5->10); before c clock=10>=6.
    assert report.succeeded == ["a", "b"]
    assert report.skipped == ["c"]
    assert report.tripped_step == "c"


def test_deadline_takes_precedence_over_timeout(fake_clock, make_step):
    steps = [make_step(name, 5, fake_clock) for name in ["a", "b"]]
    # deadline is ample; timeout would trip immediately. deadline must win.
    report = run_pipeline(steps, clock=fake_clock, deadline=100, timeout=0)
    assert report.succeeded == ["a", "b"]
    assert report.deadline_exceeded is False


def test_expired_before_first_step_all_skipped(fake_clock, make_step):
    steps = [make_step(name, 5, fake_clock) for name in ["a", "b", "c"]]
    report = run_pipeline(steps, clock=fake_clock, deadline=fake_clock())
    assert report.succeeded == []
    assert report.skipped == ["a", "b", "c"]
    assert report.tripped_step == "a"
    assert report.deadline_exceeded is True


def test_boundary_clock_equals_deadline_expired(fake_clock, make_step):
    steps = [make_step("a", 5, fake_clock), make_step("b", 5, fake_clock)]
    ctx = Context(clock=fake_clock, started_at=0.0, deadline=5.0)
    report = Pipeline(steps).run(ctx)
    # a(0->5); before b clock=5, deadline=5 -> expired (inclusive boundary).
    assert report.succeeded == ["a"]
    assert report.skipped == ["b"]
    assert report.deadline_exceeded is True
    assert report.tripped_step == "b"


def test_no_deadline_runs_all_steps(fake_clock, make_step):
    steps = [make_step(name, 1, fake_clock) for name in ["a", "b", "c"]]
    report = run_pipeline(steps, clock=fake_clock)
    assert report.succeeded == ["a", "b", "c"]
    assert report.deadline_exceeded is False


def test_strict_true_raises_deadline_exceeded(fake_clock, make_step):
    steps = [make_step(name, 5, fake_clock) for name in ["a", "b"]]
    with pytest.raises(DeadlineExceeded) as excinfo:
        run_pipeline(steps, clock=fake_clock, timeout=0, strict=True)
    assert excinfo.value.step_name == "a"


def test_strict_false_returns_report(fake_clock, make_step):
    steps = [make_step(name, 5, fake_clock) for name in ["a", "b"]]
    report = run_pipeline(steps, clock=fake_clock, timeout=0, strict=False)
    assert report.deadline_exceeded is True
    assert report.ok is False


def test_summary_mentions_deadline_when_tripped(fake_clock, make_step):
    steps = [make_step(name, 5, fake_clock) for name in ["a", "b"]]
    report = run_pipeline(steps, clock=fake_clock, timeout=0)
    summary = report.summary()
    assert "deadline" in summary.lower()
    assert report.tripped_step in summary


def test_skipped_records_in_order(fake_clock, make_step):
    steps = [make_step(name, 5, fake_clock) for name in ["a", "b", "c", "d"]]
    report = run_pipeline(steps, clock=fake_clock, timeout=6)
    # a(0->5), b(5->10); before c clock=10>=6 -> skip c and d, in order.
    assert report.succeeded == ["a", "b"]
    assert report.skipped == ["c", "d"]


def test_context_remaining_and_expired(fake_clock):
    ctx = Context(clock=fake_clock, started_at=fake_clock(), deadline=10.0)
    assert ctx.remaining() == 10.0
    assert ctx.expired() is False
    fake_clock.advance(10)
    assert ctx.remaining() == 0.0
    assert ctx.expired() is True


def test_context_no_deadline_never_expires(fake_clock):
    ctx = Context(clock=fake_clock, deadline=None)
    assert ctx.remaining() is None
    assert ctx.expired() is False


def test_skipped_step_outputs_absent(fake_clock, make_step):
    steps = [make_step(name, 5, fake_clock) for name in ["a", "b", "c"]]
    ctx = Context(clock=fake_clock, started_at=0.0, deadline=7.0)
    Pipeline(steps).run(ctx)
    # a(0->5), b(5->10); before c clock=10>=7 -> c skipped, never executed.
    assert "a" in ctx.outputs
    assert "b" in ctx.outputs
    assert "c" not in ctx.outputs
