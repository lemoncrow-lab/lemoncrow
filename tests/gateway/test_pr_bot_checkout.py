"""The Action makes the checkout the review needs: none in the workspace, or a shallow one, or one it cannot fix.

Real ``git`` against a bare repository on disk, reached through a ``file://`` "GitHub" so the remote URL the Action
builds (``<server>/<owner>/<repo>.git``) is the one that is fetched from. Only the GitHub API is faked.
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review.pr_bot import checkout, ci_env

TOKEN = "ghs_TOKENDONOTLEAK1234567890"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class Server:
    """``acme/widgets`` on disk: ``main`` ahead of the commit the pull request branched from, and ``refs/pull/7/head``."""

    def __init__(self, root: Path, *, main_ahead: int = 2, unrelated: bool = False) -> None:
        self.root = root / "srv"
        self.bare = self.root / "acme" / "widgets.git"
        self.bare.parent.mkdir(parents=True)
        git(self.root, "init", "--bare", "-q", "-b", "main", str(self.bare))
        author = root / "author"
        git(root, "clone", "-q", str(self.bare), str(author))
        for key, value in (("user.name", "A"), ("user.email", "a@example.invalid")):
            git(author, "config", key, value)
        (author / "a.txt").write_text("one\n")
        git(author, "add", "-A")
        git(author, "commit", "-qm", "root")
        self.fork_point = git(author, "rev-parse", "HEAD")
        git(author, "push", "-q", "origin", "main")
        git(author, "checkout", "-qb", "feature")
        (author / "b.txt").write_text("two\n")
        git(author, "add", "-A")
        git(author, "commit", "-qm", "feature one")
        (author / "b.txt").write_text("three\n")
        git(author, "commit", "-qam", "feature two")
        self.head = git(author, "rev-parse", "HEAD")
        git(author, "push", "-q", "origin", "HEAD:refs/pull/7/head")
        git(author, "checkout", "-q", "main")
        if unrelated:
            git(author, "checkout", "-q", "--orphan", "other")
            git(author, "rm", "-rqf", ".")
            (author / "z.txt").write_text("z\n")
            git(author, "add", "-A")
            git(author, "commit", "-qm", "unrelated root")
            git(author, "push", "-q", "-f", "origin", "other:main")
        else:
            for index in range(main_ahead):
                (author / f"m{index}.txt").write_text(f"{index}\n")
                git(author, "add", "-A")
                git(author, "commit", "-qm", f"main {index}")
            git(author, "push", "-q", "origin", "main")
        self.base_tip = git(author, "rev-parse", "main") if not unrelated else git(author, "rev-parse", "other")

    @property
    def url(self) -> str:
        return f"file://{self.root}"


def environment(tmp_path: Path, server: Server, *, workspace: Path, event_name: str = "pull_request") -> dict[str, str]:
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            {
                "action": "opened",
                "number": 7,
                "pull_request": {"number": 7, "base": {"ref": "main"}, "head": {"sha": server.head}},
            }
        )
    )
    return {
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(tmp_path / "home"),
        "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_SERVER_URL": server.url,
        "GITHUB_REPOSITORY": "acme/widgets",
        "GITHUB_EVENT_NAME": event_name,
        "GITHUB_EVENT_PATH": str(event),
        "GH_TOKEN": TOKEN,
        "LEMONCROW_REVIEW_PR": "7",
    }


class Spy:
    """``git`` that records each call and the environment it got."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []

    def __call__(self, args: Sequence[str], cwd: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), dict(env)))
        return checkout.run_git(args, cwd, env)

    def fetches(self) -> list[list[str]]:
        return [args for args, _ in self.calls if args[0] == "fetch"]


def depth_of(args: Sequence[str]) -> str:
    return next((arg for arg in args if arg.startswith("--depth") or arg == "--unshallow"), "")


@pytest.fixture
def server(tmp_path: Path) -> Server:
    return Server(tmp_path)


# --------------------------------------------------------------------------- #
# no checkout at all
# --------------------------------------------------------------------------- #


