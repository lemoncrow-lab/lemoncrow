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
    AI_FIX_MARKER,
    HUMAN_REVIEW_LINE,
    PublishCandidate,
    PullRequestFile,
    PullRequestState,
    PullRequestStateCache,
    RemoteReview,
    SignatureSignal,
    bot_display_name,
    build_plan,
    check_run_for,
    hunk_ranges,
    load_bot_approvals,
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
        self.calls: list[list[str]] = []

    def __call__(self, args: Any, stdin: str | None = None, *, cwd: Path | None = None) -> str:
        call = list(args)
        self.calls.append(call)
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


def _plan(
    candidates: list[PublishCandidate],
    *,
    outcome: str = "",
    current: bool = True,
    human_review: bool = False,
    **state: Any,
) -> Any:
    return build_plan(
        PR,
        candidates,
        commit_id="h" * 40,
        outcome=outcome,
        outcome_summary="Ship it" if outcome else "",
        outcome_current=current,
        state=_state(**state),
        human_review=human_review,
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
        (["--dry-run"], "use it with --pr NUMBER|URL and --ai, --publish-bot or --publish"),
        (["--pr", "7", "--base", "main"], "--pr picks the range itself"),
    ],
)
def test_pr_flag_combinations_are_checked(tmp_path: Path, args: list[str], message: str) -> None:
    result = CliRunner().invoke(cli, ["--root", str(tmp_path / "store"), "review", "--no-open", *args])
    assert result.exit_code != 0
    assert message in result.output


# --------------------------------------------------------------------------- #
# P6-3: the review as it will appear, the check run, bot approvals (SPEC 4.17)
# --------------------------------------------------------------------------- #


def test_every_review_body_ends_with_the_human_review_line_before_the_hidden_marker() -> None:
    plan = _plan([_candidate("outside", 25)], outcome="lgtm", human_review=True)
    shown = plan.shown_body()
    assert shown.endswith(HUMAN_REVIEW_LINE)
    assert "<!--" not in shown
    assert plan.body("op-9") == f"{shown}\n\n<!-- lemoncrow:op=op-9 -->"
    # Even a review with no summary and no folded comment carries the line.
    assert _plan([_candidate("a", 3)], human_review=True).shown_body() == HUMAN_REVIEW_LINE
    # The bot's review is an AI's: it never claims to be a human review.
    assert HUMAN_REVIEW_LINE not in _plan([_candidate("a", 3)], outcome="lgtm").body("op")
    assert plan.preview()["body"] == shown


def test_only_a_note_that_includes_an_ai_fix_is_marked_as_carrying_one() -> None:
    plain = _candidate("plain", 3)
    fixed = _candidate("fixed", 4, body=f"keep the guard\n\n{AI_FIX_MARKER}\n-x\n+y")
    plan = _plan([plain, fixed])
    assert {item.annotation_id: item.ai_fix for item in plan.inline} == {"plain": False, "fixed": True}
    assert [row["ai_fix"] for row in plan.preview()["inline"]] == [False, True]


