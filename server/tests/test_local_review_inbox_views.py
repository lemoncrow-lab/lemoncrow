"""P9-INBOX over the local server: views, workflow, attention, paging and the local user's saved filters."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Awaitable, Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from aiohttp import ClientSession
from lemoncrow_server_core import local_review_service
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

from lemoncrow.core.foundation.sqlite_base import SqliteTableStore
from lemoncrow.pro.capabilities.review.models import SCHEMA_VERSION
from lemoncrow.pro.capabilities.review.session_models import (
    Annotation,
    AnnotationAnchor,
    HostedAttachment,
    ReviewMark,
    ReviewRevision,
    ReviewSession,
    ReviewUnit,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore, utc_now
from lemoncrow.pro.capabilities.review.workflow_state import persist_readiness_summary

REPO = "repo_0123456789abcdef0123456789abcdef"
REPO_B = "repo_fedcba9876543210fedcba9876543210"
REVIEWER = "local"
FILTERS = "/api/review-inbox/filters"


class _Local:
    def __init__(self, tmp_path: Path) -> None:
        self.server, self.state = build_local_server(
            LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=False)
        )
        self.url = ""
        self.client: ClientSession | None = None

    def store(self, repo_id: str = REPO) -> ReviewStore:
        store: ReviewStore = self.state.review_repository.store(repo_id)
        return store

    async def get(self, path: str, **params: Any) -> Any:
        assert self.client is not None
        return await self.client.get(f"{self.url}{path}", params=params)

    async def send(self, method: str, path: str, document: Any = None) -> Any:
        assert self.client is not None
        return await self.client.request(method, f"{self.url}{path}", json=document, headers={"Origin": self.url})

    async def inbox(self, **params: Any) -> dict[str, Any]:
        return await _ok(await self.get("/v1/reviews", **{"view": "all", **params}))


def _run(tmp_path: Path, body: Callable[[_Local], Awaitable[None]]) -> None:
    async def exercise() -> None:
        local = _Local(tmp_path)
        local.url = await local.server.start()
        try:
            async with ClientSession() as client:
                local.client = client
                await body(local)
        finally:
            await local.server.stop()

    asyncio.run(exercise())


async def _ok(response: Any, status: int = 200) -> dict[str, Any]:
    assert response.status == status, await response.text()
    body: dict[str, Any] = await response.json()
    return body


async def _refused(response: Any, status: int, field_name: str = "") -> dict[str, Any]:
    assert response.status == status, await response.text()
    error: dict[str, Any] = (await response.json())["error"]
    if field_name:
        assert error["details"]["field"] == field_name, error
    return error


# -- fixtures crafted through the durable store --------------------------------


def _review(
    store: ReviewStore,
    title: str,
    *,
    actor: str = "agent",
    units: int = 1,
    status: str = "open",
    repo_root: str = "/srv/local-inbox",
) -> tuple[str, str]:
    session = store.create_session(
        ReviewSession(
            id="",
            subject_type="local_change",
            repo_root=repo_root,
            range_mode="working_tree",
            title=title,
            actor_type=actor,  # type: ignore[arg-type]
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id="",
            review_id=session.id,
            revision_number=0,
            range_mode="working_tree",
            tree_fingerprint=f"tree-{title}",
            packet_schema_version=SCHEMA_VERSION,
        ),
        [
            ReviewUnit(
                revision_id="",
                unit_key=f"file:{title}-{index}.py",
                kind="file",
                path=f"{title}-{index}.py",
                content_fingerprint=f"fp-{title}-{index}",
                fingerprint_method="blob_sha256",
            )
            for index in range(units)
        ],
    )
    store.set_current_revision(session.id, revision.id)
    if status != "open":
        store.update_session(session.id, status=status)
    return session.id, revision.id


def _review_everything(store: ReviewStore, review_id: str, revision_id: str) -> None:
    for unit in store.list_units(revision_id):
        store.set_mark(
            ReviewMark(
                review_id=review_id,
                unit_key=unit.unit_key,
                state="reviewed",
                reviewed_revision_id=revision_id,
                content_fingerprint=unit.content_fingerprint,
                reviewer_id=REVIEWER,
            )
        )


def _resnapshot(store: ReviewStore, review_id: str, title: str, *, changed: int, units: int) -> str:
    """A second Snapshot of ``_review``'s units whose first ``changed`` units have new content."""

    revision = store.add_revision(
        ReviewRevision(
            id="",
            review_id=review_id,
            revision_number=0,  # the store numbers it
            range_mode="working_tree",
            tree_fingerprint=f"tree-{title}-1",
            packet_schema_version=SCHEMA_VERSION,
        ),
        [
            ReviewUnit(
                revision_id="",
                unit_key=f"file:{title}-{index}.py",
                kind="file",
                path=f"{title}-{index}.py",
                content_fingerprint=f"fp-{title}-{index}" + ("-changed" if index < changed else ""),
                fingerprint_method="blob_sha256",
            )
            for index in range(units)
        ],
    )
    store.set_current_revision(review_id, revision.id)
    return revision.id


