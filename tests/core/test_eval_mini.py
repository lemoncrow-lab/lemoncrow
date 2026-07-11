"""Tests for the LemonCrow mini eval suite (schema, loader, runner, aggregation).

Verifies:
- MiniEvalCase loads and validates from YAML
- Schema validation rejects missing required fields and unknown fields
- dry-run returns all-skipped results and a dry_run report
- accepted_patch_rate and cost_per_accepted_patch are computed correctly
- Failed attempts still count against total_cost_usd (not filtered out)
- trace_coverage_pct is computed over non-skipped cases
- routing_regression_rate is computed correctly (and is 0 for dry-run)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from benchmarks.mini.loader import default_cases_path, load_cases  # noqa: E402
from benchmarks.mini.runner import (  # noqa: E402
    aggregate_report,
    run_case_dry,
    run_suite,
)
from benchmarks.mini.schema import (  # noqa: E402
    MiniEvalCase,
    MiniEvalCaseResult,
    MiniEvalReport,
)

_VALID_YAML = """
cases:
  - id: case-a
    title: "Case A"
    prompt: "Do a thing."
    starting_git_sha: HEAD
    allowed_files:
      - "src/foo.py"
    command_to_verify: "true"
    expected_success_condition: "exits 0"
    max_cost_usd: 0.02
    tags: [cheap]
  - id: case-b
    title: "Case B"
    prompt: "Do another thing."
    starting_git_sha: HEAD
    allowed_files: []
    command_to_verify: "true"
    expected_success_condition: "exits 0"
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _result(
    case_id: str,
    *,
    status: str,
    accepted: bool = False,
    cost: float = 0.0,
    trace_id: str | None = None,
    regression: bool = False,
) -> MiniEvalCaseResult:
    return MiniEvalCaseResult(
        id=case_id,
        title=case_id,
        status=status,  # type: ignore[arg-type]
        accepted=accepted,
        estimated_cost_usd=cost,
        trace_id=trace_id,
        regression=regression,
    )


# ---------------------------------------------------------------------------
# Schema + loader
# ---------------------------------------------------------------------------