def test_the_check_run_reads_only_a_signal_it_was_given() -> None:
    kwargs: dict[str, Any] = {"head_sha": "4e1b9c2" + "0" * 33, "event": "APPROVE"}
    first = {"signer": "srivera"}
    # No signal (every local review): no check run, so no "of 2" and no branch-protection claim.
    assert check_run_for(signal=None, **kwargs) is None
    assert check_run_for(signal=SignatureSignal(required=0, signed=0), **kwargs) is None
    assert check_run_for(signal=SignatureSignal(required=2, signed=1), **kwargs) is None  # no signer named
    # "signed by" needs a signature that exists, and the signer the signal names.
    assert check_run_for(signal=SignatureSignal(required=2, signed=0, **first), **kwargs) is None
    waiting = check_run_for(signal=SignatureSignal(required=2, signed=1, **first), **kwargs)
    assert waiting == {
        "name": "LemonCrow review",
        "text": "LemonCrow review \u00b7 signed by @srivera \u00b7 rev 4e1b9c2 \u00b7 1 of 2 signatures",
        "state": "pending",
        "label": "Pending \u00b7 waiting for a second signature",
    }
    done = check_run_for(signal=SignatureSignal(required=2, signed=2, **first), **kwargs)
    assert done is not None and (done["state"], done["label"]) == ("passing", "Passing")
    assert done["text"].endswith("2 of 2 signatures")
    refused = check_run_for(signal=SignatureSignal(required=2, signed=2, disagrees=True, **first), **kwargs)
    assert refused is not None and (refused["state"], refused["label"]) == (
        "failing",
        "Failing \u00b7 changes requested",
    )
    changes = check_run_for(
        signal=SignatureSignal(required=2, signed=1, **first), **{**kwargs, "event": "REQUEST_CHANGES"}
    )
    assert changes is not None and changes["state"] == "failing"
    # A count past the requirement is never printed.
    over = check_run_for(signal=SignatureSignal(required=2, signed=5, **first), **kwargs)
    assert over is not None and over["text"].endswith("2 of 2 signatures")


def test_wrap_up_previews_a_verdict_before_it_is_recorded_and_publishing_matches(
    tmp_path: Path, github: FakeGitHub
) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    ws.comment(3, "why three?", kind="request_change")
    before = ws.post("/github/preview").json()
    assert before["event"] == "COMMENT"  # nothing recorded yet

    proposed = ws.post("/github/preview", {"outcome": "changes_requested", "summary": "Fix first"})
    assert proposed.status_code == 200, proposed.text
    shown = proposed.json()
    assert shown["event"] == "REQUEST_CHANGES"
    assert shown["summary"] == "Fix first"
    assert shown["body"] == "Fix first\n\n" + HUMAN_REVIEW_LINE
    assert (shown["viewer"], shown["author"]) == ("bob", "alice")
    assert shown["check_run"] is None
    assert ws.store.latest_outcome(ws.review_id, "local") is None  # previewing recorded nothing
    assert github.posts == []

    ws.post(
        "/outcome",
        {"outcome": "changes_requested", "summary": "Fix first", "expected_revision_id": ws.revision_id()},
    )
    plan, response = ws.publish()
    assert response.status_code == 200, response.text
    assert plan["plan_hash"] == shown["plan_hash"]
    assert github.posts[0]["body"].startswith("Fix first\n\n" + HUMAN_REVIEW_LINE)


def test_a_previewed_approval_on_your_own_pull_request_is_drawn_as_a_comment(
    tmp_path: Path, github: FakeGitHub
) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    github.viewer = github.author
    shown = ws.post("/github/preview", {"outcome": "lgtm", "summary": ""}).json()
    assert shown["event"] == "COMMENT"
    assert "your own pull request" in shown["notices"][0]


@pytest.mark.parametrize("body", [{"outcome": "merge"}, {"outcome": ""}, {"outcome": "lgtm", "summary": 3}])
def test_a_preview_for_an_unknown_verdict_or_bad_summary_is_refused(
    tmp_path: Path, github: FakeGitHub, body: dict[str, Any]
) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    assert ws.post("/github/preview", body).status_code == 422
    too_long = ws.post("/github/preview", {"outcome": "lgtm", "summary": "x" * 16385})
    assert too_long.status_code == 422


def _bot_review(review_id: int, login: str, state: str, commit: str, kind: str = "Bot") -> dict[str, Any]:
    return {"id": review_id, "user": {"login": login, "type": kind}, "state": state, "commit_id": commit, "body": "x"}


