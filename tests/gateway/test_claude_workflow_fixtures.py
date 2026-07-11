from __future__ import annotations

import json
from pathlib import Path

LEMONCROW_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = (
    LEMONCROW_ROOT / "integrations" / "claude" / "plugin" / "workflows" / "fixtures" / "code-audit-review-fixture.json"
)


def _precision(findings: set[str], truth: set[str]) -> float:
    return len(findings & truth) / len(findings) if findings else 0.0


def _recall(findings: set[str], truth: set[str]) -> float:
    return len(findings & truth) / len(truth) if truth else 1.0


def test_code_audit_fixture_demonstrates_cross_check_improvement() -> None:
    payload = json.loads(FIXTURE_PATH.read_text())

    truth = set(payload["truth"]["accepted_findings"])
    single_findings = set(payload["single_pass"]["findings"])
    cross_check_findings = set(payload["cross_check"]["findings"])
    expected = payload["expected_metrics"]

    single_precision = _precision(single_findings, truth)
    cross_check_precision = _precision(cross_check_findings, truth)
    single_recall = _recall(single_findings, truth)
    cross_check_recall = _recall(cross_check_findings, truth)

    assert round(single_precision, 4) == expected["single_pass_precision"]
    assert round(cross_check_precision, 4) == expected["cross_check_precision"]
    assert round(single_recall, 4) == expected["single_pass_recall"]
    assert round(cross_check_recall, 4) == expected["cross_check_recall"]
    assert cross_check_precision > single_precision
    assert cross_check_recall >= single_recall


def test_code_audit_fixture_rejected_findings_match_truth_rejections() -> None:
    payload = json.loads(FIXTURE_PATH.read_text())

    rejected_truth = set(payload["truth"]["rejected_findings"])
    rejected_cross_check = set(payload["cross_check"]["rejected"])

    assert rejected_cross_check == rejected_truth
