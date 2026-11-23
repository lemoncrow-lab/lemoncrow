"""P9-INBOX pure rules: workflow state, readiness facts, SCM axis, attention, views, paging, saved filters."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.inbox_views import (
    INBOX_MAX_LIMIT,
    INBOX_VIEWS,
    InboxQueryError,
    InboxRow,
    InboxSelection,
    ViewFacts,
    decode_cursor,
    encode_cursor,
    inbox_as_of,
    inbox_views,
    narrow,
    parse_limit,
    select_page,
    updated_sort_key,
)
from lemoncrow.pro.capabilities.review.personal_attention import (
    PersonalAttentionInputs,
    ViewerReason,
    attention_sort_key,
    order_by_attention,
    personal_attention,
)
from lemoncrow.pro.capabilities.review.saved_filters import (
    MAX_SAVED_FILTERS,
    SavedFilterConflict,
    SavedFilterInvalid,
    SavedFilterLimit,
    SqliteSavedFilterStore,
    request_selection,
    selection_view,
    validate_document,
    validate_selection,
    wants_view_mode,
)
from lemoncrow.pro.capabilities.review.workflow_state import (
    WORKFLOW_STATES,
    ReadinessFact,
    WorkflowFacts,
    derive_workflow_state,
    readiness_fact,
    readiness_summary_payload,
    scm_axis,
)

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
READY = ReadinessFact(state="ready", reason="All readiness conditions hold", scm_conditions=1, reviewed=True)
NOT_READY = ReadinessFact(state="not_ready", reason="1 readiness condition(s) not satisfied", blocker_count=1)
STALE = ReadinessFact(state="stale", reason="stale")


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def test_inbox_workflow_state_contract() -> None:
    """case: test_workflow_state_rules"""
    for facts, state, rule in [
        (WorkflowFacts(status="finished"), "closed", "closed"),
        (WorkflowFacts(status="archived"), "closed", "closed"),
        (WorkflowFacts(provider_closed=True), "closed", "closed"),
        (WorkflowFacts(merged=True), "closed", "closed"),
        (WorkflowFacts(merged=True, post_merge_running=2), "monitoring", "post_merge_running"),
        (WorkflowFacts(status="finished", post_merge_running=2), "closed", "closed"),
        (WorkflowFacts(post_merge_running=2), "needs_review", "not_reviewed"),
        (WorkflowFacts(reviewer_reasons=1), "needs_review", "review_requested"),
        (WorkflowFacts(reviewer_reasons=1, review_evidence=True), "waiting_on_reviewer", "reviewer_turn"),
        (WorkflowFacts(threads_returned=1, review_evidence=True), "waiting_on_reviewer", "reviewer_turn"),
        (WorkflowFacts(author_reasons=1), "waiting_on_author", "author_turn"),
        (WorkflowFacts(threads_waiting_on_author=1, author_is_agent=True), "waiting_on_author", "agent_turn"),
        (WorkflowFacts(feedback_with_agent=1), "waiting_on_author", "agent_turn"),
        (WorkflowFacts(check_failures=1, review_complete=True, readiness=READY), "waiting_on_author", "author_turn"),
        (WorkflowFacts(decision_reasons=1, review_complete=True), "needs_decision", "decision_reason"),
        (WorkflowFacts(), "needs_review", "not_reviewed"),
        (WorkflowFacts(readiness=READY), "needs_review", "not_reviewed"),
        (
            WorkflowFacts(review_complete=True, readiness=replace(NOT_READY, pending_checks=1)),
            "waiting_on_checks",
            "checks_pending",
        ),
        (
            WorkflowFacts(review_complete=True, readiness=replace(STALE, pending_checks=1)),
            "needs_decision",
            "readiness_stale",
        ),
        (WorkflowFacts(review_complete=True, readiness=READY), "ready_to_merge", "readiness_ready"),
        (WorkflowFacts(review_complete=True, readiness=NOT_READY), "needs_decision", "readiness_not_ready"),
        (WorkflowFacts(review_complete=True), "needs_decision", "readiness_unknown"),
    ]:
        decision = derive_workflow_state(facts)
        assert (decision.state, decision.rule) == (state, rule), decision
        assert decision.state in WORKFLOW_STATES and decision.explanation
        assert derive_workflow_state(facts) == decision
    "case: test_ready_to_merge_comes_only_from_a_ready_record_on_completed_review"
    "Exhaustive over the boolean facts: no other combination reaches ready_to_merge."
    for readiness in (READY, NOT_READY, STALE, ReadinessFact()):
        for complete in (False, True):
            for evidence in (False, True):
                facts = WorkflowFacts(review_complete=complete, review_evidence=evidence, readiness=readiness)
                ready = derive_workflow_state(facts).state == "ready_to_merge"
                assert ready == (complete and readiness.state == "ready"), facts
    "case: test_the_waiting_on_agent_label_is_distinct_from_waiting_on_author"
    agent = derive_workflow_state(WorkflowFacts(feedback_with_agent=1))
    author = derive_workflow_state(WorkflowFacts(author_reasons=1))
    assert agent.label == "Waiting on agent" and author.label == "Waiting on author"
    assert agent.to_wire()["state"] == author.to_wire()["state"] == "waiting_on_author"
    "case: test_readiness_fact_classifies_records_conservatively"
    assert readiness_fact(None, revision_id="rrv-1", watermark="", now=NOW).state == "unknown"
    fresh = readiness_fact(_payload(), revision_id="rrv-1", watermark="", now=NOW)
    assert fresh.state == "ready" and fresh.reviewed and (fresh.scm_conditions == 1)
    earlier = readiness_fact(_payload(), revision_id="rrv-2", watermark="", now=NOW)
    assert earlier.state == "unknown" and "earlier revision" in earlier.reason
    later_fact = readiness_fact(_payload(), revision_id="rrv-1", watermark=_iso(NOW), now=NOW)
    assert later_fact.state == "stale" and (not later_fact.reviewed)
    expired = readiness_fact(
        _payload(valid_until=_iso(NOW - timedelta(seconds=1))), revision_id="rrv-1", watermark="", now=NOW
    )
    assert expired.state == "stale"
    undated = readiness_fact(_payload(evaluated_at=""), revision_id="rrv-1", watermark="", now=NOW)
    assert undated.state == "unknown"
    pending = _payload(
        ready=False, conditions=[{"id": "required-check:ci", "owner": "scm", "category": "scm", "state": "pending"}]
    )
    waiting = readiness_fact(pending, revision_id="rrv-1", watermark="", now=NOW)
    assert waiting.state == "not_ready" and waiting.pending_checks == 1 and (not waiting.reviewed)
    contradictory = dict(_payload(), blocker_count=1)
    assert readiness_fact(contradictory, revision_id="rrv-1", watermark="", now=NOW).state == "not_ready"
    "case: test_the_summary_keeps_no_condition_labels_or_evidence"
    payload = readiness_summary_payload(
        {
            "ready": False,
            "conditions": [
                {"id": "c", "owner": "scm", "category": "scm", "state": "fail", "label": "secret", "detail": "x"}
            ],
        },
        review_id="r",
        revision_id="s",
    )
    assert "secret" not in json.dumps(payload) and payload["failed_checks"] == 1
    "case: test_scm_axis_needs_positive_evidence"
    for readiness, kwargs, state in [
        (ReadinessFact(), {"connected": True}, "unknown"),
        (STALE, {"connected": True}, "unknown"),
        (STALE, {"connected": False}, "not_connected"),
        # No linked provider never claims anything about merging, even with passing local checks.
        (READY, {"connected": False}, "not_connected"),
        (NOT_READY, {"connected": False, "failed_check_reasons": 1}, "not_connected"),
        (ReadinessFact(), {"connected": True, "failed_check_reasons": 1}, "not_ready"),
        (replace(READY, scm_conditions=0), {"connected": False}, "not_connected"),
        (replace(READY, scm_conditions=0), {"connected": True}, "unknown"),
        (READY, {"connected": True, "sync_state": "stale"}, "unknown"),
        (READY, {"connected": True, "sync_state": "synced"}, "ready"),
        (replace(NOT_READY, scm_blockers=1, scm_conditions=1), {"connected": True}, "not_ready"),
        (replace(NOT_READY, pending_checks=1, scm_conditions=1), {"connected": True}, "not_ready"),
    ]:
        assert scm_axis(readiness, **kwargs)["state"] == state


def test_inbox_attention_order_contract() -> None:
    """case: test_personal_attention_factors_are_explained_and_the_order_is_total"""
    old = _iso(NOW - timedelta(days=8))
    inputs = PersonalAttentionInputs(
        open=True,
        reasons=(ViewerReason("work_returned", since=old, fact="work back"), ViewerReason("mention")),
        updated_at=_iso(NOW - timedelta(hours=2)),
        direct_request=True,
        engaged=True,
        priority="urgent",
        criticality="critical",
        effort=3,
    )
    attention = personal_attention(inputs, now=NOW)
    keys = [factor.key for factor in attention.factors]
    assert keys == ["ownership", "waiting_age", "activity", "assignment", "engagement", "impact", "effort"]
    assert attention.score == sum(factor.points for factor in attention.factors) == 40 + 15 + 5 + 10 + 5 + 20 + 5
    assert attention.waiting_since == old
    assert personal_attention(inputs, now=NOW) == attention
    closed = personal_attention(replace(inputs, open=False), now=NOW)
    assert closed.score == 0 and closed.factors == ()
    team = personal_attention(PersonalAttentionInputs(open=True, team_request=True), now=NOW)
    assert [factor.detail for factor in team.factors] == ["team review request"]
    same = personal_attention(PersonalAttentionInputs(open=True), now=NOW)
    rows = [(same, "repo-b", "r-1"), (same, "repo-a", "r-2"), (attention, "repo-z", "r-9"), (same, "repo-a", "r-1")]
    ordered = order_by_attention(rows)
    assert [(repo, review) for _a, repo, review in ordered] == [
        ("repo-z", "r-9"),
        ("repo-a", "r-1"),
        ("repo-a", "r-2"),
        ("repo-b", "r-1"),
    ]
    assert attention_sort_key(same, repo_id="a", review_id="b")[1] == "~"
    "case: test_the_oldest_wait_breaks_score_ties"
    newer = personal_attention(
        PersonalAttentionInputs(open=True, reasons=(ViewerReason("mention", since=_iso(NOW - timedelta(hours=1))),)),
        now=NOW,
    )
    older = personal_attention(
        PersonalAttentionInputs(open=True, reasons=(ViewerReason("mention", since=_iso(NOW - timedelta(hours=5))),)),
        now=NOW,
    )
    assert newer.score == older.score
    assert attention_sort_key(older, repo_id="z", review_id="z") < attention_sort_key(newer, repo_id="a", review_id="a")
    "case: test_view_membership"
    assert inbox_views(ViewFacts(open=True, reviewer_turn_for_me=True, attention_for_me=True)) == (
        "to_review",
        "waiting_on_me",
    )
    closed = ViewFacts(
        open=False,
        reviewer_turn_for_me=True,
        team_request=True,
        authored=True,
        participated=True,
        attention_for_me=True,
        workflow_state="closed",
    )
    assert inbox_views(closed) == ("authored_by_me", "participated")
    assert inbox_views(ViewFacts(open=True, workflow_state="ready_to_merge")) == ("ready",)
    for state in ("waiting_on_reviewer", "waiting_on_author", "waiting_on_checks", "needs_decision"):
        assert inbox_views(ViewFacts(open=True, workflow_state=state)) == ("blocked",)
        assert "blocked" not in inbox_views(ViewFacts(open=True, attention_for_me=True, workflow_state=state))
    assert inbox_views(ViewFacts(open=True, workflow_state="needs_review")) == ()


def test_updated_sort_key_is_exact_timezone_aware_and_stable() -> None:
    first = updated_sort_key("2026-10-13T21:46:00Z", repo_id="a", review_id="old")
    latest = updated_sort_key("2026-10-13T23:46:00.000001+02:00", repo_id="b", review_id="new")
    same = updated_sort_key("2026-10-13T21:46:00+00:00", repo_id="a", review_id="second")
    unknown = updated_sort_key("", repo_id="a", review_id="unknown")
    assert latest < first < same < unknown
    assert first == updated_sort_key("2026-10-13T23:46:00+02:00", repo_id="a", review_id="old")
    rows = [
        InboxRow("a", "old", "open", (), "needs_review", "urgent", first),
        InboxRow("b", "new", "open", (), "needs_review", "normal", latest),
        InboxRow("a", "unknown", "open", (), "needs_review", "normal", unknown),
    ]
    page = select_page(rows, InboxSelection(), limit=2, cursor=None, as_of=NOW)
    assert [row.review_id for row in page.rows] == ["new", "old"]
    assert page.next_cursor
    following = select_page(rows, InboxSelection(), limit=2, cursor=decode_cursor(page.next_cursor), as_of=NOW)
    assert [row.review_id for row in following.rows] == ["unknown"]


def test_inbox_paging_contract() -> None:
    """case: test_limits_and_cursors_are_bounded_and_strict"""
    assert parse_limit(None) == 30 and parse_limit("1") == 1 and (parse_limit(str(INBOX_MAX_LIMIT)) == INBOX_MAX_LIMIT)
    for raw in ("0", str(INBOX_MAX_LIMIT + 1), "-1", "ten", "1.5"):
        with pytest.raises(InboxQueryError) as caught:
            parse_limit(raw)
        assert caught.value.field == "limit"
    cursor = encode_cursor(NOW, (-3, "~", "repo", "r-1"))
    decoded = decode_cursor(cursor)
    assert decoded is not None and decoded.after == (-3, "~", "repo", "r-1")
    assert inbox_as_of(decoded, NOW + timedelta(hours=1)) == NOW
    assert decode_cursor("") is None
    for raw in ("%%%", "A" * 1025, encode_cursor(NOW, ("x", "~", "repo", "r")), "eyJ2IjoyfQ"):
        with pytest.raises(InboxQueryError):
            decode_cursor(raw)
    "case: test_paging_at_and_past_the_limit_visits_every_row_once"
    for count in [0, 1, 9, 10, 11, 25]:
        rows = _rows(count)
        seen: list[str] = []
        cursor = None
        for _page in range(10):
            page = select_page(rows, InboxSelection(), limit=10, cursor=cursor, as_of=NOW)
            assert page.total == count and page.counts["all"] == count
            seen.extend(row.review_id for row in page.rows)
            if not page.next_cursor:
                break
            assert len(page.rows) == 10
            cursor = decode_cursor(page.next_cursor)
        assert sorted(seen) == sorted(row.review_id for row in rows) and len(seen) == len(set(seen))
        assert seen == [row.review_id for row in sorted(rows, key=lambda row: row.sort_key)]
    "case: test_select_page_counts_every_view_and_filters_by_selection"
    rows = _rows(10)
    page = select_page(rows, InboxSelection(view="to_review"), limit=3, cursor=None, as_of=NOW)
    assert page.counts["all"] == 10 and page.counts["to_review"] == 5 and (page.total == 5)
    assert set(page.counts) == {"all", *INBOX_VIEWS}
    searched = select_page(rows, InboxSelection(query="title-00"), limit=50, cursor=None, as_of=NOW)
    assert searched.total == 10 and searched.next_cursor == ""
    none = select_page(rows, InboxSelection(repos=("\x00none",)), limit=50, cursor=None, as_of=NOW)
    assert none.total == 0 and none.counts["all"] == 0
    "case: test_narrow_intersects_and_never_widens"
    base = InboxSelection(view="to_review", repos=("a", "b"))
    assert narrow(base, InboxSelection()) == base
    assert narrow(InboxSelection(), base) == base
    both = narrow(base, InboxSelection(view="to_review", repos=("b", "c"), query="x"))
    assert both.repos == ("b",) and both.view == "to_review" and (both.query == "x")
    disjoint = narrow(base, InboxSelection(view="ready", repos=("c",)))
    assert disjoint.view == "\x00none" and disjoint.repos == ("\x00none",)
    shown = selection_view(disjoint)
    assert shown["view"] == "none" and shown["repos"] == [] and (shown["matches_nothing"] is True)
    assert selection_view(base)["matches_nothing"] is False


def test_inbox_saved_filter_contract() -> None:
    """case: test_saved_filter_definitions_are_validated_strictly"""
    for definition, field in [
        ([], "definition"),
        ({"colour": "red"}, "colour"),
        ({"view": "everything"}, "view"),
        ({"status": "deleted"}, "status"),
        ({"repos": "repo"}, "repos"),
        ({"repos": ["../x"]}, "repos"),
        ({"repos": ["r"] * 51}, "repos"),
        ({"teams": ["\x00"]}, "teams"),
        ({"teams": ["t" * 201]}, "teams"),
        ({"workflow_states": ["merged"]}, "workflow_states"),
        ({"priorities": ["p0"]}, "priorities"),
        ({"query": "q" * 257}, "query"),
        ({"query": 5}, "query"),
    ]:
        with pytest.raises(SavedFilterInvalid) as caught:
            validate_selection(definition)
        assert caught.value.field == field
    "case: test_saved_filter_documents_and_request_parameters"
    name, selection = validate_document({"name": " Team   queue ", "definition": {"teams": ["Platform", "Platform"]}})
    assert name == "Team queue" and selection.teams == ("Platform",)
    with pytest.raises(SavedFilterInvalid) as caught:
        validate_document({"name": "x", "definition": {"teams": ["Platform"]}}, teams_supported=False)
    assert caught.value.field == "teams"
    for document, field in (({"name": "x", "owner": "y"}, "owner"), ({"name": "\x07"}, "name"), ([], "body")):
        with pytest.raises(SavedFilterInvalid) as caught:
            validate_document(document)
        assert caught.value.field == field
    params = {"view": ["ready"], "repo": ["a", " ", "b"], "workflow": ["closed"], "q": ["  Fix "]}
    parsed = request_selection(lambda key: params.get(key, []), teams_supported=False)
    assert parsed == InboxSelection(view="ready", repos=("a", "b"), workflow_states=("closed",), query="fix")
    with pytest.raises(SavedFilterInvalid) as caught:
        request_selection(lambda key: {"workflow": ["merged"]}.get(key, []), teams_supported=True)
    assert caught.value.field == "workflow"
    with pytest.raises(InboxQueryError):
        request_selection(lambda key: {"q": ["x" * 257]}.get(key, []), teams_supported=True)
    assert wants_view_mode({"view": "all"}) and wants_view_mode({"filter": ""})
    assert not wants_view_mode({"status": "open", "q": "x", "limit": "5", "bucket": "all"})


def _payload(**overrides: Any) -> dict[str, Any]:
    readiness = {
        "ready": True,
        "conditions": [
            {"id": "latest-snapshot-reviewed", "owner": "reviewer", "category": "human_review", "state": "pass"},
            {"id": "required-check:ci", "owner": "scm", "category": "scm", "state": "pass"},
        ],
        "evaluated_at": _iso(NOW - timedelta(minutes=5)),
        "valid_until": "",
        **overrides,
    }
    return readiness_summary_payload(readiness, review_id="rev-1", revision_id="rrv-1")


def _rows(count: int) -> list[InboxRow]:
    return [
        InboxRow(
            repo_id="repo",
            review_id=f"r-{index:03d}",
            status="open",
            views=("to_review",) if index % 2 else (),
            workflow_state="needs_review",
            priority="normal",
            sort_key=(-index % 7, "~", "repo", f"r-{index:03d}"),
            haystack=f"title-{index:03d}",
        )
        for index in range(count)
    ]


def test_sqlite_saved_filters_are_per_user_bounded_and_closed(tmp_path: Path) -> None:
    store = SqliteSavedFilterStore(lambda org: tmp_path / org / "filters.db")
    mine = store.create("org-a", "alice", "Mine", InboxSelection(view="authored_by_me"))
    assert store.get("org-a", "alice", mine.id) == mine
    assert store.get("org-a", "bob", mine.id) is None and store.list("org-b", "alice") == ()
    assert store.update("org-a", "bob", mine.id, "Stolen", InboxSelection()) is None
    assert store.delete("org-a", "bob", mine.id) is False
    with pytest.raises(SavedFilterConflict):
        store.create("org-a", "alice", "Mine", InboxSelection())
    store.create("org-a", "bob", "Mine", InboxSelection())
    other = store.create("org-a", "alice", "Other", InboxSelection())
    with pytest.raises(SavedFilterConflict):
        store.update("org-a", "alice", other.id, "Mine", InboxSelection())
    renamed = store.update("org-a", "alice", other.id, "Renamed", InboxSelection(status="open"))
    assert renamed is not None and renamed.created_at == other.created_at and (renamed.selection.status == "open")
    for index in range(MAX_SAVED_FILTERS - 2):
        store.create("org-a", "alice", f"f-{index:02d}", InboxSelection())
    with pytest.raises(SavedFilterLimit):
        store.create("org-a", "alice", "one more", InboxSelection())
    assert len(store.list("org-a", "alice")) == MAX_SAVED_FILTERS
    with sqlite3.connect(tmp_path / "org-a" / "filters.db") as conn:
        conn.execute("UPDATE review_saved_filters SET definition_json = ? WHERE id = ?", ('{"view": "gone"}', mine.id))
    corrupted = store.get("org-a", "alice", mine.id)
    assert corrupted is not None and corrupted.selection.view == "\x00none"
    store.create("org-b", "alice", "Elsewhere", InboxSelection())
    assert store.delete_principal("org-a", "alice") == MAX_SAVED_FILTERS
    assert store.list("org-a", "alice") == () and len(store.list("org-a", "bob")) == 1
    assert len(store.list("org-b", "alice")) == 1
    assert store.forget_org("org-a") == 1 and store.list("org-a", "bob") == ()
    assert len(store.list("org-b", "alice")) == 1
