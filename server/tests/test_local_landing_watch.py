"""The running local server keeps armed GitHub landing intents moving."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_server_core.local_reviews import LocalReviewRepository

from lemoncrow.pro.capabilities.review.landing import landing_relationship
from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship, ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.sources import github as github_source
from lemoncrow.pro.capabilities.review.store import new_revision_id, new_session_id

HEAD = "b" * 40


class _MergeableGitHub:
    """Every pull request is open, green and at ``HEAD``; records each ``gh`` call."""

    def __init__(self, *, failing: frozenset[str] = frozenset()) -> None:
        self.calls: list[list[str]] = []
        self.merged: set[str] = set()
        self.failing = failing

    def __call__(self, args: Sequence[str], stdin: str | None = None, **_: Any) -> str:
        call = list(args)
        self.calls.append(call)
        if call[:2] == ["pr", "merge"]:
            self.merged.add(call[2])
            return ""
        if call[:2] == ["api", "graphql"]:
            number = next(item.split("=", 1)[1] for item in call if item.startswith("number="))
            if number in self.failing:
                raise RuntimeError(f"unexpected crash for #{number}")
            pull = {
                "id": f"PR_{number}",
                "state": "MERGED" if number in self.merged else "OPEN",
                "isDraft": False,
                "headRefOid": HEAD,
                "mergeStateStatus": "CLEAN",
                "isInMergeQueue": False,
                "autoMergeRequest": None,
                "url": f"https://github.com/acme/widgets/pull/{number}",
            }
            repository = {
                "pullRequest": pull,
                "viewerDefaultMergeMethod": "MERGE",
                "mergeCommitAllowed": True,
                "squashMergeAllowed": False,
                "rebaseMergeAllowed": False,
            }
            return json.dumps({"data": {"repository": repository}})
        raise AssertionError(f"unexpected gh call: {call}")

    def merged_numbers(self) -> list[str]:
        return [call[2] for call in self.calls if call[:2] == ["pr", "merge"]]


def _change(repository: LocalReviewRepository, repo_id: str, source_ref: str, *, landing: str) -> ReviewSession:
    store = repository.store(repo_id)
    session = store.create_session(
        ReviewSession(
            id=new_session_id(),
            subject_type="pull_request",
            repo_root=f"/repo/{repo_id}",
            range_mode="commit_range",
            source_ref=source_ref,
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
            head_sha=HEAD,
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
            external_id=source_ref,
            title="Merge when ready",
            status=landing,
            source="human",
            metadata_json=json.dumps({"revision_id": revision.id, "tree_fingerprint": "tree-1"}),
        )
    )
    return session


def _status(repository: LocalReviewRepository, repo_id: str, session: ReviewSession) -> str:
    landing = landing_relationship(repository.store(repo_id), session.id)
    assert landing is not None
    return landing.status


def test_the_watch_merges_only_armed_github_intents_across_repositories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github = _MergeableGitHub()
    monkeypatch.setattr(github_source, "run_gh", github)
    repository = LocalReviewRepository(tmp_path / "reviews")
    armed = _change(repository, "repo-a", "github:acme/widgets#7", landing="armed")
    cancelled = _change(repository, "repo-a", "github:acme/widgets#8", landing="cancelled")
    elsewhere = _change(repository, "repo-b", "github:acme/widgets#9", landing="armed")
    local_only = _change(repository, "repo-b", "named:refs/heads/main...refs/heads/topic", landing="armed")

    repository.reconcile_landings()

    assert sorted(github.merged_numbers()) == ["7", "9"]
    assert _status(repository, "repo-a", armed) == "merged"
    assert _status(repository, "repo-b", elsewhere) == "merged"
    assert _status(repository, "repo-a", cancelled) == "cancelled"
    assert _status(repository, "repo-b", local_only) == "armed"

    github.calls.clear()
    repository.reconcile_landings()

    assert github.calls == []  # settled intents are never polled again


def test_one_broken_intent_never_stops_the_watch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    github = _MergeableGitHub(failing=frozenset({"7"}))
    monkeypatch.setattr(github_source, "run_gh", github)
    repository = LocalReviewRepository(tmp_path / "reviews")
    broken = _change(repository, "repo-a", "github:acme/widgets#7", landing="armed")
    healthy = _change(repository, "repo-a", "github:acme/widgets#8", landing="armed")

    repository.reconcile_landings()

    assert github.merged_numbers() == ["8"]
    assert _status(repository, "repo-a", broken) == "armed"
    assert _status(repository, "repo-a", healthy) == "merged"


def test_the_local_server_maintenance_pass_runs_the_landing_watch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow_server_core.bootstrap import build_local_server
    from lemoncrow_server_core.config import LocalServerConfig
    from lemoncrow_server_core.local_maintenance import LocalMaintenance

    watched: list[Path] = []
    monkeypatch.setattr(LocalReviewRepository, "reconcile_landings", lambda self: watched.append(self.root))
    monkeypatch.setattr(LocalMaintenance, "_maybe_import_sessions", lambda self: None)
    _server, state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=False)
    )
    try:
        state.maintenance.run_once()
    finally:
        state.index_worker.close()

    assert watched == [state.review_repository.root]
