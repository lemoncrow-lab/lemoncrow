"""`lc review --pr`: review a GitHub pull request locally, publish back as one review.

GitHub is replaced by :class:`FakeGitHub`, which answers the exact ``gh`` calls
the code makes and keeps the reviews/comments a real PR would, so the tests can
assert what GitHub ends up holding -- including after a retry.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

import pygit2
import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from lemoncrow.gateway.cli import cli
from lemoncrow.pro.capabilities.review.api import make_token_dependency, register_review_api
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.github_publish import (
    PublishCandidate,
    PullRequestFile,
    PullRequestState,
    RemoteReview,
    build_plan,
    hunk_ranges,
)
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.sources import github as github_source
from lemoncrow.pro.capabilities.review.sources.github import (
    GitHubCliError,
    PullRequestRef,
    find_remote,
    parse_pr_source_ref,
    pull_request_ref,
    resolve_pull_request,
)
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore

TOKEN = "test-token"
PORT = 45998
BASE = f"http://127.0.0.1:{PORT}"
PR = PullRequestRef(owner="acme", repo="widgets", number=7)
REPO_ID = 4242
PATCH = "@@ -1,6 +1,6 @@\n line 1\n line 2\n-line 3\n+line three\n line 4\n line 5\n line 6"

_EVENT_STATE = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


class FakeGitHub:
    """The slice of GitHub's REST API that publishing uses, behind `gh api`."""

    def __init__(self, head_sha: str, *, author: str = "alice", viewer: str = "bob") -> None:
        self.head_sha = head_sha
        self.author = author
        self.viewer = viewer
        self.state = "open"
        self.files: list[dict[str, Any]] = [{"filename": "src/app.py", "patch": PATCH}]
        self.reviews: list[dict[str, Any]] = []
        self.comments: dict[int, list[dict[str, Any]]] = {}
        self.posts: list[dict[str, Any]] = []
        self.fail_post = ""
        self.lose_response = False
        self.repository_id: Any = REPO_ID
        self.html_url: Any = PR.url

    def __call__(self, args: Any, stdin: str | None = None, *, cwd: Path | None = None) -> str:
        call = list(args)
        api = f"repos/{PR.slug}/pulls/{PR.number}"
        if call == ["api", "user"]:
            return json.dumps({"login": self.viewer})
        if call == ["api", api]:
            pull: dict[str, Any] = {"head": {"sha": self.head_sha}, "user": {"login": self.author}, "state": self.state}
            if self.repository_id is not None:
                pull["base"] = {"repo": {"id": self.repository_id, "full_name": PR.slug}}
            if self.html_url is not None:
                pull["html_url"] = self.html_url
            return json.dumps(pull)
        if call[:3] == ["api", "--paginate", "--slurp"]:
            endpoint = call[3]
            comments = re.match(rf"{api}/reviews/(\d+)/comments", endpoint)
            if comments is not None:
                return json.dumps([self.comments[int(comments[1])]])
            if endpoint.startswith(f"{api}/files"):
                return json.dumps([self.files])
            if endpoint.startswith(f"{api}/reviews"):
                return json.dumps([self.reviews])
        if call[:4] == ["api", "--method", "POST", f"{api}/reviews"]:
            assert stdin is not None
            payload = json.loads(stdin)
            self.posts.append(payload)
            if self.fail_post:
                raise GitHubCliError(self.fail_post)
            review_id = 100 + len(self.reviews)
            review = {
                "id": review_id,
                "user": {"login": self.viewer},
                "body": payload["body"],
                "commit_id": payload["commit_id"],
                "state": _EVENT_STATE[payload["event"]],
                "html_url": f"{PR.url}#pullrequestreview-{review_id}",
            }
            self.reviews.append(review)
            self.comments[review_id] = [
                {"id": review_id * 10 + index, "path": row["path"], "body": row["body"], "line": row["line"]}
                for index, row in enumerate(payload["comments"])
            ]
            if self.lose_response:
                raise GitHubCliError("`gh api --method` timed out after 60s")
            return json.dumps(review)
        raise AssertionError(f"unexpected gh call: {call}")


