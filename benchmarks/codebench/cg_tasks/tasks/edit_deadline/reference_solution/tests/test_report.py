"""Baseline Report / StepRecord behavior.

These tests cover the result types' non-deadline behavior: record helpers, the
succeeded/failed name lists, total elapsed, the container protocol, and the
basic summary. They must pass regardless of the deadline feature. Deadline-
specific report behavior lives in test_deadline.py.
"""

from __future__ import annotations

from jobrunner import Report, StepRecord


def test_step_record_defaults():
    record = StepRecord("a", "ok")
    assert record.name == "a"
    assert record.status == "ok"
    assert record.output is None
    assert record.elapsed == 0.0
    assert record.error is None


def test_report_succeeded_and_failed():
    report = Report()
    report.record_ok("a", output="A", elapsed=1.0)
    report.record_failed("b", error=ValueError("x"), elapsed=2.0)
    report.record_ok("c", output="C", elapsed=3.0)
    assert report.succeeded == ["a", "c"]
    assert report.failed == ["b"]


def test_report_total_elapsed():
    report = Report()
    report.record_ok("a", elapsed=1.5)
    report.record_ok("b", elapsed=2.5)
    assert report.total_elapsed == 4.0


def test_report_get_len_iter():
    report = Report()
    report.record_ok("a")
    report.record_ok("b")
    assert len(report) == 2
    assert report.get("a").name == "a"
    assert [r.name for r in report] == ["a", "b"]
    assert report.get("missing") is None


def test_report_ok_true_when_no_failures():
    report = Report()
    report.record_ok("a")
    assert report.ok is True


def test_report_summary_basic():
    report = Report()
    report.record_ok("a", elapsed=1.0)
    report.record_failed("b", error=ValueError("x"), elapsed=1.0)
    summary = report.summary()
    assert "a" in summary
    assert "b" in summary