def test_bot_approvals_are_the_bots_latest_approval_typed_bot_by_github(github: FakeGitHub) -> None:
    head = "a" * 40
    github.reviews = [
        _bot_review(1, "copilot-pull-request-reviewer[bot]", "APPROVED", head),
        _bot_review(2, "copilot-pull-request-reviewer[bot]", "COMMENTED", head),  # a comment ends nothing
        _bot_review(3, "old-bot[bot]", "APPROVED", "b" * 40),  # approved an earlier commit
        _bot_review(4, "changed-mind[bot]", "APPROVED", head),
        _bot_review(5, "changed-mind[bot]", "CHANGES_REQUESTED", head),  # a later verdict wins
        _bot_review(6, "dismissed[bot]", "APPROVED", head),
        _bot_review(7, "dismissed[bot]", "DISMISSED", head),
        _bot_review(8, "bob", "APPROVED", head, kind="User"),  # a person is not a bot
        _bot_review(9, "copilot-lookalike", "APPROVED", head, kind="User"),
        _bot_review(10, "bad login[bot]", "APPROVED", head),  # not a login GitHub could issue
        _bot_review(11, "pending[bot]", "PENDING", head),
    ]
    found = load_bot_approvals(PR, github, reviewed_head=head)
    assert [(item.login, item.name, item.current) for item in found] == [
        ("copilot-pull-request-reviewer[bot]", "Copilot", True),
        ("old-bot[bot]", "old-bot", False),
    ]
    assert load_bot_approvals(PR, github, reviewed_head="") == tuple(
        item.__class__(item.login, item.name, item.commit_id, False) for item in found
    )


def test_the_bot_approvals_route_reports_a_hint_and_changes_nothing(tmp_path: Path, github: FakeGitHub) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    github.reviews = [_bot_review(1, "copilot-pull-request-reviewer[bot]", "APPROVED", ws.head_sha)]
    ws.comment(3, "why three?")
    headers = {"Authorization": f"Bearer {TOKEN}"}
    notes_url = f"/api/reviews/{ws.review_id}/annotations"
    parts_url = f"/api/reviews/{ws.review_id}/targets"
    notes_before = ws.client.get(notes_url, headers=headers).json()
    parts_before = ws.client.get(parts_url, headers=headers)
    assert parts_before.status_code == 200, parts_before.text
    got = ws.client.get(f"/api/reviews/{ws.review_id}/github/bot-approvals", headers=headers)
    assert got.status_code == 200, got.text
    assert got.json()["approvals"] == [
        {
            "login": "copilot-pull-request-reviewer[bot]",
            "name": "Copilot",
            "commit_id": ws.head_sha,
            "current": True,
        }
    ]
    assert ws.client.get(notes_url, headers=headers).json() == notes_before
    assert ws.client.get(parts_url, headers=headers).json() == parts_before.json()
    assert github.posts == []

    plain = _workspace(tmp_path / "plain", source_ref="main...feature")
    assert plain.client.get(f"/api/reviews/{plain.review_id}/github/bot-approvals", headers=headers).status_code == 409


def test_a_plan_without_a_summary_posts_the_marker_alone_unless_it_is_a_human_review() -> None:
    # The bot's plan (no human line, no summary) posts exactly the marker, as before.
    assert _plan([_candidate("a", 3)]).body("op-1") == "<!-- lemoncrow:op=op-1 -->"
    assert (
        _plan([_candidate("a", 3)], human_review=True).body("op-1")
        == f"{HUMAN_REVIEW_LINE}\n\n<!-- lemoncrow:op=op-1 -->"
    )


def test_only_githubs_own_copilot_logins_are_named_copilot() -> None:
    assert bot_display_name("copilot-pull-request-reviewer[bot]") == "Copilot"
    assert bot_display_name("Copilot") == "Copilot"
    assert bot_display_name("copilot-helper[bot]") == "copilot-helper"
    assert bot_display_name("dependabot[bot]") == "dependabot"


