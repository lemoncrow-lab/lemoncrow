from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.pro.capabilities.review.landing import (
    conditions_digest,
    landing_metadata,
    landing_relationship,
    local_github_landing,
    reconcile_local_github_landing,
    record_landing_state,
)
from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship, ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.sources.github import GitHubCliError
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_revision_id, new_session_id


def _session() -> ReviewSession:
    return ReviewSession(
        id="review-1",
        subject_type="pull_request",
        repo_root="/repo",
        range_mode="commit_range",
        source_ref="github:acme/widgets#7",
    )


def _revision(head: str = "b" * 40) -> ReviewRevision:
    return ReviewRevision(
        id="rr-1",
        review_id="review-1",
        revision_number=1,
        range_mode="commit_range",
        tree_fingerprint="tree-1",
        packet_schema_version=1,
        head_sha=head,
    )


def test_conditions_digest_changes_when_visible_landing_condition_changes() -> None:
    passing = [
        {
            "id": "checks",
            "label": "Checks",
            "state": "pass",
            "detail": "green",
            "owner": "scm",
            "category": "scm",
            "url": "",
        }
    ]
    pending = [{**passing[0], "state": "pending", "detail": "waiting"}]
    assert conditions_digest(passing) == conditions_digest(list(passing))
    assert conditions_digest(passing) != conditions_digest(pending)


class _FakeGitHub:
    """Records every ``gh`` call and answers the landing state query."""

    def __init__(self, **pull_request: Any) -> None:
        self.pull_request: dict[str, Any] = {
            "id": "PR_node_7",
            "state": "OPEN",
            "isDraft": False,
            "headRefOid": "b" * 40,
            "mergeStateStatus": "BLOCKED",
            "isInMergeQueue": False,
            "autoMergeRequest": None,
            "url": "https://github.com/acme/widgets/pull/7",
            **pull_request,
        }
        self.repository: dict[str, Any] = {
            "viewerDefaultMergeMethod": "SQUASH",
            "mergeCommitAllowed": True,
            "squashMergeAllowed": True,
            "rebaseMergeAllowed": False,
        }
        self.merged_state = "MERGED"
        self.calls: list[list[str]] = []
        self.on_state_query: Callable[[], None] | None = None
        self.merge_error = ""

    def run(self, args: Sequence[str], stdin: str | None = None) -> str:
        del stdin
        args = list(args)
        self.calls.append(args)
        text = " ".join(args)
        if args[:2] == ["pr", "merge"] and "--disable-auto" in args:
            self.pull_request["autoMergeRequest"] = None
        elif args[:2] == ["pr", "merge"]:
            self.pull_request["state"] = self.merged_state
            self.pull_request["isInMergeQueue"] = self.merged_state == "OPEN"
            if self.merge_error:
                raise GitHubCliError(self.merge_error)
        elif args[:2] == ["api", "graphql"] and "dequeuePullRequest" in text:
            self.pull_request["isInMergeQueue"] = False
        elif args[:2] == ["api", "graphql"] and "pullRequest(number" in text:
            if self.on_state_query is not None:
                self.on_state_query()
            return json.dumps({"data": {"repository": {"pullRequest": dict(self.pull_request), **self.repository}}})
        return ""

    def merges(self) -> list[list[str]]:
        return [args for args in self.calls if args[:2] == ["pr", "merge"] and "--disable-auto" not in args]


def _use(monkeypatch: pytest.MonkeyPatch, fake: _FakeGitHub) -> _FakeGitHub:
    from lemoncrow.pro.capabilities.review.sources import github as github_source

    monkeypatch.setattr(github_source, "run_gh", fake.run)
    return fake


def test_local_github_landing_never_enables_provider_auto_merge_while_requirements_are_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _use(monkeypatch, _FakeGitHub(mergeStateStatus="BLOCKED"))

    result = local_github_landing(_session(), _revision(), action="arm")

    # GitHub auto-merge follows later pushes by writers; LemonCrow keeps the
    # intent itself and merges only the exact reviewed head once GitHub is ready.
    assert result["state"] == "armed"
    assert "b" * 12 in result["detail"]
    assert fake.merges() == []
    assert not any("--auto" in args for args in fake.calls)


def test_local_github_landing_merges_only_the_reviewed_head_once_mergeable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _use(monkeypatch, _FakeGitHub(mergeStateStatus="CLEAN"))

    result = local_github_landing(_session(), _revision(), action="arm")

    assert result["state"] == "merged"
    [merge] = fake.merges()
    assert merge[merge.index("--match-head-commit") + 1] == "b" * 40
    assert "--squash" in merge  # the repository's/viewer's default method
    assert "--auto" not in merge
    assert "--admin" not in merge