_WORKFLOW_COUNTS = ("target_count", "reviewed", "unreviewed", "changed_since_review", "unknown", "later")


def _comment(store: ReviewStore, review_id: str, revision_id: str) -> None:
    store.add_annotation(
        Annotation(
            id="",
            review_id=review_id,
            revision_id=revision_id,
            anchor=AnnotationAnchor(path="a.py", side="new", start_line=1, end_line=1),
            body="secret comment body",
        )
    )


def _readiness(
    store: ReviewStore,
    review_id: str,
    revision_id: str,
    *,
    ready: bool = True,
    checks: str = "pass",
    reviewed: bool = True,
    evaluator: str = REVIEWER,
) -> None:
    conditions = [
        {
            "id": "latest-snapshot-reviewed",
            "owner": "reviewer",
            "category": "human_review",
            "state": "pass" if reviewed else "pending",
        },
        {"id": "required-check:ci", "owner": "scm", "category": "scm", "state": checks},
    ]
    persist_readiness_summary(
        store,
        review_id=review_id,
        revision_id=revision_id,
        readiness={
            "ready": ready,
            "conditions": conditions,
            "conditions_hash": "fixture",
            "policy_generation": {"digest": "generation-1"},
            "evaluated_at": utc_now(),
            "valid_until": "",
        },
        evaluator=evaluator,
    )


def _row(body: Mapping[str, Any], review_id: str) -> dict[str, Any]:
    rows = [row for row in body["reviews"] if row["id"] == review_id]
    assert len(rows) == 1, (review_id, [row["id"] for row in body["reviews"]])
    row: dict[str, Any] = rows[0]
    return row


ALL_VIEWS = (
    "all",
    "to_review",
    "assigned_to_my_teams",
    "authored_by_me",
    "participated",
    "waiting_on_me",
    "ready",
    "blocked",
)


# -- views, workflow, distinct fields ---------------------------------------------