# --------------------------------------------------------------------------- #
# planning (pure)
# --------------------------------------------------------------------------- #


def _state(**overrides: Any) -> PullRequestState:
    fields: dict[str, Any] = {
        "head_sha": "h" * 40,
        "author": "alice",
        "viewer": "bob",
        "open": True,
        "files": (PullRequestFile("src/app.py", PATCH), PullRequestFile("logo.png", None)),
    }
    fields.update(overrides)
    return PullRequestState(**fields)


def _candidate(annotation_id: str, start: int, end: int = 0, **overrides: Any) -> PublishCandidate:
    fields: dict[str, Any] = {
        "annotation_id": annotation_id,
        "path": "src/app.py",
        "side": "new",
        "start_line": start,
        "end_line": end or start,
        "kind": "comment",
        "body": f"note {annotation_id}",
    }
    fields.update(overrides)
    return PublishCandidate(**fields)


def _plan(candidates: list[PublishCandidate], *, outcome: str = "", current: bool = True, **state: Any) -> Any:
    return build_plan(
        PR,
        candidates,
        commit_id="h" * 40,
        outcome=outcome,
        outcome_summary="Ship it" if outcome else "",
        outcome_current=current,
        state=_state(**state),
    )


def test_hunk_ranges_cover_both_sides_and_default_counts() -> None:
    ranges = hunk_ranges("@@ -3,4 +3,5 @@ def f():\n x\n@@ -20 +21,0 @@\n-y")
    assert ranges == {"LEFT": ((3, 6), (20, 20)), "RIGHT": ((3, 7),)}


def test_comments_inside_a_github_hunk_go_inline_on_the_right_side() -> None:
    plan = _plan(
        [
            _candidate("a", 3),
            _candidate("b", 2, 4, kind="request_change"),
            _candidate("c", 3, side="old", kind="suggestion"),
        ]
    )
    assert [item.payload() for item in plan.inline] == [
        {
            "path": "src/app.py",
            "side": "RIGHT",
            "line": 4,
            "body": "**Change requested:** note b",
            "start_line": 2,
            "start_side": "RIGHT",
        },
        {"path": "src/app.py", "side": "RIGHT", "line": 3, "body": "note a"},
        {"path": "src/app.py", "side": "LEFT", "line": 3, "body": "**Suggestion:** note c"},
    ]
    assert plan.folded == ()


def test_comments_github_cannot_place_move_to_the_body_and_are_never_dropped() -> None:
    plan = _plan(
        [
            _candidate("outside", 25),
            _candidate("straddles", 5, 9),
            _candidate("file", 0),
            _candidate("orphan", 3, state="orphaned", anchor_detail="line was deleted"),
            _candidate("binary", 1, path="logo.png"),
            _candidate("elsewhere", 1, path="README.md"),
        ]
    )
    assert plan.inline == ()
    reasons = {item.annotation_id: item.reason for item in plan.folded}
    assert reasons == {
        "outside": "outside the lines GitHub shows in the diff",
        "straddles": "outside the lines GitHub shows in the diff",
        "file": "file-level comment",
        "orphan": "line was deleted",
        "binary": "GitHub shows no diff for this file",
        "elsewhere": "file is not part of the pull request diff",
    }
    body = plan.body("op-1")
    assert "- `src/app.py:L25`: note outside" in body
    assert "- `src/app.py:L5-L9`: note straddles" in body
    assert "- `src/app.py`: note file" in body
    assert body.endswith("<!-- lemoncrow:op=op-1 -->")
    assert set(plan.annotation_ids) == set(reasons)


def test_a_renamed_file_is_addressed_by_its_new_name() -> None:
    plan = _plan(
        [_candidate("old-name", 3, path="src/old.py")],
        files=(PullRequestFile("src/app.py", PATCH, previous_path="src/old.py"),),
    )
    assert plan.inline[0].path == "src/app.py"


def test_an_empty_looks_good_is_not_published() -> None:
    plan = _plan([_candidate("lg", 3, kind="looks_good", body="  ")])
    assert plan.annotation_ids == ()
    assert plan.empty