def test_the_body_a_preview_shows_is_the_body_published_minus_the_hidden_marker(
    tmp_path: Path, github: FakeGitHub
) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    ws.comment(25, "unrelated cleanup")  # lands in the body, not inline
    ws.post("/outcome", {"outcome": "lgtm", "summary": "Ship it", "expected_revision_id": ws.revision_id()})
    plan, response = ws.publish()
    assert response.status_code == 200, response.text
    posted = github.posts[0]["body"]
    assert posted == f"{plan['body']}\n\n<!-- lemoncrow:op={plan['operation_id']} -->"
    assert HUMAN_REVIEW_LINE in plan["body"] and "unrelated cleanup" in plan["body"]


def test_typing_a_summary_re_plans_without_reading_the_pull_request_again(tmp_path: Path, github: FakeGitHub) -> None:
    ws = _workspace(tmp_path)
    github.head_sha = ws.head_sha
    ws.comment(3, "why three?")

    def pulls() -> list[list[str]]:
        return [call for call in github.calls if call == ["api", f"repos/{PR.slug}/pulls/{PR.number}"]]

    ws.post("/github/preview")  # a preview with no verdict always reads GitHub
    assert len(pulls()) == 1
    first = ws.post("/github/preview", {"outcome": "lgtm", "summary": "a"}).json()
    again = ws.post("/github/preview", {"outcome": "lgtm", "summary": "ab"}).json()
    assert (first["summary"], again["summary"]) == ("a", "ab")  # the plan follows what was typed
    assert len(pulls()) == 1  # the pull request was read once for all of them
    ws.post("/github/preview")
    assert len(pulls()) == 2  # a preview with no verdict is fresh again

    # Publishing reads GitHub afresh and drops what was held, so the next plan sees what it posted.
    github.head_sha = ws.head_sha
    ws.post("/outcome", {"outcome": "lgtm", "summary": "Ship it", "expected_revision_id": ws.revision_id()})
    _plan_after, response = ws.publish()
    assert response.status_code == 200, response.text
    seen = ws.post("/github/preview", {"outcome": "lgtm", "summary": "Ship it"}).json()
    assert "already on GitHub" in seen["notices"][0]
    assert seen["summary"] == ""  # the typed summary is dropped, and the notice says why


def test_held_pull_request_states_are_bounded_by_age_and_by_count() -> None:
    now = [0.0]
    cache = PullRequestStateCache(ttl=15.0, max_entries=3, clock=lambda: now[0])
    one, two = _state(head_sha="1" * 40), _state(head_sha="2" * 40)
    cache.put("a", one)
    cache.put("b", two)
    assert cache.get("a") is one
    now[0] = 16.0
    assert cache.get("a") is None  # expired: never served
    assert "a" in cache and "b" in cache  # ...but still held until the next write
    cache.put("c", one)
    assert "a" not in cache and "b" not in cache and len(cache) == 1  # expired entries go on the next write
    for key in ("d", "e", "f"):
        cache.put(key, two)
    assert list(k for k in "cdef" if k in cache) == ["d", "e", "f"]  # the cap evicts the oldest written
    cache.put("d", one)  # a rewrite is the newest
    cache.put("g", two)
    assert [k for k in "defg" if k in cache] == ["d", "f", "g"]
    cache.drop("d")
    assert "d" not in cache


def test_a_verdict_already_on_github_is_flagged_for_the_screen_not_only_in_a_notice() -> None:
    earlier = RemoteReview(id=1, body="Ship it\n\n<!-- lemoncrow:op=gop-1 -->", commit_id="h" * 40, state="APPROVED")
    held = _plan([_candidate("a", 3)], outcome="lgtm", viewer_reviews=(earlier,))
    assert held.verdict_on_github is True and held.preview()["verdict_on_github"] is True
    fresh = _plan([_candidate("a", 3)], outcome="lgtm")
    assert fresh.verdict_on_github is False and fresh.preview()["verdict_on_github"] is False
    # Downgraded for another reason (your own pull request): GitHub shows no verdict from this post, and says why.
    own = _plan([], outcome="lgtm", viewer="Alice")
    assert own.event == "COMMENT" and own.verdict_on_github is False
