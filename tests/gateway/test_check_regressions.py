"""Only exact, comparable, persisted test regressions earn an evidence-linked row."""

from types import SimpleNamespace

from lemoncrow.pro.capabilities.review.check_regressions import cited_regressions, observed_test_regressions


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


def test_exact_current_snapshot_annotation_citation_links_observed_test_without_proving_defect():
    observation = observed_test_regressions([_result()])
    annotation = SimpleNamespace(
        id="ai-finding-1",
        revision_id="snapshot-current",
        source="ai_review",
        parent_id="",
        evidence=("check-recipe:unit", "test:tests.test_calc::test_mul", "target:file"),
    )
    result = cited_regressions([annotation], observation, revision_id="snapshot-current")
    assert result["schema_version"] == 1 and result["omitted"] == 0
    assert len(result["rows"]) == 1
    row = result["rows"][0]
    assert row["annotation_id"] == "ai-finding-1"
    assert row["differential_evidence_id"] == "evidence-a"
    assert row["independent_execution"] is False and row["confirmed_defect"] is False
    assert "not independently confirm" in row["limitation"]
    assert cited_regressions([annotation], observation, revision_id="snapshot-next")["rows"] == []


def test_citation_join_refuses_partial_refs_fabricated_rows_and_other_sources():
    observation = observed_test_regressions([_result()])
    complete = ("check-recipe:unit", "test:tests.test_calc::test_mul")
    for source, refs, parent, revision in (
        ("ai_review", ("check-recipe:unit",), "", "current"),
        ("ai_review", ("test:tests.test_calc::test_mul",), "", "current"),
        ("ai_review", ("check-recipe:wrong", *complete[1:]), "", "current"),
        ("ai_review", complete, "parent-id", "current"),
        ("ai_review", complete, "", "previous"),
        ("agent", complete, "", "current"),
    ):
        annotation = SimpleNamespace(
            id="finding",
            revision_id=revision,
            source=source,
            parent_id=parent,
            evidence=refs,
        )
        assert cited_regressions([annotation], observation, revision_id="current")["rows"] == []
    annotation = SimpleNamespace(
        id="feedback",
        revision_id="current",
        source="human",
        parent_id="",
        evidence=complete,
    )
    assert len(cited_regressions([annotation, annotation], observation, revision_id="current")["rows"]) == 1
    assert (
        cited_regressions([annotation], observed_test_regressions([_result(comparable=False)]), revision_id="current")[
            "rows"
        ]
        == []
    )
    assert (
        cited_regressions([annotation], observed_test_regressions([_result(klass="flaky")]), revision_id="current")[
            "rows"
        ]
        == []
    )


def test_worker_report_cannot_claim_independent_execution_without_process_attestation():
    for executor in ("managed_sandbox", "customer_worker", "author_machine"):
        reported = _result()
        reported["executor_class"] = executor
        reported["signed_attestation"] = True  # Untrusted self-claim.
        reported["queue_completion"] = {"verified": True}
        (observation,) = observed_test_regressions([reported])["rows"]
        assert observation["executor_class"] == executor
        assert observation["independent_execution"] is False
        assert observation["differential_evidence_id"] == "evidence-a"
        assert observation["observation"] == "passed_on_base_failed_on_head"


def test_repeated_test_subject_in_same_receipt_is_one_observation_not_two_findings():
    row = _result()
    row["attention"].append(dict(row["attention"][0]))
    output = observed_test_regressions([row])
    assert len(output["rows"]) == 1
    assert output["omitted"] == 0
    assert output["rows"][0]["subject"] == "tests.test_calc::test_mul"
    different = _result(evidence="evidence-b")
    assert len(observed_test_regressions([row, different])["rows"]) == 2


def test_untyped_test_subject_cannot_be_coerced_into_a_proven_failure():
    for value in (None, 7, True, {"id": "test"}, ""):
        row = _result()
        row["attention"][0]["subject"] = value
        assert observed_test_regressions([row])["rows"] == []


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