@pytest.mark.parametrize(
    ("outcome", "event"), [("lgtm", "APPROVE"), ("changes_requested", "REQUEST_CHANGES"), ("comment", "COMMENT")]
)
def test_the_current_verdict_becomes_the_review_event(outcome: str, event: str) -> None:
    plan = _plan([], outcome=outcome)
    assert plan.event == event
    assert plan.body("x").startswith("Ship it")
    assert plan.notices == ()


def test_a_verdict_on_an_older_revision_publishes_as_comment() -> None:
    plan = _plan([_candidate("a", 3)], outcome="lgtm", current=False)
    assert plan.event == "COMMENT"
    assert plan.summary == ""
    assert "older revision" in plan.notices[0]


def test_your_own_or_a_closed_pull_request_publishes_as_comment() -> None:
    own = _plan([], outcome="lgtm", viewer="Alice")
    assert own.event == "COMMENT"
    assert "your own pull request" in own.notices[0]
    closed = _plan([], outcome="changes_requested", open=False)
    assert closed.event == "COMMENT"
    assert "closed" in closed.notices[0]


def test_a_verdict_already_on_github_is_not_published_twice() -> None:
    earlier = RemoteReview(id=1, body="Ship it\n\n<!-- lemoncrow:op=gop-1 -->", commit_id="h" * 40, state="APPROVED")
    plan = _plan([_candidate("a", 3)], outcome="lgtm", viewer_reviews=(earlier,))
    assert plan.event == "COMMENT"
    assert plan.summary == ""
    assert len(plan.inline) == 1
    assert _plan([], outcome="lgtm", viewer_reviews=(earlier,)).empty


def test_the_plan_hash_binds_content_not_the_operation_id() -> None:
    first = _plan([_candidate("a", 3)], outcome="lgtm")
    assert first.plan_hash == _plan([_candidate("a", 3)], outcome="lgtm").plan_hash
    assert first.plan_hash != _plan([_candidate("a", 3, body="edited")], outcome="lgtm").plan_hash


def test_pr_source_refs_round_trip() -> None:
    assert PR.source_ref == "github:acme/widgets#7"
    assert parse_pr_source_ref(PR.source_ref) == PR
    assert parse_pr_source_ref("main...feature") is None
    assert pull_request_ref(Path("."), "https://github.com/acme/widgets/pull/7/files") == PR
    assert pull_request_ref(Path("."), "acme/widgets#7") == PR


# --------------------------------------------------------------------------- #
# the Reader API against a fake GitHub
# --------------------------------------------------------------------------- #


def _git_commit(repo: Any, message: str, offset: int) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature("Fixture", "fixture@example.invalid", 1700000000 + offset, 0)
    return str(repo.create_commit("HEAD", signature, signature, message, tree, parents))


class Workspace:
    def __init__(self, client: TestClient, store: ReviewStore, review_id: str, head_sha: str) -> None:
        self.client = client
        self.store = store
        self.review_id = review_id
        self.head_sha = head_sha

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self.client.post(
            f"/api/reviews/{self.review_id}{path}", json=body or {}, headers={"Authorization": f"Bearer {TOKEN}"}
        )

    def revision_id(self) -> str:
        revision = self.store.latest_revision(self.review_id)
        assert revision is not None
        return revision.id

    def comment(self, line: int, body: str, **extra: Any) -> str:
        response = self.post(
            "/annotations",
            {
                "path": "src/app.py",
                "start_line": line,
                "body": body,
                "expected_revision_id": self.revision_id(),
                **extra,
            },
        )
        assert response.status_code == 201, response.text
        return str(response.json()["annotation"]["id"])

    def publish(self) -> Any:
        preview = self.post("/github/preview")
        assert preview.status_code == 200, preview.text
        plan = preview.json()
        return plan, self.post(
            "/github/publish",
            {
                "operation_id": plan["operation_id"],
                "expected_revision_id": plan["revision_id"],
                "expected_plan_hash": plan["plan_hash"],
            },
        )


