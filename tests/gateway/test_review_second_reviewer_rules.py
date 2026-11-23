"""P7-1: the second reviewer rules are pure and fail closed (SPEC 4.16, D1, R3, R13)."""

from __future__ import annotations

import pytest

from lemoncrow.pro.capabilities.review.second_reviewer import (
    DecisionPart,
    OwnerRef,
    SecondPlan,
    agrees_with,
    answered,
    area_label,
    first_answers_visible,
    plan_second_reviewer,
    second_state,
    settled_verdict,
    signature_counts,
)

MAYA = OwnerRef("usr_maya", "mchen", "Maya Chen")
DANA = OwnerRef("usr_dana", "dkim", "Dana Kim")
AUTH = DecisionPart("u-auth", "app/auth.py", "can_view_order", 6, 9)
PAY = DecisionPart("u-pay", "app/payments/refund.py", "refund", 1, 4)


def plan(parts=(AUTH,), owners=None, excluded=("usr_sam",)):
    return plan_second_reviewer(parts, owners if owners is not None else {"app/auth.py": [MAYA]}, excluded_ids=excluded)


def test_the_codeowner_of_a_decision_part_is_the_second_reviewer() -> None:
    result = plan()
    assert (result.required, result.reviewer, result.area, result.parts) == (True, MAYA, "app/auth", (AUTH,))


@pytest.mark.parametrize("barred", ["usr_maya"])
def test_the_author_or_the_first_reviewer_is_never_chosen(barred: str) -> None:
    result = plan(excluded=("usr_sam", barred))
    assert result.required is False
    assert result.reviewer is None
    # the CODEOWNER exists but cannot sign: say so, never silently pass
    assert "no CODEOWNER" in result.unavailable
    assert second_state(result, first_outcome="lgtm", second_outcome=None) == "unavailable"


def test_no_codeowner_at_all_is_not_required_and_says_nothing() -> None:
    result = plan(owners={})
    assert (result.required, result.unavailable) == (False, "")
    assert second_state(result, first_outcome="lgtm", second_outcome=None) == "not_required"


def test_a_provider_handle_with_no_principal_cannot_sign() -> None:
    result = plan(owners={"app/auth.py": [OwnerRef("", "ghost")]})
    assert result.required is False and result.unavailable


def test_no_decision_parts_asks_nobody() -> None:
    assert plan(parts=()).required is False


def test_the_owner_of_the_most_decision_parts_wins_then_the_lower_id() -> None:
    owners = {"app/auth.py": [MAYA, DANA], "app/payments/refund.py": [DANA]}
    assert plan((AUTH, PAY), owners).reviewer == DANA
    tie = plan((AUTH,), {"app/auth.py": [MAYA, DANA]})
    assert tie.reviewer == DANA  # usr_dana < usr_maya


def test_the_area_names_only_what_the_second_reviewer_owns() -> None:
    result = plan((AUTH, PAY), {"app/auth.py": [MAYA], "app/payments/refund.py": [DANA]})
    assert (result.reviewer, result.area, result.parts) == (DANA, "app/payments/refund", (PAY,))


def test_area_label() -> None:
    assert area_label(["app/auth.py"]) == "app/auth"
    assert area_label(["app/auth/a.py", "app/auth/b.py"]) == "app/auth"
    assert area_label(["a.py", "b/c.py"]) == ""
    assert area_label([]) == ""


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (None, None, "will_be_asked"),
        ("changes_requested", None, "not_needed_yet"),
        ("lgtm", None, "waiting"),
        ("lgtm", "lgtm", "agrees"),
        ("lgtm", "changes_requested", "disagrees"),
    ],
)
def test_the_second_reviewer_is_asked_only_when_the_first_approves(first, second, expected) -> None:
    assert second_state(plan(), first_outcome=first, second_outcome=second) == expected


def test_a_request_for_changes_never_waits_for_anyone_even_with_a_stale_second_answer() -> None:
    assert second_state(plan(), first_outcome="changes_requested", second_outcome="lgtm") == "not_needed_yet"


def test_any_needs_a_change_blocks_approval_and_waiting_is_not_approved() -> None:
    waiting = second_state(plan(), first_outcome="lgtm", second_outcome=None)
    assert settled_verdict(waiting, "lgtm") == "waiting"
    assert settled_verdict("agrees", "lgtm") == "approved"
    assert settled_verdict("disagrees", "lgtm") == "changes_requested"
    assert settled_verdict("not_needed_yet", "changes_requested") == "changes_requested"
    assert settled_verdict("not_required", "lgtm") == "approved"
    assert settled_verdict("will_be_asked", None) == "waiting"


def test_counts_are_only_signatures_that_exist() -> None:
    assert signature_counts(first_signed=True, second_signed=False, required=True) == (1, 2)
    assert signature_counts(first_signed=True, second_signed=True, required=True) == (2, 2)
    # a second signature nobody required is not counted
    assert signature_counts(first_signed=True, second_signed=True, required=False) == (1, 1)
    assert signature_counts(first_signed=False, second_signed=False, required=True) == (0, 2)


def test_answered_needs_a_real_answer_for_every_asked_part() -> None:
    both = SecondPlan(True, MAYA, "app", (AUTH, PAY))
    assert not answered(both, {})
    assert not answered(both, {"u-auth": "reviewed"})
    assert not answered(both, {"u-auth": "reviewed", "u-pay": "later"})
    assert not answered(both, {"u-auth": "reviewed", "u-pay": "unreviewed"})
    assert answered(both, {"u-auth": "reviewed", "u-pay": "needs_changes"})
    assert agrees_with(both, {"u-auth": "reviewed", "u-pay": "reviewed"})
    assert not agrees_with(both, {"u-auth": "reviewed", "u-pay": "needs_changes"})
    assert not answered(SecondPlan(False), {"x": "reviewed"})


def test_the_first_reviewers_answers_stay_hidden_from_the_second_until_they_answer() -> None:
    assert first_answers_visible(viewer_is_second=True, second_answered=False) is False
    assert first_answers_visible(viewer_is_second=True, second_answered=True) is True
    assert first_answers_visible(viewer_is_second=False, second_answered=False) is True
