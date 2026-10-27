"""Only exact, comparable, persisted test regressions earn an evidence-linked row."""

from lemoncrow.pro.capabilities.review.check_regressions import observed_test_regressions


def _result(*, comparable=True, klass="introduced_failure", evidence="evidence-a", head="FAIL", quarantine=""):
    return {
        "recipe": {"id": "unit"},
        "comparable": comparable,
        "head_verdict": head,
        "executor_class": "author_machine",
        "evidence_id": evidence,
        "attention": [
            {
                "subject": "tests.test_calc::test_mul",
                "file": "tests/test_calc.py",
                "class": klass,
                "base": "passed",
                "head": "failed",
                "quarantine": quarantine,
            }
        ],
    }


def test_exact_comparable_failure_is_linked_but_never_called_a_confirmed_defect():
    output = observed_test_regressions([_result()])
    assert output["schema_version"] == 1 and output["omitted"] == 0
    (row,) = output["rows"]
    assert row["subject"] == "tests.test_calc::test_mul"
    assert row["differential_evidence_id"] == "evidence-a"
    assert row["independent_execution"] is False
    assert "not a confirmed code defect" in row["limitation"]


def test_unknown_environment_flaky_quarantine_and_missing_evidence_do_not_make_a_regression():
    candidates = [
        _result(comparable=None),
        _result(comparable=False),
        _result(klass="flaky"),
        _result(klass="quarantined"),
        _result(head="PASS"),
        _result(quarantine="network"),
        _result(evidence=""),
    ]
    assert observed_test_regressions(candidates)["rows"] == []