def _workspace(tmp_path: Path, *, enabled: bool = True, source_ref: str = PR.source_ref) -> Workspace:
    repo_root = tmp_path / "repo"
    repo_root.mkdir(parents=True)
    repo = pygit2.init_repository(str(repo_root), initial_head="main")
    lines = [f"line {number}\n" for number in range(1, 31)]
    (repo_root / "src").mkdir()
    (repo_root / "src" / "app.py").write_text("".join(lines), encoding="utf-8")
    base = _git_commit(repo, "base", 0)
    lines[2] = "line three\n"
    (repo_root / "src" / "app.py").write_text("".join(lines), encoding="utf-8")
    head = _git_commit(repo, "head", 1)

    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    rng = resolve_rev_range(repo_root, base=base, head=head)
    build = build_review_packet_with_blobs(repo_root, rng, store_root=store_root, with_patch_text=True)
    session = open_or_create_session(store, repo_root, rng, title="PR #7: widgets")
    store.update_session(session.id, source_ref=source_ref)
    snapshot_revision(store, session, repo_root, rng, store_root=store_root, build=build)
    app = FastAPI()
    register_review_api(
        app,
        store,
        auth_dependency=make_token_dependency(TOKEN),
        repo_root=repo_root,
        port=PORT,
        github_publish_enabled=enabled,
    )
    return Workspace(TestClient(app, base_url=BASE), store, session.id, head)


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub(head_sha="")
    monkeypatch.setattr(github_source, "run_gh", fake)
    return fake


def test_publish_posts_one_review_and_records_each_comment(tmp_path: Path, github: FakeGitHub) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    inline = ws.comment(3, "why three?", kind="request_change")
    folded = ws.comment(25, "unrelated cleanup")
    ws.post(
        "/outcome",
        {"outcome": "lgtm", "summary": "Ship it", "expected_revision_id": ws.revision_id()},
    )

    plan, response = ws.publish()
    assert response.status_code == 200, response.text
    assert plan["event"] == "APPROVE"
    assert [item["annotation_id"] for item in plan["inline"]] == [inline]
    assert [item["annotation_id"] for item in plan["folded"]] == [folded]

    assert len(github.posts) == 1
    posted = github.posts[0]
    assert posted["commit_id"] == ws.head_sha
    assert posted["event"] == "APPROVE"
    assert posted["comments"] == [
        {"path": "src/app.py", "side": "RIGHT", "line": 3, "body": "**Change requested:** why three?"}
    ]
    assert posted["body"].startswith("Ship it")
    assert "`src/app.py:L25`: unrelated cleanup" in posted["body"]

    sent_inline = ws.store.list_deliveries(inline)
    assert [(item.target_type, item.target_ref, item.state, item.remote_ref) for item in sent_inline] == [
        ("github_review", f"github-repo:{REPO_ID}#7@github.com", "sent", "github-comment:1000")
    ]
    assert ws.store.list_deliveries(folded)[0].remote_ref == "github-review:100"

    # Nothing new: a second publish has nothing to post, and the approval is not repeated.
    again = ws.post("/github/preview").json()
    assert again["empty"] is True
    assert again["inline"] == [] and again["folded"] == []


def test_publishing_to_github_does_not_count_as_telling_the_agent(tmp_path: Path, github: FakeGitHub) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    ws.comment(3, "why three?")
    assert ws.publish()[1].status_code == 200
    feedback = ws.post("/feedback/export").json()
    assert feedback["open"] == 1