def test_load_cases_from_valid_yaml(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(_VALID_YAML, encoding="utf-8")
    cases = load_cases(path)
    assert len(cases) == 2
    assert cases[0].id == "case-a"
    assert cases[0].allowed_files == ["src/foo.py"]
    assert cases[0].max_cost_usd == 0.02
    # default applied when omitted
    assert cases[1].max_cost_usd == 0.05
    assert cases[1].tags == []


def test_load_cases_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_cases(tmp_path / "nope.yaml")


def test_schema_rejects_missing_required_field() -> None:
    with pytest.raises(ValidationError):
        MiniEvalCase.model_validate(
            {
                "id": "x",
                "title": "X",
                # prompt missing
                "starting_git_sha": "HEAD",
                "command_to_verify": "true",
                "expected_success_condition": "exits 0",
            }
        )


def test_schema_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        MiniEvalCase.model_validate(
            {
                "id": "x",
                "title": "X",
                "prompt": "p",
                "starting_git_sha": "HEAD",
                "command_to_verify": "true",
                "expected_success_condition": "exits 0",
                "surprise": "boom",
            }
        )


def test_repo_cases_yaml_loads() -> None:
    """The shipped benchmarks/mini/cases.yaml loads and validates."""
    cases = load_cases(default_cases_path())
    assert len(cases) >= 5
    assert {c.id for c in cases} >= {
        "mini-001-eval-mini-schema-doc",
        "mini-005-dry-run-no-network",
    }


# ---------------------------------------------------------------------------
# Dry-run behavior
# ---------------------------------------------------------------------------


def test_run_case_dry_returns_skipped() -> None:
    case = MiniEvalCase(
        id="case-a",
        title="Case A",
        prompt="p",
        starting_git_sha="HEAD",
        command_to_verify="true",
        expected_success_condition="exits 0",
    )
    result = run_case_dry(case)
    assert result.status == "skipped"
    assert result.accepted is False
    assert result.estimated_cost_usd == 0.0
    assert result.file_boundary_respected is True


def test_run_suite_dry_run_all_skipped(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(_VALID_YAML, encoding="utf-8")
    cases = load_cases(path)
    report = run_suite(cases, root=tmp_path, git_repo=tmp_path, dry_run=True, limit=5)
    assert report.status == "dry_run"
    assert report.total_tasks == 2
    assert all(c.status == "skipped" for c in report.cases)
    assert report.total_cost_usd == 0.0
    assert report.routing_regression_rate == 0.0
    assert report.trace_coverage_pct == 0.0


def test_run_suite_limit_caps_cases(tmp_path: Path) -> None:
    path = tmp_path / "cases.yaml"
    path.write_text(_VALID_YAML, encoding="utf-8")
    cases = load_cases(path)
    report = run_suite(cases, root=tmp_path, git_repo=tmp_path, dry_run=True, limit=1)
    assert report.total_tasks == 1


# ---------------------------------------------------------------------------
# Metric aggregation
# ---------------------------------------------------------------------------


def test_accepted_patch_rate_and_cost_per_accepted_patch() -> None:
    results = [
        _result("a", status="accepted", accepted=True, cost=0.02, trace_id="t-a"),
        _result("b", status="accepted", accepted=True, cost=0.04, trace_id="t-b"),
        _result("c", status="failed", accepted=False, cost=0.01, trace_id="t-c"),
        _result("d", status="failed", accepted=False, cost=0.03, trace_id="t-d"),
    ]
    report = aggregate_report(results, dry_run=False, started_at="s", finished_at="f")
    assert report.total_tasks == 4
    assert report.accepted_tasks == 2
    assert report.failed_tasks == 2
    # 2 / 4
    assert report.accepted_patch_rate == 0.5
    # total cost = 0.02 + 0.04 + 0.01 + 0.03 = 0.10 over 2 accepted = 0.05
    assert report.total_cost_usd == pytest.approx(0.10)
    assert report.cost_per_accepted_patch == pytest.approx(0.05)
    assert report.cheap_success_rate == 0.5


def test_failed_attempts_count_toward_total_cost() -> None:
    """Failed cheap attempts must NOT be filtered out of total cost."""
    results = [
        _result("a", status="accepted", accepted=True, cost=0.01, trace_id="t-a"),
        _result("b", status="failed", accepted=False, cost=0.09, trace_id="t-b"),
    ]
    report = aggregate_report(results, dry_run=False, started_at="s", finished_at="f")
    # The expensive failure is still counted.
    assert report.total_cost_usd == pytest.approx(0.10)
    # cost_per_accepted_patch reflects the full cost over the single accepted patch.
    assert report.cost_per_accepted_patch == pytest.approx(0.10)


def test_cost_per_accepted_patch_when_zero_accepted() -> None:
    results = [
        _result("a", status="failed", accepted=False, cost=0.02, trace_id="t-a"),
    ]
    report = aggregate_report(results, dry_run=False, started_at="s", finished_at="f")
    assert report.accepted_tasks == 0
    # Falls back to total cost (cannot divide by zero accepted patches).
    assert report.cost_per_accepted_patch == pytest.approx(0.02)


def test_trace_coverage_pct_over_non_skipped() -> None:
    results = [
        _result("a", status="accepted", accepted=True, cost=0.01, trace_id="t-a"),
        _result("b", status="failed", accepted=False, cost=0.01, trace_id=None),
        _result("c", status="skipped", accepted=False, cost=0.0, trace_id=None),
    ]
    report = aggregate_report(results, dry_run=False, started_at="s", finished_at="f")
    # non-skipped = a, b ; covered = a -> 1/2 -> 50%
    assert report.trace_coverage_pct == pytest.approx(50.0)


def test_trace_coverage_full() -> None:
    results = [
        _result("a", status="accepted", accepted=True, cost=0.01, trace_id="t-a"),
        _result("b", status="failed", accepted=False, cost=0.01, trace_id="t-b"),
    ]
    report = aggregate_report(results, dry_run=False, started_at="s", finished_at="f")
    assert report.trace_coverage_pct == pytest.approx(100.0)


def test_routing_regression_rate() -> None:
    results = [
        _result("a", status="accepted", accepted=True, cost=0.01, trace_id="t-a"),
        _result("b", status="failed", accepted=False, cost=0.01, trace_id="t-b", regression=True),
        _result("c", status="failed", accepted=False, cost=0.01, trace_id="t-c", regression=False),
        _result("d", status="failed", accepted=False, cost=0.01, trace_id="t-d", regression=True),
    ]
    report = aggregate_report(results, dry_run=False, started_at="s", finished_at="f")
    # 2 regressions / 4 cases
    assert report.routing_regression_rate == pytest.approx(0.5)


def test_routing_regression_rate_zero_for_dry_run() -> None:
    results = [
        _result("a", status="skipped", regression=True),
        _result("b", status="skipped", regression=True),
    ]
    report = aggregate_report(results, dry_run=True, started_at="s", finished_at="f")
    assert report.status == "dry_run"
    assert report.routing_regression_rate == 0.0


def test_status_pass_when_all_accepted() -> None:
    results = [
        _result("a", status="accepted", accepted=True, cost=0.01, trace_id="t-a"),
        _result("b", status="accepted", accepted=True, cost=0.01, trace_id="t-b"),
    ]
    report = aggregate_report(results, dry_run=False, started_at="s", finished_at="f")
    assert report.status == "pass"


def test_status_fail_when_any_failed() -> None:
    results = [
        _result("a", status="accepted", accepted=True, cost=0.01, trace_id="t-a"),
        _result("b", status="failed", accepted=False, cost=0.01, trace_id="t-b"),
    ]
    report = aggregate_report(results, dry_run=False, started_at="s", finished_at="f")
    assert report.status == "fail"


def test_report_status_not_derived_from_token_reduction() -> None:
    """Even with a high context reduction, a failed case keeps status=fail."""
    results = [
        _result("a", status="failed", accepted=False, cost=0.5, trace_id="t-a"),
    ]
    report = aggregate_report(
        results,
        dry_run=False,
        started_at="s",
        finished_at="f",
        context_reduction_pct=99.0,
    )
    assert report.status == "fail"
    assert report.context_reduction_pct == 99.0


def test_empty_suite_does_not_crash() -> None:
    report = aggregate_report([], dry_run=False, started_at="s", finished_at="f")
    assert isinstance(report, MiniEvalReport)
    assert report.total_tasks == 0
    assert report.accepted_patch_rate == 0.0
    assert report.cost_per_accepted_patch == 0.0
    assert report.trace_coverage_pct == 0.0
    assert report.status == "fail"
