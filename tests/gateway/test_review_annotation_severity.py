"""The annotation wire carries P0/P1/P2 for AI findings, by the product's one severity rule.

The Reader draws a severity badge and rail from ``annotation.severity``. The server computes it with
``criticality.severity_for`` (the function the pull request bot stores on its findings) from the evidence row
written when the finding was confirmed. A human comment keeps its kind and carries no severity at all, and the
field is optional so an older client and an older row both keep working.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review import criticality
from lemoncrow.pro.capabilities.review.pr_bot import findings as bot_findings
from lemoncrow.pro.capabilities.review.review_triage import (
    FINDING_EVIDENCE_KIND,
    FindingEvidence,
    ai_finding_evidence,
    finding_severities,
    record_finding_evidence,
)
from lemoncrow.pro.capabilities.review.sources.local import annotate_machine_finding
from tests.gateway.test_review_triage_api import _CHANGED, HEADERS, _get, _next_snapshot, _review


def _finding(
    review: SimpleNamespace,
    line: int,
    *,
    category: str | None,
    impact: str = "",
    verified: bool = False,
    rule_refs: tuple[str, ...] = (),
    revision_id: str | None = None,
) -> str:
    """One AI finding on ``src/auth.py``; ``category=None`` writes no severity evidence (an unrated finding)."""

    session = review.store.get_session(review.review_id)
    revision = review.store.latest_revision(review.review_id)
    annotation, _created = annotate_machine_finding(
        review.store,
        session,
        revision,
        path="src/auth.py",
        start_line=line,
        end_line=line,
        body=f"finding on line {line}",
        title=f"finding {category or 'unrated'} {line}",
        created_by="investigator",
        created_by_actor="agent",
        source="ai_review",
        source_id=f"investigator-{line}",
        new_text=_CHANGED,
    )
    if rule_refs:  # the machine-finding helper takes no rule refs, and the column is not updatable
        with review.store._transaction() as conn:
            conn.execute(
                "UPDATE annotations SET rule_refs_json = ? WHERE id = ?", (json.dumps(list(rule_refs)), annotation.id)
            )
    if category is not None:
        record_finding_evidence(
            review.store,
            review_id=review.review_id,
            revision_id=revision_id or revision.id,
            rows=[
                ai_finding_evidence(
                    annotation.id,
                    finding_id=f"f{line}",
                    category=category,
                    target_ids=(),
                    title=category,
                    impact=impact,
                    verified=verified,
                )
            ],
        )
    return str(annotation.id)


def _by_id(review: SimpleNamespace) -> dict[str, dict[str, Any]]:
    return {row["id"]: row for row in _get(review, "annotations")["annotations"]}


def _human_comment(review: SimpleNamespace, *, kind: str = "comment") -> str:
    response = review.client.post(
        f"/api/reviews/{review.review_id}/annotations",
        headers=HEADERS,
        json={
            "path": "src/auth.py",
            "file_level": True,
            "body": "A person wrote this.",
            "kind": kind,
            "expected_revision_id": review.store.latest_revision(review.review_id).id,
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["annotation"]["id"])


def test_the_wire_carries_a_severity_for_an_ai_finding_and_none_for_a_human_comment(tmp_path: Path) -> None:
    review = _review(tmp_path)
    security = _finding(review, 2, category="security")
    performance = _finding(review, 3, category="performance")
    comment = _human_comment(review)
    change = _human_comment(review, kind="request_change")

    rows = _by_id(review)

    assert rows[security]["severity"] == "p1"
    assert rows[performance]["severity"] == "p2"
    # A person's comment keeps its kind, whatever that kind is, and is never rated.
    for annotation_id, kind in ((comment, "comment"), (change, "request_change")):
        assert rows[annotation_id]["kind"] == kind
        assert rows[annotation_id]["source"] == "human"
        assert "severity" not in rows[annotation_id]


def test_an_unrated_ai_finding_has_no_severity_rather_than_a_guess(tmp_path: Path) -> None:
    review = _review(tmp_path)
    unrated = _finding(review, 2, category=None)
    unknown = _finding(review, 3, category="made_up")

    rows = _by_id(review)

    assert rows[unrated]["source"] == "ai_review" and "severity" not in rows[unrated]
    assert rows[unknown]["source"] == "ai_review" and "severity" not in rows[unknown]


@pytest.mark.parametrize(
    ("category", "impact", "verified", "cited"),
    [
        ("correctness", "high", True, False),  # P0: a verified high finding in a P0 category
        ("correctness", "high", False, False),  # P1: speculation is never P0
        ("concurrency_state", "high", True, False),  # P1: outside the P0 categories
        ("api_compatibility", "medium", True, False),
        ("api_compatibility", "medium", False, False),
        ("security", "low", True, False),
        ("critical_rule", "low", False, True),  # P0 whatever the rating, because it cites its rule
        ("critical_rule", "high", True, False),  # no rule cited: not a gate
        ("performance", "", False, False),  # unrated keeps the category's own severity
        ("verification", "", True, False),
    ],
)
def test_the_wire_severity_is_the_pr_bot_rule_applied_to_the_recorded_evidence(
    tmp_path: Path, category: str, impact: str, verified: bool, cited: bool
) -> None:
    review = _review(tmp_path)
    annotation_id = _finding(
        review,
        2,
        category=category,
        impact=impact,
        verified=verified,
        rule_refs=("rule:no-open-redirect",) if cited else (),
    )

    wire = _by_id(review)[annotation_id]["severity"]

    assert wire == bot_findings.severity_for(category, impact=impact, verified=verified, rule_cited=cited)
    assert wire in {"p0", "p1", "p2"}


def test_the_pr_bot_and_the_reader_share_one_severity_function() -> None:
    """Moving the rule must not fork it: the bot's names are the neutral module's own objects."""

    assert bot_findings.severity_for is criticality.severity_for
    assert bot_findings.P0_CATEGORIES is criticality.P0_CATEGORIES
    assert bot_findings.IMPACTS is criticality.FINDING_IMPACTS


def test_a_verified_high_finding_is_p0_on_the_wire_while_the_triage_summary_keeps_its_category_policy(
    tmp_path: Path,
) -> None:
    review = _review(tmp_path)
    annotation_id = _finding(review, 2, category="security", impact="high", verified=True)

    assert _by_id(review)[annotation_id]["severity"] == "p0"
    # Only the optional wire field changed: the summary, threshold and priority still read the category policy.
    summary = _get(review, "projection")["triage"]["severity_summary"]
    assert (summary["p0"], summary["p1"]) == (0, 1)


def test_a_finding_is_rated_by_the_newest_snapshot_that_holds_evidence_for_it(tmp_path: Path) -> None:
    review = _review(tmp_path)
    first = review.revision_id
    annotation_id = _finding(review, 2, category="performance", impact="low")
    assert _by_id(review)[annotation_id]["severity"] == "p2"

    second = _next_snapshot(review)
    # Not revalidated on the second Snapshot: it keeps the rating it was last given.
    assert _by_id(review)[annotation_id]["severity"] == "p2"

    record_finding_evidence(
        review.store,
        review_id=review.review_id,
        revision_id=second,
        rows=[
            ai_finding_evidence(
                annotation_id,
                finding_id="f2",
                category="correctness",
                target_ids=(),
                title="t",
                impact="high",
                verified=True,
            )
        ],
    )
    assert _by_id(review)[annotation_id]["severity"] == "p0"

    # The first Snapshot's view (zero-based in the URL) is not rewritten by what a later one learned.
    assert first != second
    historical = _get(review, "revisions/0/annotations")
    assert historical["revision_id"] == first
    assert {row["id"]: row.get("severity") for row in historical["annotations"]}[annotation_id] == "p2"


def test_resolving_or_dismissing_a_finding_returns_it_with_its_severity(tmp_path: Path) -> None:
    review = _review(tmp_path)
    annotation_id = _finding(review, 2, category="security", impact="high", verified=True)
    revision = review.store.latest_revision(review.review_id)

    response = review.client.patch(
        f"/api/annotations/{annotation_id}",
        headers=HEADERS,
        json={
            "finding_disposition": "dismissed",
            "finding_disposition_reason": "incorrect",
            "expected_revision_id": revision.id,
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["annotation"]["finding_disposition"] == "dismissed"
    assert response.json()["annotation"]["severity"] == "p0"


def test_a_human_comment_patch_response_has_no_severity(tmp_path: Path) -> None:
    review = _review(tmp_path)
    comment = _human_comment(review)

    response = review.client.patch(f"/api/annotations/{comment}", headers=HEADERS, json={"body": "Reworded."})

    assert response.status_code == 200, response.text
    assert "severity" not in response.json()["annotation"]


def test_evidence_rows_written_before_the_rating_was_kept_still_read() -> None:
    legacy = {
        "annotation_id": "a",
        "finding_id": "f",
        "source": "ai_review",
        "category": "security",
        "severity": "p1",
        "target_ids": [],
        "title": "t",
    }
    parsed = FindingEvidence.from_wire(legacy)

    assert parsed is not None and (parsed.impact, parsed.verified) == ("", False)
    assert parsed.to_wire() == legacy  # a row without a rating is written exactly as before

    class _Store:
        def derived_results(self, kind: str, revision_ids: Any) -> dict[tuple[str, str], tuple[str, dict[str, Any]]]:
            assert kind == FINDING_EVIDENCE_KIND
            return {("r1", "a"): ("f", legacy)}

    finding = SimpleNamespace(id="a", source="ai_review", parent_id="", rule_refs=())
    reply = SimpleNamespace(id="b", source="ai_review", parent_id="a", rule_refs=())
    human = SimpleNamespace(id="c", source="human", parent_id="", rule_refs=())
    assert finding_severities(_Store(), [finding, reply, human], ["r1"]) == {"a": "p1"}
    assert finding_severities(_Store(), [finding], []) == {}