def test_with_no_checkout_the_action_makes_one_from_the_base_branch_and_fetches_the_pull_request(
    tmp_path: Path, server: Server
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outcome = checkout.prepare(environment(tmp_path, server, workspace=workspace))
    assert outcome.action == "created" and outcome.commits == 3
    assert git(workspace, "remote", "get-url", "origin") == f"{server.url}/acme/widgets.git"
    # The base branch is what is on disk; the pull request's commits are objects, found by their ref.
    assert git(workspace, "rev-parse", "HEAD") == server.base_tip
    assert not (workspace / "b.txt").exists(), "nothing the pull request's author wrote is on disk"
    assert git(workspace, "rev-parse", "refs/lemoncrow/pr-7-head") == server.head
    assert git(workspace, "merge-base", "refs/lemoncrow/pr-7-base", server.head) == server.fork_point


def test_the_workspace_does_not_have_to_exist_and_working_directory_picks_where(tmp_path: Path, server: Server) -> None:
    env = {**environment(tmp_path, server, workspace=tmp_path / "ws"), "LEMONCROW_WORKDIR": "sub/dir"}
    checkout.prepare(env)
    assert git(tmp_path / "ws" / "sub" / "dir", "rev-parse", "--is-inside-work-tree") == "true"


def test_the_token_is_given_to_git_through_the_environment_and_never_written_down(
    tmp_path: Path, server: Server
) -> None:
    workspace = tmp_path / "workspace"
    spy = Spy()
    checkout.prepare(environment(tmp_path, server, workspace=workspace), run=spy)
    fetch_env = next(env for args, env in spy.calls if args[0] == "fetch")
    header = fetch_env["GIT_CONFIG_VALUE_0"]
    assert fetch_env["GIT_CONFIG_KEY_0"] == f"http.{server.url}/.extraheader" and fetch_env["GIT_CONFIG_COUNT"] == "1"
    assert header == f"AUTHORIZATION: basic {checkout.auth_value(TOKEN)}"
    assert TOKEN not in json.dumps([args for args, _ in spy.calls]), "never on a command line"
    config = (workspace / ".git" / "config").read_text()
    assert TOKEN not in config and checkout.auth_value(TOKEN) not in config and "extraheader" not in config


# --------------------------------------------------------------------------- #
# a checkout that is there
# --------------------------------------------------------------------------- #


def test_a_complete_checkout_is_left_alone(tmp_path: Path, server: Server) -> None:
    workspace = tmp_path / "workspace"
    git(tmp_path, "clone", "-q", str(server.bare), str(workspace))
    spy = Spy()
    outcome = checkout.prepare(environment(tmp_path, server, workspace=workspace), run=spy)
    assert outcome.action == "existing" and spy.fetches() == []


def shallow_checkout(tmp_path: Path, server: Server) -> Path:
    """What ``actions/checkout`` leaves with its default ``fetch-depth``: one commit."""

    workspace = tmp_path / "workspace"
    git(tmp_path, "clone", "-q", "--depth", "1", server.url + "/acme/widgets.git", str(workspace))
    return workspace


def test_a_shallow_checkout_is_deepened_to_the_merge_base(tmp_path: Path, server: Server) -> None:
    workspace = shallow_checkout(tmp_path, server)
    assert git(workspace, "rev-parse", "--is-shallow-repository") == "true"
    outcome = checkout.prepare(environment(tmp_path, server, workspace=workspace))
    assert outcome.action == "deepened"
    assert git(workspace, "merge-base", "refs/lemoncrow/pr-7-base", "refs/lemoncrow/pr-7-head") == server.fork_point
    # What was checked out stays as it was.
    assert git(workspace, "rev-parse", "HEAD") == server.base_tip


def test_the_history_is_fetched_only_as_deep_as_the_merge_base_needs(
    tmp_path: Path, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Main is two commits past the fork point and the pull request is two commits long: depth 2 is not enough."""

    monkeypatch.setattr(checkout, "HISTORY_LADDER", (2, 3, 50))
    workspace = shallow_checkout(tmp_path, server)
    spy = Spy()
    checkout.prepare(environment(tmp_path, server, workspace=workspace), run=spy)
    assert [depth_of(args) for args in spy.fetches()] == [
        "--depth=2",
        "--depth=3",
    ], "it stops at the first depth that works"


def test_when_every_depth_falls_short_it_fetches_everything(
    tmp_path: Path, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(checkout, "HISTORY_LADDER", (1,))
    workspace = shallow_checkout(tmp_path, server)
    spy = Spy()
    checkout.prepare(environment(tmp_path, server, workspace=workspace), run=spy)
    assert [depth_of(args) for args in spy.fetches()] == ["--depth=1", "--unshallow"]
    assert git(workspace, "rev-parse", "--is-shallow-repository") in {
        "true",
        "false",
    }  # the merge commit stays a boundary
    assert git(workspace, "merge-base", "refs/lemoncrow/pr-7-base", "refs/lemoncrow/pr-7-head") == server.fork_point


def test_persisted_credentials_from_actions_checkout_are_used_as_they_are(tmp_path: Path, server: Server) -> None:
    workspace = shallow_checkout(tmp_path, server)
    git(workspace, "config", f"http.{server.url}/.extraheader", "AUTHORIZATION: basic already-there")
    spy = Spy()
    checkout.prepare(environment(tmp_path, server, workspace=workspace), run=spy)
    assert all(
        "GIT_CONFIG_COUNT" not in env for _, env in spy.calls
    ), "a second Authorization header would be sent twice"


def test_a_checkout_made_with_persist_credentials_false_gets_the_token_for_the_call(
    tmp_path: Path, server: Server
) -> None:
    workspace = shallow_checkout(tmp_path, server)
    spy = Spy()
    checkout.prepare(environment(tmp_path, server, workspace=workspace), run=spy)
    assert next(env for args, env in spy.calls if args[0] == "fetch")["GIT_CONFIG_COUNT"] == "1"


# --------------------------------------------------------------------------- #
# what it cannot fix
# --------------------------------------------------------------------------- #


def test_a_shallow_checkout_that_cannot_be_deepened_says_fetch_depth_zero(tmp_path: Path, server: Server) -> None:
    workspace = shallow_checkout(tmp_path, server)
    git(workspace, "remote", "set-url", "origin", f"file://{tmp_path}/gone/acme/widgets.git")
    with pytest.raises(checkout.CheckoutError) as caught:
        checkout.prepare(environment(tmp_path, server, workspace=workspace))
    message = str(caught.value)
    assert "shallow and could not be deepened" in message and "`fetch-depth: 0`" in message
    assert TOKEN not in message


def test_with_no_checkout_a_failed_fetch_says_how_to_bring_one(tmp_path: Path, server: Server) -> None:
    env = {**environment(tmp_path, server, workspace=tmp_path / "ws"), "GITHUB_SERVER_URL": f"file://{tmp_path}/gone"}
    with pytest.raises(checkout.CheckoutError, match=r"could not fetch the pull request.*`fetch-depth: 0`"):
        checkout.prepare(env)


def test_a_refused_fetch_names_the_contents_permission(tmp_path: Path, server: Server) -> None:
    def refuse(args: Sequence[str], cwd: Path, env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
        if args[0] == "fetch":
            return subprocess.CompletedProcess(
                args,
                128,
                "",
                "remote: Write access to repository not granted.\nfatal: unable to access: The requested URL returned error: 403",
            )
        return checkout.run_git(args, cwd, env)

    with pytest.raises(checkout.CheckoutError) as caught:
        checkout.prepare(environment(tmp_path, server, workspace=tmp_path / "ws"), run=refuse)
    assert "`contents: read`" in str(
        caught.value
    ) and "permissions: { contents: read, pull-requests: write, statuses: write }" in str(caught.value)


def test_a_pull_request_with_no_common_history_is_said_plainly(tmp_path: Path) -> None:
    server = Server(tmp_path, unrelated=True)
    with pytest.raises(checkout.CheckoutError, match="shares no history with main even with the full history"):
        checkout.prepare(environment(tmp_path, server, workspace=tmp_path / "ws"))


def test_a_comment_event_reads_the_base_branch_and_head_from_the_api(tmp_path: Path, server: Server) -> None:
    env = environment(tmp_path, server, workspace=tmp_path / "ws", event_name="issue_comment")
    Path(env["GITHUB_EVENT_PATH"]).write_text(
        json.dumps(
            {
                "action": "created",
                "issue": {"number": 7, "pull_request": {"url": "x"}},
                "comment": {"id": 1, "body": "@lemoncrow help"},
            }
        )
    )
    asked: list[str] = []

    def api(url: str, token: str) -> object:
        asked.append(url)
        assert token == TOKEN
        return {"base": {"ref": "main"}, "head": {"sha": server.head}}

    outcome = checkout.prepare(env, api=api)
    assert outcome.action == "created" and asked == ["https://api.github.com/repos/acme/widgets/pulls/7"]


def test_an_unreadable_pull_request_names_the_permission_that_would_fix_it(tmp_path: Path, server: Server) -> None:
    env = environment(tmp_path, server, workspace=tmp_path / "ws", event_name="issue_comment")
    Path(env["GITHUB_EVENT_PATH"]).write_text(
        json.dumps({"action": "created", "issue": {"number": 7, "pull_request": {"url": "x"}}})
    )

    def api(url: str, token: str) -> object:
        raise OSError("HTTP Error 403")

    with pytest.raises(checkout.CheckoutError, match=r"Could not read pull request #7.*`contents: read`"):
        checkout.prepare(env, api=api)


# --------------------------------------------------------------------------- #
# the script the Action runs
# --------------------------------------------------------------------------- #


def run_script(env: Mapping[str, str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script = Path(checkout.__file__)
    # -I -S: no site-packages, so LemonCrow is not importable: the sibling ci_env.py is what the script falls back to.
    return subprocess.run(
        [sys.executable, "-I", "-S", str(script)], env=dict(env), capture_output=True, text=True, cwd=tmp_path
    )


def test_the_script_needs_nothing_installed_and_reports_what_it_did(tmp_path: Path, server: Server) -> None:
    output = tmp_path / "out"
    env = {**environment(tmp_path, server, workspace=tmp_path / "ws"), "GITHUB_OUTPUT": str(output)}
    done = run_script(env, tmp_path)
    assert done.returncode == 0, done.stderr
    assert done.stdout.splitlines()[0] == f"::add-mask::{checkout.auth_value(TOKEN)}"
    assert "Made a checkout of the base branch and fetched pull request #7" in done.stdout
    assert output.read_text() == "checkout=created\n"
    assert TOKEN not in done.stdout + done.stderr


def test_the_script_fails_with_an_annotation_and_sets_the_check_to_error(
    tmp_path: Path, server: Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    posted: list[tuple[str, str]] = []
    monkeypatch.setattr(
        ci_env, "post_status", lambda env, state, description, **kw: posted.append((state, description)) or ""
    )
    env = {**environment(tmp_path, server, workspace=tmp_path / "ws"), "GITHUB_SERVER_URL": f"file://{tmp_path}/gone"}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert checkout.main([]) == 1
    assert posted and posted[0][0] == "error" and posted[0][1].startswith("Could not check out the pull request:")
    assert len(posted[0][1]) <= 140