def test_local_github_landing_refuses_moved_head_before_arming(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _use(monkeypatch, _FakeGitHub(headRefOid="c" * 40, mergeStateStatus="CLEAN"))

    result = local_github_landing(_session(), _revision(), action="arm")

    assert result["state"] == "blocked"
    assert "head moved" in result["detail"]
    assert fake.merges() == []


def test_local_github_landing_replaces_existing_auto_merge_before_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _use(
        monkeypatch,
        _FakeGitHub(mergeStateStatus="BLOCKED", autoMergeRequest={"enabledAt": "2026-09-25T00:00:00Z"}),
    )

    result = local_github_landing(_session(), _revision(), action="arm")

    assert result["state"] == "armed"
    assert ["pr", "merge", "7", "--repo", "acme/widgets", "--disable-auto"] in fake.calls
    assert fake.merges() == []


def test_local_github_landing_cancel_disables_auto_merge_and_leaves_the_merge_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _use(
        monkeypatch,
        _FakeGitHub(autoMergeRequest={"enabledAt": "2026-09-25T00:00:00Z"}, isInMergeQueue=True),
    )

    result = local_github_landing(_session(), _revision(), action="cancel")

    assert result["state"] == "cancelled"
    assert ["pr", "merge", "7", "--repo", "acme/widgets", "--disable-auto"] in fake.calls
    dequeue = next(args for args in fake.calls if "dequeuePullRequest" in " ".join(args))
    assert "id=PR_node_7" in dequeue


def _armed_pull_request(tmp_path: Path, *, head: str = "b" * 40) -> tuple[ReviewStore, ReviewSession]:
    store = ReviewStore(tmp_path / "store")
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="pull_request",
            repo_root="/repo",
            range_mode="commit_range",
            title="acme/widgets#7",
            source_ref="github:acme/widgets#7",
        )
    )
    revision = store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="commit_range",
            tree_fingerprint="tree-1",
            packet_schema_version=1,
            head_sha=head,
        ),
        [],
    )
    store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=session.id,
            kind="landing",
            relation="merge_when_ready",
            provider="github",
            external_id="acme/widgets#7",
            url="https://github.com/acme/widgets/pull/7",
            title="Merge when ready",
            status="armed",
            source="human",
            metadata_json=json.dumps({"revision_id": revision.id, "tree_fingerprint": "tree-1"}),
        )
    )
    return store, session


def _landing_status(store: ReviewStore, session: ReviewSession) -> tuple[str, str]:
    landing = landing_relationship(store, session.id)
    assert landing is not None
    return landing.status, str(landing_metadata(landing).get("detail") or "")


def test_landing_watch_marks_a_moved_pull_request_head_stale_and_never_merges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _armed_pull_request(tmp_path)
    fake = _use(monkeypatch, _FakeGitHub(headRefOid="c" * 40, mergeStateStatus="CLEAN"))

    reconcile_local_github_landing(store, session)

    status, detail = _landing_status(store, session)
    assert status == "stale"
    assert "c" * 12 in detail
    assert fake.merges() == []


def test_landing_watch_waits_while_github_requirements_are_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _armed_pull_request(tmp_path)
    fake = _use(monkeypatch, _FakeGitHub(mergeStateStatus="BLOCKED"))

    reconcile_local_github_landing(store, session)

    assert _landing_status(store, session)[0] == "armed"
    assert fake.merges() == []


def test_landing_watch_merges_the_armed_head_once_github_is_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _armed_pull_request(tmp_path)
    fake = _use(monkeypatch, _FakeGitHub(mergeStateStatus="CLEAN"))

    reconcile_local_github_landing(store, session)

    assert _landing_status(store, session)[0] == "merged"
    [merge] = fake.merges()
    assert merge[merge.index("--match-head-commit") + 1] == "b" * 40


def test_landing_watch_records_a_pull_request_closed_without_merging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _armed_pull_request(tmp_path)
    fake = _use(monkeypatch, _FakeGitHub(state="CLOSED"))

    reconcile_local_github_landing(store, session)

    status, detail = _landing_status(store, session)
    assert status == "blocked"
    assert "closed" in detail
    assert fake.merges() == []


def test_landing_watch_never_merges_after_the_semantic_snapshot_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _armed_pull_request(tmp_path)
    store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="commit_range",
            tree_fingerprint="tree-2",
            packet_schema_version=1,
            head_sha="b" * 40,
        ),
        [],
    )
    fake = _use(monkeypatch, _FakeGitHub(mergeStateStatus="CLEAN"))

    reconcile_local_github_landing(store, session)

    assert _landing_status(store, session)[0] == "stale"
    assert fake.merges() == []