def test_each_local_view_returns_exactly_the_right_reviews(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        store = local.store()
        ids: dict[str, str] = {}
        ids["pending"], _ = _review(store, "pending")
        ids["mine"], _ = _review(store, "mine", actor="human")
        ids["reviewed"], reviewed_rev = _review(store, "reviewed")
        _review_everything(store, ids["reviewed"], reviewed_rev)
        ids["ready"], ready_rev = _review(store, "ready")
        _review_everything(store, ids["ready"], ready_rev)
        ids["checks"], checks_rev = _review(store, "checks")
        _review_everything(store, ids["checks"], checks_rev)
        ids["finished"], _ = _review(store, "finished", status="finished")
        _readiness(store, ids["ready"], ready_rev)
        _readiness(store, ids["checks"], checks_rev, ready=False, checks="pending")
        names = {review_id: name for name, review_id in ids.items()}

        expected = {
            "all": set(ids),
            "to_review": {"pending", "mine"},
            "assigned_to_my_teams": set(),
            "authored_by_me": {"mine"},
            "participated": {"reviewed", "ready", "checks"},
            "waiting_on_me": {"pending", "mine", "reviewed"},
            "ready": {"ready"},
            "blocked": {"checks"},
        }
        for view in ALL_VIEWS:
            page = await local.inbox(view=view)
            assert {names[row["id"]] for row in page["reviews"]} == expected[view], view
            assert page["counts"] == {key: len(value) for key, value in expected.items()}, view
            assert page["total"] == len(expected[view]) and page["next_cursor"] == ""
            assert page["mode"] == "views" and page["teams_supported"] is False
        everything = await local.inbox()
        states = {names[row["id"]]: row["workflow"]["state"] for row in everything["reviews"]}
        assert states == {
            "pending": "needs_review",
            "mine": "needs_review",
            "reviewed": "needs_decision",
            "ready": "ready_to_merge",
            "checks": "waiting_on_checks",
            "finished": "closed",
        }
        # The legacy list row is still there: progress counts, title, location.
        pending = _row(everything, ids["pending"])
        assert pending["progress"]["unreviewed"] == 1 and pending["title"] and pending["ref"].startswith("r/")
        # Without a view parameter the legacy list answers unchanged.
        legacy = await _ok(await local.get("/v1/reviews"))
        assert "mode" not in legacy and len(legacy["reviews"]) == len(ids)

    _run(tmp_path, body)


def test_an_unreviewed_local_review_without_findings_is_needs_review_never_ready(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        store = local.store()
        quiet, quiet_rev = _review(store, "quiet", units=2)
        empty, empty_rev = _review(store, "empty", units=0)
        # Even a record that claims ready cannot stand in for review that never happened.
        _readiness(store, quiet, quiet_rev, ready=True, reviewed=False)
        _readiness(store, empty, empty_rev, ready=True, reviewed=False)
        page = await local.inbox()
        for review_id in (quiet, empty):
            row = _row(page, review_id)
            assert row["workflow"]["state"] == "needs_review", row["workflow"]
            assert "ready" not in row["views"] and "to_review" in row["views"]
            assert row["axes"]["your_review"]["state"] == "not_started"
        assert (await local.inbox(view="ready"))["reviews"] == []

    _run(tmp_path, body)


def test_a_review_with_a_part_left_for_later_is_still_needing_review_never_complete(tmp_path: Path) -> None:
    """P0-4: `later` is not judged. Setting a part aside must not turn "needs review" into "needs decision"."""

    async def body(local: _Local) -> None:
        store = local.store()
        aside, aside_rev = _review(store, "aside", units=2)
        done, done_rev = _review(store, "done", units=2)
        _review_everything(store, done, done_rev)
        first, second = store.list_units(aside_rev)
        for unit, state in ((first, "reviewed"), (second, "later")):
            store.set_mark(
                ReviewMark(
                    review_id=aside,
                    unit_key=unit.unit_key,
                    state=state,  # type: ignore[arg-type]
                    reviewed_revision_id=aside_rev,
                    content_fingerprint=unit.content_fingerprint,
                    reviewer_id=REVIEWER,
                )
            )
        page = await local.inbox()
        row = _row(page, aside)
        assert row["progress"]["later"] == 1 and row["progress"]["reviewed"] == 1
        assert row["workflow"]["state"] == "needs_review", row["workflow"]
        assert _row(page, done)["workflow"]["state"] == "needs_decision"
        # The repository-level actionable count sees it too.
        assert row["axes"]["your_review"]["state"] != "complete"

    _run(tmp_path, body)


def test_a_part_left_for_later_keeps_a_review_out_of_the_waiting_buckets(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The handoff fields treat a review with only `later` parts left as still the reviewer's turn."""
    from lemoncrow.pro.capabilities.review import handoff

    store = ReviewStore(tmp_path / "store")
    session_id, _revision = _review(store, "aside")
    session = store.get_session(session_id)
    assert session is not None
    items = list(store.review_list_inputs([session]).items())
    seen: list[int] = []

    def capture(_handoff: Any, *, status: str, actionable_targets: int) -> dict[str, Any]:
        seen.append(actionable_targets)
        return {}

    monkeypatch.setattr(handoff, "handoff_row_fields", capture)
    key = ("repo", session_id)  # progress is keyed (repo_id, review_id)
    base = {"unreviewed": 0, "changed_since_review": 0, "unknown": 0}
    for counts in ({**base, "later": 2}, {**base}, {**base, "unreviewed": 1}):
        local_review_service._review_handoff(store, [(session, items[0][1])], {key: counts}, "repo")
    assert seen == [2, 0, 1]


def test_an_approved_revision_leaves_came_back_to_you(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Notes came back on Revision 2 and the reviewer approved it: it is no longer back with them."""

    from lemoncrow_server_core import local_inbox

    from lemoncrow.pro.capabilities.review import handoff

    real = handoff.review_handoff

    def returned(*args: Any, **kwargs: Any) -> handoff.ReviewHandoff:
        return handoff.ReviewHandoff(**{**real(*args, **kwargs).__dict__, "returned": 1})

    monkeypatch.setattr(local_inbox, "review_handoff", returned)
    monkeypatch.setattr(handoff, "review_handoff", returned)

    async def body(local: _Local) -> None:
        store = local.store()
        back, _first = _review(store, "back")
        approved, _old = _review(store, "approved")
        current = _resnapshot(store, approved, "approved", changed=1, units=1)
        store.record_outcome(approved, current, REVIEWER, outcome="lgtm")
        page = await local.inbox()
        kinds = {row["id"]: [reason["kind"] for reason in row["next_action"]["my_reasons"]] for row in page["reviews"]}
        assert "thread_returned" in kinds[back]
        # Approved: neither back with the reviewer nor left to review (landing it is still the one user's decision).
        assert not {"thread_returned", "review_pending"} & set(kinds[approved]), kinds[approved]
        assert _row(page, approved)["axes"]["your_review"]["state"] == "complete"
        assert _row(page, approved)["axes"]["your_review"]["approved"] is True
        assert _row(page, back)["axes"]["your_review"]["approved"] is False
        assert _row(page, approved).get("primary_bucket") != "returned_to_me"
        assert _row(page, back)["primary_bucket"] == "returned_to_me"
        # The plain Reviews list (the legacy rows) agrees.
        legacy = await _ok(await local.get("/v1/reviews"))
        assert _row(legacy, approved).get("primary_bucket") != "returned_to_me"
        assert _row(legacy, back)["primary_bucket"] == "returned_to_me"

    _run(tmp_path, body)


def test_a_stale_or_foreign_local_readiness_record_never_makes_a_review_ready(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        store = local.store()
        stale, stale_rev = _review(store, "stale")
        _review_everything(store, stale, stale_rev)
        _readiness(store, stale, stale_rev)
        _comment(store, stale, stale_rev)  # a durable fact after the evaluation
        foreign, foreign_rev = _review(store, "foreign")
        _review_everything(store, foreign, foreign_rev)
        _readiness(store, foreign, foreign_rev, evaluator="someone-else")
        page = await local.inbox()
        assert _row(page, stale)["readiness"]["state"] == "stale"
        assert _row(page, foreign)["readiness"]["state"] == "unknown"
        for review_id in (stale, foreign):
            assert _row(page, review_id)["workflow"]["state"] != "ready_to_merge"
            assert _row(page, review_id)["axes"]["scm"]["state"] == "not_connected"

    _run(tmp_path, body)


def test_local_priority_workflow_owner_and_readiness_are_distinct_fields(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        store = local.store()
        review_id, revision_id = _review(store, "fields", units=2)
        before = _row(await local.inbox(), review_id)
        assert before["workflow"]["state"] == "needs_review"
        assert before["next_action"]["for_me"] is True
        assert before["readiness"]["state"] == "unknown"
        assert before["priority"]["level"] == "normal"
        _review_everything(store, review_id, revision_id)
        _readiness(store, review_id, revision_id, ready=False, checks="fail")
        after = _row(await local.inbox(), review_id)
        # Review complete with a failing check: workflow moved, priority did not.
        assert after["workflow"]["state"] == "waiting_on_author"
        assert after["readiness"]["state"] == "not_ready"
        assert after["priority"] == before["priority"]
        assert after["next_action"]["for_me"] is False
        # Honest axes: your review complete, no team review locally, and no merge claim
        # without source control (the failed check is still counted).
        assert after["axes"]["your_review"]["state"] == "complete"
        assert after["axes"]["team_review"]["state"] == "not_applicable"
        assert after["axes"]["scm"]["state"] == "not_connected" and after["axes"]["scm"]["failed_checks"] == 1

    _run(tmp_path, body)


def test_local_review_status_trust_repeats_the_inbox_workflow_and_next_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Trust T2: Review Status and the Inbox give one answer to why a Review waits and who acts next."""

    monkeypatch.setenv("LEMONCROW_REVIEW_TRUST", "on")
    derived: list[str] = []
    derive = local_review_service.LocalReviewService._review_progress

    def counting(self: Any, store: ReviewStore, items: Any, *args: Any, **kwargs: Any) -> Any:
        derived.extend(str(session.id) for session, _inputs in items)
        return derive(self, store, items, *args, **kwargs)

    monkeypatch.setattr(local_review_service.LocalReviewService, "_review_progress", counting)

    async def body(local: _Local) -> None:
        store = local.store()
        here = f"local:{REPO}"  # the Reader opens only this repository's Reviews
        ids: dict[str, str] = {}
        ids["pending"], _ = _review(store, "pending", units=2, repo_root=here)
        ids["mine"], _ = _review(store, "mine", actor="human", repo_root=here)
        ids["reviewed"], reviewed_rev = _review(store, "reviewed", repo_root=here)
        _review_everything(store, ids["reviewed"], reviewed_rev)
        ids["commented"], commented_rev = _review(store, "commented", repo_root=here)
        _review_everything(store, ids["commented"], commented_rev)
        _comment(store, ids["commented"], commented_rev)
        ids["finished"], _ = _review(store, "finished", status="finished", repo_root=here)
        ids["partial"], partial_rev = _review(store, "partial", units=3, repo_root=here)
        [first, *_rest] = store.list_units(partial_rev)
        store.set_mark(
            ReviewMark(
                review_id=ids["partial"],
                unit_key=first.unit_key,
                state="reviewed",
                reviewed_revision_id=partial_rev,
                content_fingerprint=first.content_fingerprint,
                reviewer_id=REVIEWER,
            )
        )
        ids["changed"], changed_rev = _review(store, "changed", units=2, repo_root=here)
        _review_everything(store, ids["changed"], changed_rev)
        _resnapshot(store, ids["changed"], "changed", changed=1, units=2)
        statuses: dict[str, dict[str, Any]] = {}
        progress: dict[str, dict[str, Any]] = {}
        with caplog.at_level("INFO", logger="lemoncrow.review.trust"):
            for name, review_id in ids.items():
                before = len(derived)
                statuses[name] = await _ok(await local.get(f"/api/reviews/{review_id}/readiness"))
                # The workflow read reuses this Status read's counts instead of deriving the targets again.
                assert derived[before:] == [], name
                progress[name] = (await _ok(await local.get(f"/api/reviews/{review_id}/targets")))["progress"]
        page = await local.inbox()
        seen = set()
        for name, review_id in ids.items():
            trust = statuses[name]["trust"]
            row = _row(page, review_id)
            # Status hands the trust section's workflow read its own target counts; the Inbox list
            # derives its own. The counts the workflow reads must agree, Review by Review.
            assert {key: row["progress"][key] for key in _WORKFLOW_COUNTS} == {
                key: progress[name][key] for key in _WORKFLOW_COUNTS
            }, name
            assert trust["summary"]["source"] == "workflow", name
            assert {key: trust["summary"][key] for key in ("state", "label", "rule", "explanation")} == row[
                "workflow"
            ], name
            assert trust["next"] == row["next_action"], name
            seen.add(row["workflow"]["state"])
        assert {"needs_review", "closed"} <= seen and len(seen) >= 3, seen
        assert progress["changed"]["changed_since_review"] == 1 and progress["changed"]["reviewed"] == 1
        assert progress["partial"]["reviewed"] == 1 and progress["partial"]["unreviewed"] == 2
        lines = [record.getMessage() for record in caplog.records if record.name == "lemoncrow.review.trust"]
        assert len(lines) == len(ids) and all("mismatches=none" in line for line in lines), lines

    _run(tmp_path, body)


# -- attention, determinism, no writes, bounded reads ------------------------------

_STORE_WRITES = (
    "put_derived_results",
    "set_mark",
    "add_annotation",
    "update_session",
    "set_current_revision",
    "create_session",
    "add_revision",
    "record_activity_event",
    "set_destination",
)
_BODY_LOADS = ("list_units", "list_annotations", "get_revision", "latest_revision", "list_marks", "get_destination")


@contextmanager
def _refuse(monkeypatch: pytest.MonkeyPatch, names: Sequence[str], what: str) -> Iterator[list[str]]:
    calls: list[str] = []
    for name in names:
        if not hasattr(ReviewStore, name):
            continue

        def refused(*_args: Any, _name: str = name, **_kwargs: Any) -> Any:
            calls.append(_name)
            raise AssertionError(f"{what} during a warm Inbox read: {_name}")

        monkeypatch.setattr(ReviewStore, name, refused)
    yield calls


def test_local_attention_is_deterministic_explained_and_a_warm_read_never_writes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(local: _Local) -> None:
        store = local.store()
        small, _ = _review(store, "small", units=1)
        large, _ = _review(store, "large", units=30)
        twins = [_review(store, f"twin-{index}", units=3)[0] for index in range(3)]
        warm = await local.inbox()  # derives and stores the list's progress/title rows once

        with (
            _refuse(monkeypatch, _STORE_WRITES, "store write") as writes,
            _refuse(monkeypatch, _BODY_LOADS, "Review body load") as loads,
        ):
            packets: list[Any] = []
            monkeypatch.setattr(local_review_service, "read_packet_json", lambda *args: packets.append(args))
            first = await local.inbox()
            second = await local.inbox()
        monkeypatch.undo()
        assert writes == [] and loads == [] and packets == []
        assert [row["id"] for row in first["reviews"]] == [row["id"] for row in second["reviews"]]
        assert [row["attention"] for row in first["reviews"]] == [row["attention"] for row in second["reviews"]]
        # The directory is newest-updated first, independent of attention score.
        order = [row["id"] for row in first["reviews"]]
        assert order == [row["id"] for row in warm["reviews"]]
        assert order == [row["id"] for row in sorted(first["reviews"], key=lambda row: row["updated_at"], reverse=True)]
        assert set(twins) <= set(order)
        assert order.index(large) < order.index(small)
        selected = _row(first, small)["attention"]
        keys = {factor["key"]: factor for factor in selected["factors"]}
        assert keys["ownership"]["detail"] == "1 part left to review"
        assert keys["effort"]["detail"] == "small: 1 part"
        assert selected["score"] == sum(factor["points"] for factor in selected["factors"])

    _run(tmp_path, body)


def test_default_directory_summary_is_body_free_and_safe_to_show_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(local: _Local) -> None:
        review_id, _revision_id = _review(local.store(), "summary")

        def refuse(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("initial directory must not compute full Review projections")

        monkeypatch.setattr(ReviewStore, "review_list_inputs", refuse)
        monkeypatch.setattr(ReviewStore, "list_sessions", refuse)
        monkeypatch.setattr(local_review_service, "read_packet_json", refuse)
        monkeypatch.setattr(local_review_service, "_review_location_metadata", refuse)
        initial = await local.get("/v1/reviews", summary="1", status="open", limit="5")
        assert "review_summary;dur=" in initial.headers.get("Server-Timing", "")
        response = await _ok(initial)
        assert response["summary"] is True
        assert [row["id"] for row in response["reviews"]] == [review_id]
        assert response["reviews"][0]["ref"] == f"r/{review_id}"
        assert "readiness" not in response["reviews"][0]
        rejected = await local.get("/v1/reviews", summary="1", view="ready")
        assert rejected.status == 400
        monkeypatch.undo()
        enriched = await local.get("/v1/reviews", view="all", status="open", limit="5")
        assert "inbox_score;dur=" in enriched.headers.get("Server-Timing", "")
        assert "inbox_hydrate;dur=" in enriched.headers.get("Server-Timing", "")

    _run(tmp_path, body)


def test_the_local_queue_reads_a_bounded_number_of_statements(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(local: _Local) -> None:
        for index in range(6):
            _review(local.store(REPO), f"small-{index}")
        for index in range(30):
            _review(local.store(REPO_B), f"large-{index}")
        await local.inbox(limit="5", repo=REPO)
        await local.inbox(limit="5", repo=REPO_B)
        statements: list[str] = []
        original = SqliteTableStore._connect

        def counting(self: SqliteTableStore) -> sqlite3.Connection:
            conn = original(self)
            conn.set_trace_callback(statements.append)
            return conn

        monkeypatch.setattr(SqliteTableStore, "_connect", counting)

        async def selects(repo_id: str) -> int:
            statements.clear()
            page = await local.inbox(limit="5", repo=repo_id)
            assert len(page["reviews"]) == 5 and page["next_cursor"]
            return sum(1 for text in statements if text.lstrip().upper().startswith("SELECT"))

        small = await selects(REPO)
        large = await selects(REPO_B)
        monkeypatch.undo()
        # The same statements for 6 and 30 Reviews: no per-row fan-out.
        assert small > 0 and small == large, (small, large)

    _run(tmp_path, body)


def test_local_paging_is_bounded_at_and_past_the_limit_without_silent_truncation(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        store = local.store()
        for index in range(101):
            _review(store, f"page-{index:03d}")
        first = await local.inbox(limit="100")
        assert len(first["reviews"]) == 100 and first["total"] == 101 and first["counts"]["all"] == 101
        assert first["next_cursor"]
        second = await local.inbox(limit="100", cursor=first["next_cursor"])
        assert len(second["reviews"]) == 1 and second["next_cursor"] == "" and second["total"] == 101
        assert len({row["id"] for row in first["reviews"] + second["reviews"]}) == 101
        exact = await local.inbox(limit="10", q="page-00")
        assert len(exact["reviews"]) == 10 and exact["next_cursor"] == "" and exact["total"] == 10

        bad: list[tuple[dict[str, str], str]] = [
            ({"limit": "101"}, "limit"),
            ({"limit": "0"}, "limit"),
            ({"limit": "ten"}, "limit"),
            ({"cursor": "not-a-cursor"}, "cursor"),
            ({"cursor": "A" * 1025}, "cursor"),
            ({"view": "everything"}, "view"),
            ({"workflow": "merged"}, "workflow"),
            ({"status": "deleted"}, "status"),
            ({"team": "Platform"}, "team"),
            ({"q": "x" * 257}, "q"),
        ]
        for params, field_name in bad:
            await _refused(await local.get("/v1/reviews", **{"view": "all", **params}), 400, field_name)

    _run(tmp_path, body)


# -- saved filters ---------------------------------------------------------------


def test_local_saved_filters_crud_validation_bounds_and_scope(tmp_path: Path) -> None:
    async def body(local: _Local) -> None:
        mine, _ = _review(local.store(), "mine", actor="human")
        _review(local.store(), "theirs")
        assert await _ok(await local.get(FILTERS)) == {"filters": [], "limit": 50, "teams_supported": False}

        created = await _ok(
            await local.send("POST", FILTERS, {"name": "  My   work ", "definition": {"view": "authored_by_me"}}), 201
        )
        saved = created["filter"]
        assert saved["name"] == "My work" and saved["definition"]["view"] == "authored_by_me"
        applied = await local.inbox(filter=saved["id"])
        assert [row["id"] for row in applied["reviews"]] == [mine] and applied["filter"]["id"] == saved["id"]
        # A filter narrows the request; it never widens it.
        narrowed = await local.inbox(filter=saved["id"], view="blocked")
        assert narrowed["reviews"] == [] and narrowed["selection"]["matches_nothing"] is True

        # A filter naming a repository with no Reviews (or none at all) selects nothing; it is scope, not access.
        elsewhere = await _ok(
            await local.send("POST", FILTERS, {"name": "Elsewhere", "definition": {"repos": [REPO_B]}}), 201
        )
        scoped = await local.inbox(filter=elsewhere["filter"]["id"])
        assert scoped["reviews"] == [] and scoped["counts"]["all"] == 0

        await _refused(await local.send("POST", FILTERS, {"name": "My work", "definition": {}}), 409, "name")
        refusals: list[tuple[Any, str]] = [
            ({"name": "", "definition": {}}, "name"),
            ({"name": "x" * 81, "definition": {}}, "name"),
            ({"name": "Teams", "definition": {"teams": ["Platform"]}}, "teams"),
            ({"name": "Odd", "definition": {"colour": "red"}}, "colour"),
            ({"name": "Odd", "definition": {"workflow_states": ["merged"]}}, "workflow_states"),
            ({"name": "Odd", "definition": {"repos": ["../etc"]}}, "repos"),
            ({"name": "Odd", "definition": {"repos": [REPO] * 51}}, "repos"),
            ({"name": "Odd", "definition": {"query": "q" * 257}}, "query"),
            ({"name": "Odd", "definition": {}, "owner": "someone"}, "owner"),
        ]
        for document, field_name in refusals:
            await _refused(await local.send("POST", FILTERS, document), 400, field_name)

        renamed = await _ok(
            await local.send("PUT", f"{FILTERS}/{saved['id']}", {"name": "Authored", "definition": {"view": "all"}})
        )
        assert renamed["filter"]["name"] == "Authored" and renamed["filter"]["created_at"] == saved["created_at"]
        await _refused(await local.send("PUT", f"{FILTERS}/rsf-missing", {"name": "N", "definition": {}}), 404)
        await _refused(await local.send("PUT", f"{FILTERS}/bad%20id", {"name": "N", "definition": {}}), 404)
        await _refused(await local.get("/v1/reviews", view="all", filter="rsf-missing"), 404, "filter")

        for index in range(48):
            await _ok(await local.send("POST", FILTERS, {"name": f"Filter {index:02d}", "definition": {}}), 201)
        listed = await _ok(await local.get(FILTERS))
        assert len(listed["filters"]) == 50
        over = await _refused(await local.send("POST", FILTERS, {"name": "One more", "definition": {}}), 409)
        assert over["details"] == {"field": "filters", "limit": 50}
        assert len((await _ok(await local.get(FILTERS)))["filters"]) == 50  # refused, never evicted

        await _ok(await local.send("DELETE", f"{FILTERS}/{saved['id']}"))
        await _refused(await local.send("DELETE", f"{FILTERS}/{saved['id']}"), 404)
        assert saved["id"] not in {row["id"] for row in (await _ok(await local.get(FILTERS)))["filters"]}

        # The file lives inside the Review state root, never read as a repository.
        root = local.state.review_repository.root
        assert (root / ".inbox" / "saved_filters.db").is_file()
        assert ".inbox" not in local.state.review_repository.list_repository_ids()
        assert (await local.inbox())["repositories"] == len(local.state.review_repository.list_repository_ids())

    _run(tmp_path, body)


def test_an_attached_review_is_listed_and_routed_by_its_hosted_reference(tmp_path: Path) -> None:
    """G5-SYNC one identity: the inbox names an attached Review only by its hosted r/<id>."""

    hosted_id = "0199aaaabbbbccccddddeeeeffff0000"

    async def body(local: _Local) -> None:
        store = local.store()
        attached_id, _ = _review(store, "attached")
        plain_id, _ = _review(store, "plain")
        store.attach_hosted(
            HostedAttachment(
                review_id=attached_id,
                origin="https://hosted.example",
                hosted_repo_id="hosted-repo",
                hosted_review_id=hosted_id,
                principal_id="alice",
            )
        )
        for page in (await local.inbox(), await _ok(await local.get("/v1/reviews"))):
            # The canonical Change is hosted's (RC5): even ``id``; the local id is provenance only.
            assert [row for row in page["reviews"] if row["id"] == attached_id] == []
            row = _row(page, hosted_id)
            assert (row["short_id"], row["ref"], row["change_ref"], row["review_path"]) == (
                hosted_id,
                f"r/{hosted_id}",
                f"r/{hosted_id}",
                f"/r/{hosted_id}",
            )
            assert row["hosted"] == {
                "reference": f"r/{hosted_id}",
                "origin": "https://hosted.example",
                "url": f"https://hosted.example/r/{hosted_id}",
                "local_review_id": attached_id,
            }
            plain = _row(page, plain_id)
            assert "hosted" not in plain and plain["short_id"] != hosted_id
        # The hosted reference resolves to the attached local Review (what /api/reviews/<ref> routes by).
        routed = local.state.review_repository.get_review(REPO, hosted_id)
        assert routed is not None and routed.id == attached_id
        assert local.state.review_repository.get_review(REPO, f"r/{hosted_id}").id == attached_id
        # A row's id is what the inbox's own actions address: renaming by it reaches the local Review.
        renamed = await _ok(
            await local.send("PUT", f"/api/repos/{REPO}/reviews/{hosted_id}/title", {"title": "Renamed"})
        )
        assert (renamed["id"], renamed["title"]) == (hosted_id, "Renamed")
        assert local.state.review_repository.manual_review_title(REPO, attached_id) == "Renamed"

    _run(tmp_path, body)


def test_the_legacy_review_list_names_an_attached_review_by_its_hosted_change(tmp_path: Path) -> None:
    """The local shell's own ``GET /api/reviews`` (no repository Reader) shows one identity too (RC5)."""

    from aiohttp.test_utils import make_mocked_request
    from lemoncrow_server_core.local_review import LocalReviewSurface

    hosted_id = "0199aaaabbbbccccddddeeeeffff0000"
    surface = LocalReviewSurface(tmp_path / "store", port=45999)
    surface.store.init()
    attached_id, _ = _review(surface.store, "attached")
    plain_id, _ = _review(surface.store, "plain")
    surface.store.attach_hosted(
        HostedAttachment(
            review_id=attached_id,
            origin="https://hosted.example",
            hosted_repo_id="hosted-repo",
            hosted_review_id=hosted_id,
            principal_id="alice",
        )
    )
    request = make_mocked_request("GET", "/api/reviews?status_filter=all", headers={"Host": "127.0.0.1:45999"})
    response = asyncio.run(surface.api_reviews(request))
    assert response.status == 200, response.text  # type: ignore[attr-defined]
    rows = {row["id"]: row for row in json.loads(response.text)["reviews"]}  # type: ignore[attr-defined]
    assert set(rows) == {hosted_id, plain_id}
    assert rows[hosted_id]["ref"] == f"r/{hosted_id}"
    assert rows[hosted_id]["hosted"]["local_review_id"] == attached_id
    assert rows[plain_id]["ref"] == f"r/{plain_id}" and "hosted" not in rows[plain_id]


def test_the_status_progress_is_reused_only_for_the_current_snapshot(tmp_path: Path) -> None:
    """The trust section's Inbox row reads the Status read's own counts only for this Review's current Snapshot."""

    from datetime import UTC, datetime

    from lemoncrow_server_core.local_inbox import local_review_workflow

    from lemoncrow.pro.capabilities.review.readiness import SnapshotProgress

    store = _Local(tmp_path).store()
    review_id, first = _review(store, "guard", units=2)
    current = _resnapshot(store, review_id, "guard", changed=1, units=2)
    session = store.get_session(review_id)
    counts = {"target_count": 2, "reviewed": 0, "unreviewed": 2, "changed_since_review": 0, "unknown": 0}
    derived: list[list[str]] = []

    def progress_of(_store: ReviewStore, items: Sequence[tuple[Any, Any]]) -> dict[str, dict[str, int]]:
        derived.append([str(row.id) for row, _inputs in items])
        return {review_id: counts}

    def workflow(progress: SnapshotProgress | None) -> Any:
        return local_review_workflow(
            store, REPO, session, progress_of=progress_of, as_of=datetime.now(UTC), progress=progress
        )

    expected = workflow(None)
    assert expected is not None and derived == [[review_id]]
    assert workflow(SnapshotProgress(revision_id=current, counts=counts)) == expected
    assert len(derived) == 1  # reused, not derived again
    stale = SnapshotProgress(revision_id=first, counts={**counts, "reviewed": 2, "unreviewed": 0})
    assert workflow(stale) == expected
    assert len(derived) == 2  # another Snapshot's counts are never read