def test_the_reader_is_told_what_is_already_on_github(tmp_path: Path, github: FakeGitHub) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    ws.comment(3, "why three?")
    ws.comment(25, "unrelated cleanup")

    def status() -> dict[str, Any]:
        response = ws.client.get(
            f"/api/reviews/{ws.review_id}/annotations", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        return dict(response.json()["feedback"])

    assert status()["github"] == {"unpublished": 2, "published": 0, "in_flight": 0}
    assert ws.publish()[1].status_code == 200
    after = status()
    assert after["github"] == {"unpublished": 0, "published": 2, "in_flight": 0}
    assert after["unpublished"] == 2  # still not handed to an agent

    plain = _workspace(tmp_path / "plain", source_ref="main...feature")
    plain.comment(3, "why three?")
    response = plain.client.get(
        f"/api/reviews/{plain.review_id}/annotations", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert "github" not in response.json()["feedback"]


def test_a_retry_after_a_lost_response_adopts_the_review_instead_of_posting_again(
    tmp_path: Path, github: FakeGitHub
) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    annotation = ws.comment(3, "why three?")
    github.lose_response = True
    _plan, first = ws.publish()
    assert first.status_code == 409
    assert "timed out" in first.json()["detail"]
    assert len(github.reviews) == 1  # GitHub did create it
    github.lose_response = False

    # The next preview finds the marked review, records the comment as sent,
    # and has nothing left to post.
    again = ws.post("/github/preview").json()
    assert again["empty"] is True
    assert ws.store.list_deliveries(annotation)[0].state == "sent"
    assert len(github.posts) == 1


def test_a_refused_post_records_failure_and_can_be_published_again(tmp_path: Path, github: FakeGitHub) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    annotation = ws.comment(3, "why three?")
    github.fail_post = "Validation Failed: pull_request_review_thread.line must be part of the diff"
    _plan, refused = ws.publish()
    assert refused.status_code == 409
    assert "must be part of the diff" in refused.json()["detail"]
    assert [item.state for item in ws.store.list_deliveries(annotation)] == ["failed"]
    assert github.reviews == []

    github.fail_post = ""
    _plan, published = ws.publish()
    assert published.status_code == 200, published.text
    assert len(github.reviews) == 1


@pytest.mark.parametrize(
    ("repository_id", "html_url"),
    [
        (None, PR.url),
        ("4242", PR.url),
        (True, PR.url),
        (0.5, PR.url),
        (REPO_ID, None),
        (REPO_ID, ""),
        (REPO_ID, 7),
        (REPO_ID, "ftp://github.com/acme/widgets/pull/7"),
        (REPO_ID, "https://user@github.com/acme/widgets/pull/7"),
        (REPO_ID, "http://[::1/acme/widgets/pull/7"),
    ],
)
def test_publishing_comments_needs_the_repositorys_durable_id_and_host_and_never_records_its_name(
    tmp_path: Path, github: FakeGitHub, repository_id: Any, html_url: Any
) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    github.repository_id = repository_id
    github.html_url = html_url
    annotation = ws.comment(3, "why three?")
    _plan, refused = ws.publish()
    assert refused.status_code == 409
    assert "repository id and host" in refused.json()["detail"]
    assert "nothing was published" in refused.json()["detail"]
    assert github.posts == [] and ws.store.list_deliveries(annotation) == ()


def test_published_comments_record_the_enterprise_host_that_served_the_pull_request(
    tmp_path: Path, github: FakeGitHub
) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    github.html_url = "https://GHE.Example.com/acme/widgets/pull/7"
    annotation = ws.comment(3, "why three?")
    _plan, published = ws.publish()
    assert published.status_code == 200, published.text
    [sent] = ws.store.list_deliveries(annotation)
    assert sent.target_ref == f"github-repo:{REPO_ID}#7@ghe.example.com"


def test_new_commits_on_the_pr_block_publishing_until_refresh(tmp_path: Path, github: FakeGitHub) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = "f" * 40
    ws.comment(3, "why three?")
    plan, response = ws.publish()
    assert plan["stale"] is True
    assert response.status_code == 409
    assert "to refresh" in response.json()["detail"]
    assert github.posts == []


def test_an_edit_after_the_preview_must_be_previewed_again(tmp_path: Path, github: FakeGitHub) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    annotation = ws.comment(3, "why three?")
    preview = ws.post("/github/preview").json()
    ws.client.patch(
        f"/api/annotations/{annotation}",
        json={"body": "changed", "expected_revision_id": ws.revision_id()},
        headers={"Authorization": f"Bearer {TOKEN}"},
    )
    response = ws.post(
        "/github/publish",
        {
            "operation_id": preview["operation_id"],
            "expected_revision_id": preview["revision_id"],
            "expected_plan_hash": preview["plan_hash"],
        },
    )
    assert response.status_code == 409
    assert "preview again" in response.json()["detail"]
    assert github.posts == []


def test_publishing_is_refused_off_the_local_server_and_for_non_pr_reviews(tmp_path: Path, github: FakeGitHub) -> None:
    hosted = _workspace(tmp_path / "hosted", enabled=False)
    response = hosted.post("/github/preview")
    assert response.status_code == 409
    assert "local LemonCrow server" in response.json()["detail"]

    local = _workspace(tmp_path / "local", source_ref="main...feature")
    response = local.post("/github/preview")
    assert response.status_code == 409
    assert "lc review --pr" in response.json()["detail"]


# --------------------------------------------------------------------------- #
# resolving and fetching a PR into a clone
# --------------------------------------------------------------------------- #


def _run(cwd: Path, *args: str) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def test_resolve_fetches_the_pr_without_creating_refs_and_diffs_from_the_merge_base(tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    _run(tmp_path, "git", "init", "--bare", "-q", "-b", "main", str(origin))
    author = tmp_path / "author"
    _run(tmp_path, "git", "clone", "-q", str(origin), str(author))
    for key, value in (("user.name", "A"), ("user.email", "a@example.invalid")):
        _run(author, "git", "config", key, value)
    (author / "a.txt").write_text("one\n")
    _run(author, "git", "add", ".")
    _run(author, "git", "commit", "-qm", "base")
    fork_point = _run(author, "git", "rev-parse", "HEAD")
    _run(author, "git", "push", "-q", "origin", "main")

    clone = tmp_path / "clone"
    _run(tmp_path, "git", "clone", "-q", str(origin), str(clone))
    _run(clone, "git", "remote", "set-url", "origin", "git@github.com:acme/widgets.git")
    _run(clone, "git", "config", f"url.{origin}.insteadOf", "git@github.com:acme/widgets.git")

    _run(author, "git", "checkout", "-qb", "feature")
    (author / "a.txt").write_text("two\n")
    _run(author, "git", "commit", "-qam", "feature")
    head = _run(author, "git", "rev-parse", "HEAD")
    _run(author, "git", "push", "-q", "origin", "HEAD:refs/pull/7/head")
    _run(author, "git", "checkout", "-q", "main")
    (author / "b.txt").write_text("later\n")
    _run(author, "git", "add", ".")
    _run(author, "git", "commit", "-qm", "main moved on")
    base_tip = _run(author, "git", "rev-parse", "HEAD")
    _run(author, "git", "push", "-q", "origin", "main")

    refs_before = _run(clone, "git", "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags")

    def gh(args: Any, stdin: str | None) -> str:
        assert list(args[:3]) == ["pr", "view", "7"]
        return json.dumps(
            {
                "url": PR.url,
                "title": "Widgets",
                "author": {"login": "alice"},
                "state": "OPEN",
                "headRefOid": head,
                "baseRefName": "main",
                "baseRefOid": base_tip,
            }
        )

    pull = resolve_pull_request(clone, "#7", runner=gh)
    assert pull.ref == PR
    assert pull.remote == "origin"
    assert (pull.head_sha, pull.base_sha, pull.merge_base_sha) == (head, base_tip, fork_point)
    assert pull.review_title == "PR #7: Widgets"
    assert _run(clone, "git", "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags") == refs_before
    assert "refs/pull" not in _run(clone, "git", "for-each-ref", "--format=%(refname)")


def test_a_clone_of_another_repository_is_refused_with_the_repo_to_clone(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(repo, "git", "init", "-q")
    _run(repo, "git", "remote", "add", "origin", "https://github.com/someone/else.git")
    _run(repo, "git", "remote", "add", "upstream", "https://github.com/ACME/widgets")
    assert find_remote(repo, PR) == "upstream"
    _run(repo, "git", "remote", "remove", "upstream")
    with pytest.raises(ValueError, match="inside a clone of acme/widgets"):
        find_remote(repo, PR)


# --------------------------------------------------------------------------- #
# CLI flags
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--publish"], "--publish needs --pr"),
        (["--dry-run"], "--dry-run only applies to --publish"),
        (["--pr", "7", "--base", "main"], "--pr picks the range itself"),
    ],
)
def test_pr_flag_combinations_are_checked(tmp_path: Path, args: list[str], message: str) -> None:
    result = CliRunner().invoke(cli, ["--root", str(tmp_path / "store"), "review", "--no-open", *args])
    assert result.exit_code != 0
    assert message in result.output