def _human_cancels_once(store: ReviewStore, session: ReviewSession) -> Callable[[], None]:
    """The user presses Cancel while the watch is waiting on GitHub."""

    done = False

    def cancel() -> None:
        nonlocal done
        if done:
            return
        done = True
        landing = landing_relationship(store, session.id)
        assert landing is not None
        record_landing_state(
            store, landing, status="cancelled", metadata_updates={"detail": "Landing intent cancelled."}
        )

    return cancel


def test_a_human_cancel_that_races_the_watch_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _armed_pull_request(tmp_path)
    fake = _use(monkeypatch, _FakeGitHub(mergeStateStatus="BLOCKED"))
    fake.on_state_query = _human_cancels_once(store, session)

    reconcile_local_github_landing(store, session)

    assert _landing_status(store, session)[0] == "cancelled"
    assert fake.merges() == []


def test_the_watch_never_merges_after_a_human_cancel_it_raced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store, session = _armed_pull_request(tmp_path)
    fake = _use(monkeypatch, _FakeGitHub(mergeStateStatus="CLEAN"))
    fake.on_state_query = _human_cancels_once(store, session)

    reconcile_local_github_landing(store, session)

    assert fake.merges() == []
    assert _landing_status(store, session)[0] == "cancelled"


def test_cancelling_after_github_merged_reports_the_merge(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _use(monkeypatch, _FakeGitHub(state="MERGED"))

    result = local_github_landing(_session(), _revision(), action="cancel")

    assert result["state"] == "merged"
    assert "b" * 12 in result["detail"]
    assert fake.merges() == []


def test_a_merge_of_the_reviewed_head_that_raced_another_merge_is_recorded_as_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _use(monkeypatch, _FakeGitHub(mergeStateStatus="CLEAN"))
    fake.merge_error = "Pull request acme/widgets#7 was already merged"

    result = local_github_landing(_session(), _revision(), action="arm")

    assert result["state"] == "merged"
    assert len(fake.merges()) == 1


def test_cancelling_never_claims_a_merge_of_an_unreviewed_head(monkeypatch: pytest.MonkeyPatch) -> None:
    _use(monkeypatch, _FakeGitHub(state="MERGED", headRefOid="c" * 40))

    result = local_github_landing(_session(), _revision(), action="cancel")

    assert result["state"] == "blocked"
    assert "c" * 12 in result["detail"] and "b" * 12 in result["detail"]


def _discarded(store: ReviewStore, session: ReviewSession) -> ReviewSession:
    store.update_session(session.id, status="archived")
    archived = store.get_session(session.id)
    assert archived is not None and archived.status == "archived"
    return archived


def test_the_watch_withdraws_a_discarded_changes_landing_and_never_merges_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _armed_pull_request(tmp_path)
    archived = _discarded(store, session)
    fake = _use(monkeypatch, _FakeGitHub(mergeStateStatus="CLEAN", isInMergeQueue=True))

    reconcile_local_github_landing(store, archived)

    status, detail = _landing_status(store, archived)
    assert status == "cancelled"
    assert "discarded" in detail
    assert fake.merges() == []
    # Anything GitHub would still land on its own is withdrawn, too.
    assert any("dequeuePullRequest" in " ".join(args) for args in fake.calls)


def test_the_watch_records_a_merge_github_made_before_the_change_was_discarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _armed_pull_request(tmp_path)
    archived = _discarded(store, session)
    fake = _use(monkeypatch, _FakeGitHub(state="MERGED"))

    reconcile_local_github_landing(store, archived)

    assert _landing_status(store, archived)[0] == "merged"
    assert fake.merges() == []


def _captured_new_semantic_snapshot(store: ReviewStore, session: ReviewSession) -> None:
    store.add_revision(
        ReviewRevision(
            id=new_revision_id(),
            review_id=session.id,
            revision_number=0,
            range_mode="commit_range",
            tree_fingerprint="tree-2",
            packet_schema_version=1,
            head_sha="c" * 40,
        ),
        [],
    )


def test_an_owed_cancel_that_finds_the_reviewed_head_merged_records_the_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _armed_pull_request(tmp_path)
    _captured_new_semantic_snapshot(store, session)
    _use(monkeypatch, _FakeGitHub(state="MERGED"))

    reconcile_local_github_landing(store, session)

    status, detail = _landing_status(store, session)
    assert status == "merged"
    assert "was cancelled" not in detail


def test_an_owed_cancel_that_finds_an_unreviewed_head_merged_says_so(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, session = _armed_pull_request(tmp_path)
    _captured_new_semantic_snapshot(store, session)
    _use(monkeypatch, _FakeGitHub(state="MERGED", headRefOid="c" * 40))

    reconcile_local_github_landing(store, session)

    status, detail = _landing_status(store, session)
    assert status == "blocked"
    assert "c" * 12 in detail and "not the reviewed head" in detail
    assert "was cancelled" not in detail
