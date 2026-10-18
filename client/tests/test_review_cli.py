from __future__ import annotations

import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from lemoncrow_client.cli import main, review_entrypoint
from lemoncrow_client.errors import AgentAction, ClientError, ErrorCode
from lemoncrow_client.review import (
    _active_review_base,
    _freeze_range,
    _gitlinks_for_range,
    _materialize_commit,
    _review_base,
    _with_review_base,
    _workspace_branch,
    resolve_review_range,
)
from lemoncrow_client.review_base import BRANCH_REVIEW_PATH, EMPTY_TREE_SHA
from lemoncrow_client.transport import Response


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "review@example.test")
    _git(repo, "config", "user.name", "Review Test")
    (repo / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (repo / "a.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_review_help_is_part_of_the_thin_cli() -> None:
    sink = StringIO()
    assert main(["review", "--help"], stdout=sink) == 0
    assert "--working-tree" in sink.getvalue()
    assert "--staged" in sink.getvalue()


def test_full_cli_review_flag_points_to_the_reader() -> None:
    sink = StringIO()
    assert main(["review", "--since-my-review", "--no-open"], stdout=sink) == 2
    out = sink.getvalue()
    assert "unknown review option: --since-my-review" in out
    assert "`uv run lc review`" in out
    assert "`lc review --open`" in out
    sink = StringIO()
    assert main(["review", "--bogus"], stdout=sink) == 2
    assert sink.getvalue() == (
        "unknown review option: --bogus\nRun `lc review --help` for the options this client supports.\n"
    )


@pytest.mark.parametrize(
    ("review", "noted"),
    [
        # A hosted server titles only a Review its capture creates.
        ({"title": "Existing hosted Review"}, True),
        ({"title": "Fix review labels"}, False),
        # A local server keeps it as the Snapshot's author title under a manual name.
        ({"title": "Manual name", "auto_title": "Fix review labels"}, False),
    ],
)
def test_review_title_that_the_server_did_not_apply_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, review: dict[str, str], noted: bool
) -> None:
    from lemoncrow_client import review as review_mod

    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    titles: list[str] = []

    def capture(config: object, rng: object, **kwargs: object) -> tuple[dict[str, object], str]:
        titles.append(str(kwargs["title"]))
        return {"review": review}, "http://127.0.0.1:7420/r/1"

    monkeypatch.setattr(
        review_mod, "load_config", lambda **_kwargs: SimpleNamespace(hosted=False, authenticated=True, repo_root=repo)
    )
    monkeypatch.setattr(review_mod, "_capture", capture)
    sink = StringIO()
    assert main(["review", "--working-tree", "--no-open", "--title", "Fix review labels"], stdout=sink) == 0
    assert titles == ["Fix review labels"]
    note = f'--title was not applied; the Review this Snapshot joined keeps the title "{review['title']}"'
    assert (note in sink.getvalue()) is noted


def test_review_open_arms_local_browser_pairing_before_opening(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """SL-1: the local Reader claims a browser capability only inside a CLI-armed window."""

    import webbrowser

    from lemoncrow_client import review as review_mod
    from lemoncrow_client.session import RemoteSession

    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    events: list[str] = []
    monkeypatch.setattr(
        review_mod,
        "load_config",
        lambda **_kwargs: SimpleNamespace(hosted=False, authenticated=True, token="lc_local_machine", repo_root=repo),
    )
    monkeypatch.setattr(review_mod, "_capture", lambda *_a, **_k: ({"review": {}}, "http://127.0.0.1:7420/r/1"))
    monkeypatch.setattr(RemoteSession, "__init__", lambda self, config: None)
    monkeypatch.setattr(RemoteSession, "close", lambda self: None)
    monkeypatch.setattr(
        RemoteSession,
        "pair_local_review_browser",
        lambda self, path, timeout_s=None: events.append(f"arm {path}") or {"state": "armed"},
    )
    monkeypatch.setattr(webbrowser, "open", lambda url: events.append(f"open {url}"))

    assert main(["review", "--working-tree", "--open"], stdout=StringIO()) == 0
    assert events == ["arm /r/1", "open http://127.0.0.1:7420/r/1"]


def test_dirty_default_resolves_to_working_tree_without_pygit2(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "new.py").write_text("new = True\n", encoding="utf-8")

    resolved = resolve_review_range(repo)

    assert resolved.mode == "working_tree"
    assert resolved.base_rev == "HEAD"
    assert resolved.head_rev == "WORKDIR"
    assert resolved.dirty is True


def test_working_tree_snapshot_is_exact_and_excludes_ignored_state(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    (repo / "new.py").write_text("new = True\n", encoding="utf-8")
    (repo / "ignored.txt").write_text("secret cache\n", encoding="utf-8")
    resolved = resolve_review_range(repo, working_tree=True)
    base, head = tmp_path / "base", tmp_path / "head"

    _freeze_range(repo, resolved, base, head)

    assert (base / "a.py").read_text() == "value = 1\n"
    assert (head / "a.py").read_text() == "value = 2\n"
    assert (head / "new.py").read_text() == "new = True\n"
    assert not (head / "ignored.txt").exists()
    assert not (head / ".git").exists()


def test_staged_snapshot_does_not_include_unstaged_edit(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    (repo / "a.py").write_text("value = 3\n", encoding="utf-8")
    resolved = resolve_review_range(repo, staged=True)
    base, head = tmp_path / "base", tmp_path / "head"

    _freeze_range(repo, resolved, base, head)

    assert (base / "a.py").read_text() == "value = 1\n"
    assert (head / "a.py").read_text() == "value = 2\n"


def test_materialized_commit_is_the_exact_tree_and_leaves_the_index_alone(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / ".gitattributes").write_text("hidden.txt export-ignore\n", encoding="utf-8")
    (repo / "hidden.txt").write_text("kept\n", encoding="utf-8")
    (repo / "run.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (repo / "run.sh").chmod(0o755)
    (repo / "link.py").symlink_to("a.py")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "shapes")
    sha = _git(repo, "rev-parse", "HEAD")
    (repo / "a.py").write_text("value = 99\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    staged_before = _git(repo, "diff", "--cached", "--name-only")
    target = tmp_path / "frozen"

    _materialize_commit(repo, sha, target)

    assert (target / "a.py").read_text(encoding="utf-8") == "value = 1\n"
    assert (target / "hidden.txt").read_text(encoding="utf-8") == "kept\n"
    assert (target / "run.sh").stat().st_mode & 0o111
    assert (target / "link.py").is_symlink()
    assert (target / "link.py").readlink() == Path("a.py")
    assert _git(repo, "diff", "--cached", "--name-only") == staged_before == "a.py"


def test_clean_default_resolves_previous_commit_range(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "second")

    resolved = resolve_review_range(repo)

    assert resolved.mode == "commit_range"
    assert resolved.base_rev == "HEAD~1"
    assert resolved.head_rev == "HEAD"
    assert resolved.base_sha == _git(repo, "rev-parse", "HEAD~1")
    assert resolved.head_sha == _git(repo, "rev-parse", "HEAD")


def test_named_branch_range_keeps_a_stable_review_source_ref(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "branch", "base-branch")
    _git(repo, "checkout", "-q", "-b", "feature-branch")
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "feature")

    resolved = resolve_review_range(repo, "base-branch...feature-branch")

    assert resolved.source_ref(repo) == "named:refs/heads/base-branch...refs/heads/feature-branch"


def test_sha_and_relative_ranges_remain_snapshot_keyed(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-q", "-m", "second")
    resolved = resolve_review_range(repo, "HEAD~1..HEAD")

    assert resolved.source_ref(repo) == f"{resolved.base_sha}..{resolved.head_sha}"


def test_working_tree_snapshot_preserves_a_tracked_deletion(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").unlink()
    resolved = resolve_review_range(repo, working_tree=True)
    base, head = tmp_path / "base", tmp_path / "head"

    _freeze_range(repo, resolved, base, head)

    assert (base / "a.py").is_file()
    assert not (head / "a.py").exists()


def test_working_tree_gitlink_pointer_move_is_preserved(tmp_path: Path) -> None:
    sub = tmp_path / "sub-source"
    sub.mkdir()
    _git(sub, "init", "-q")
    _git(sub, "config", "user.email", "review@example.test")
    _git(sub, "config", "user.name", "Review Test")
    (sub / "value.txt").write_text("one\n", encoding="utf-8")
    _git(sub, "add", ".")
    _git(sub, "commit", "-q", "-m", "one")
    first = _git(sub, "rev-parse", "HEAD")
    (sub / "value.txt").write_text("two\n", encoding="utf-8")
    _git(sub, "commit", "-qam", "two")
    second = _git(sub, "rev-parse", "HEAD")
    _git(sub, "checkout", "-q", first)

    repo = _repo(tmp_path)
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "deps/sub")
    _git(repo, "commit", "-qam", "add submodule")
    _git(repo / "deps/sub", "checkout", "-q", second)

    resolved = resolve_review_range(repo, working_tree=True)
    base, head = _gitlinks_for_range(repo, resolved)

    assert base == {"deps/sub": first}
    assert head == {"deps/sub": second}


def test_lcr_entrypoint_dispatches_directly_to_review(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import sys

    monkeypatch.setattr(sys, "argv", ["/opt/lemoncrow/bin/lcr", "--help"])

    assert review_entrypoint() == 0
    output = capsys.readouterr().out
    assert "--working-tree" in output
    assert "--staged" in output


def test_review_capture_uses_review_owned_scratch_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_client import review as review_mod
    from lemoncrow_client.config import load_config

    repo = _repo(tmp_path)
    state_dir = tmp_path / "state"
    config = load_config(
        {"LEMONCROW_HOME": str(state_dir), "HOME": str(tmp_path)},
        cwd=repo,
    )
    observed: dict[str, object] = {}

    class StopAfterDirectoryChoice:
        def __init__(self, *, prefix: str, dir: Path) -> None:
            observed["prefix"] = prefix
            observed["dir"] = dir

        def __enter__(self) -> str:
            raise RuntimeError("stop after scratch-root selection")

        def __exit__(self, *args: object) -> bool:
            return False

    monkeypatch.setattr(review_mod.tempfile, "TemporaryDirectory", StopAfterDirectoryChoice)

    with pytest.raises(RuntimeError, match="scratch-root selection"):
        review_mod._capture(
            config,
            object(),  # type: ignore[arg-type]
            with_impact=False,
            restore_archived=False,
        )

    assert observed == {"prefix": "capture-", "dir": config.review_state_dir}
    assert config.review_state_dir.is_dir()
    assert not config.runtime_state_dir.exists()


def test_the_capture_branch_is_the_checked_out_branch_and_none_when_detached(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _git(repo, "switch", "-q", "-c", "feature/x")
    assert _workspace_branch(repo) == "feature/x"
    _git(repo, "checkout", "-q", "--detach")
    assert _workspace_branch(repo) == ""


class _Remote:
    """The one read ``_active_review_base`` makes of a ``ReviewSession``: the branch Review lookup."""

    def __init__(self, answer: Response | ClientError) -> None:
        self.answer = answer
        self.asked: list[tuple[str, str, str]] = []

    def branch_review(self, repo_id: str, *, branch: str, range_mode: str, timeout_s: float | None = None) -> Response:
        self.asked.append((repo_id, branch, range_mode))
        if isinstance(self.answer, ClientError):
            raise self.answer
        return self.answer


_OPEN = {
    "id": "r1",
    "repo_id": "repo_1",
    "range_mode": "working_tree",
    "status": "open",
    "review_base_sha": "a" * 40,
    "review_base_policy": "fixed",
}
_REFUSED = {"error": {"code": "internal", "message": "store unavailable", "retryable": False, "action": "none"}}


@pytest.mark.parametrize("hosted", [False, True], ids=["local", "hosted"])
@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        pytest.param(Response(200, {"review": _OPEN}), ("a" * 40, "fixed"), id="the branch's open Review"),
        pytest.param(Response(200, {"review": None}), ("", ""), id="no Review yet"),
        pytest.param(Response(200, {"review": {**_OPEN, "review_base_sha": ""}}), ("", "fixed"), id="no recorded base"),
    ],
)
def test_the_lookup_reads_the_base_of_the_branchs_open_review(
    answer: Response, expected: tuple[str, str], hosted: bool
) -> None:
    remote = _Remote(answer)
    found = _active_review_base(
        remote,  # type: ignore[arg-type]
        "repo_1",
        branch="main",
        range_mode="working_tree",
        hosted=hosted,
        timeout_s=1.0,
    )
    assert found == expected
    assert remote.asked == [("repo_1", "main", "working_tree")]


def test_a_local_server_without_the_lookup_refuses_instead_of_dropping_commits() -> None:
    """A branch-aware old server cannot answer the base lookup: the new client must fail closed."""

    remote = _Remote(Response(404, {"error": {"code": "review_unknown", "message": "not found"}}))
    with pytest.raises(ClientError) as raised:
        _active_review_base(
            remote,  # type: ignore[arg-type]
            "repo_1",
            branch="main",
            range_mode="working_tree",
            hosted=False,
            timeout_s=1.0,
        )
    assert raised.value.code is ErrorCode.CAPABILITY_UNSUPPORTED
    assert raised.value.action is AgentAction.UPGRADE_SERVER
    assert raised.value.retryable is True
    assert "nothing was captured" in raised.value.message


@pytest.mark.parametrize("hosted", [False, True], ids=["local", "hosted"])
@pytest.mark.parametrize(
    "answer",
    [
        pytest.param(Response(500, _REFUSED), id="the server refuses"),
        pytest.param(ClientError(ErrorCode.SERVER_UNREACHABLE, "connection reset", retryable=True), id="no answer"),
        pytest.param(Response(200, {}), id="no review field"),
        pytest.param(Response(200, {"review": "r1"}), id="not an object"),
        pytest.param(Response(200, {"review": {**_OPEN, "id": ""}}), id="no id"),
        pytest.param(Response(200, {"review": {**_OPEN, "status": "finished"}}), id="not open"),
        pytest.param(Response(200, {"review": {**_OPEN, "range_mode": "staged"}}), id="another range mode"),
        pytest.param(Response(200, {"review": {**_OPEN, "review_base_sha": None}}), id="base not a string"),
    ],
)
def test_a_served_lookup_that_fails_fails_the_capture(answer: Response | ClientError, hosted: bool) -> None:
    """Advertised but failed is not "not advertised": the capture fails, retryably, instead of using HEAD."""

    with pytest.raises(ClientError) as raised:
        _active_review_base(
            _Remote(answer),  # type: ignore[arg-type]
            "repo_1",
            branch="main",
            range_mode="working_tree",
            hosted=hosted,
            timeout_s=1.0,
        )
    error = raised.value
    assert error.retryable is True
    assert error.action is AgentAction.RETRY_LATER
    assert "branch 'main'" in error.message and "nothing was captured" in error.message
    assert error.details["lookup"] == BRANCH_REVIEW_PATH


def test_on_hosted_a_missing_lookup_fails_the_capture() -> None:
    """Hosted never advertised the branch without the lookup, so a 404 there is a failure, not an older server."""

    with pytest.raises(ClientError) as raised:
        _active_review_base(
            _Remote(Response(404, {"error": {"code": "review_unknown", "message": "not found"}})),  # type: ignore[arg-type]
            "repo_1",
            branch="main",
            range_mode="working_tree",
            hosted=True,
            timeout_s=1.0,
        )
    assert raised.value.retryable is True


def test_a_failed_lookup_keeps_guidance_a_retry_alone_cannot_satisfy() -> None:
    refused = ClientError(ErrorCode.UNAUTHENTICATED, "token expired", action=AgentAction.REAUTHENTICATE)
    with pytest.raises(ClientError) as raised:
        _active_review_base(
            _Remote(refused),  # type: ignore[arg-type]
            "repo_1",
            branch="main",
            range_mode="working_tree",
            hosted=True,
            timeout_s=1.0,
        )
    assert (raised.value.code, raised.value.action) == (ErrorCode.UNAUTHENTICATED, AgentAction.REAUTHENTICATE)


def test_the_policy_decides_whether_the_recorded_base_advances(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    recorded = _git(repo, "rev-parse", "HEAD")
    (repo / "b.py").write_text("pulled = True\n", encoding="utf-8")
    _git(repo, "add", "b.py")
    _git(repo, "commit", "-q", "-m", "integrated upstream")
    pulled = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", pulled)

    # integrated: work pulled from the integration branch moves the base past it.
    assert _review_base(repo, recorded, "integrated") == pulled
    assert _review_base(repo, recorded[:12], "") == pulled
    # fixed: the recorded base stays, as a full commit id.
    assert _review_base(repo, recorded[:12], "fixed") == recorded
    assert _review_base(repo, EMPTY_TREE_SHA, "fixed") == EMPTY_TREE_SHA
    # A base this clone lacks, or a spec that is no commit, is HEAD under either policy.
    for policy in ("integrated", "fixed"):
        assert _review_base(repo, "b" * 40, policy) == ""
        assert _review_base(repo, "--all", policy) == ""


def test_the_review_base_replaces_head_as_the_range_base(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "a.py").write_text("value = 2\n", encoding="utf-8")
    rng = resolve_review_range(repo, working_tree=True)

    assert _with_review_base(rng, "") is rng
    assert _with_review_base(rng, rng.base_sha) is rng
    rebased = _with_review_base(rng, "c" * 40)
    assert (rebased.base_rev, rebased.base_sha, rebased.head_rev) == ("c" * 40, "c" * 40, "WORKDIR")
    empty = _with_review_base(rng, EMPTY_TREE_SHA)
    assert (empty.base_rev, empty.base_sha) == ("(empty tree)", "")
