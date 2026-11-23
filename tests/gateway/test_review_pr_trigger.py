"""The agent opens a pull request: it is reviewed in the chat, saved on its Review, answered by number."""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from click.testing import CliRunner
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.review_findings import GitHubError, finding_main
from lemoncrow_client.review_terminal import AGENT_SESSION_ENVS

from lemoncrow.gateway.cli.commands.review_finding_cli import report_findings_cmd
from lemoncrow.gateway.cli.commands.review_record_cli import REVIEW_SUBCOMMANDS
from lemoncrow.pro.capabilities.review import hosted, pr_trigger, reported_findings
from lemoncrow.pro.capabilities.review.sources import github

REPO_ROOT = Path(__file__).resolve().parents[2]
CLAUDE_HOOKS = REPO_ROOT / "integrations" / "claude" / "plugin" / "hooks"
CODEX_HOOKS = REPO_ROOT / "integrations" / "codex" / "hooks"
PR_URL = "https://github.com/acme/shop/pull/12"
SID = "claude-session"
PRICE = {
    "file": "app.py",
    "line": 2,
    "summary": "price() returns 2 without validating the caller's tier.",
    "short_summary": "Unvalidated price",
    "severity": "important",
}
TAX = {"file": "tax.py", "line": 1, "summary": "tax() ignores the region.", "short_summary": "Region ignored"}
URL = "http://127.0.0.1:1/r/t"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str) -> str:
    (repo / name).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(tmp_path: Path, *, remote: bool = True) -> tuple[Path, str, str]:
    """A clone on `feature`, one commit past origin/main: (checkout, merge base, head)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    base = _commit(repo, "app.py", "def price():\n    return 1\n", "base")
    if remote:
        _git(repo, "update-ref", "refs/remotes/origin/main", base)
        _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _git(repo, "checkout", "-qb", "feature")
    head = _commit(repo, "app.py", "def price():\n    return 2\n", "price is 2")
    return repo.resolve(), base, head


def _created(command: str = "gh pr create --base main --title 'Fix price' --body x", **response: Any) -> dict[str, Any]:
    return {
        "session_id": SID,
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": response or {"stdout": f"Creating pull request\n{PR_URL}\n", "stderr": ""},
    }


def _report(repo: Path, *findings: dict[str, Any], tool_use_id: str = "toolu_1") -> dict[str, Any]:
    return {
        "session_id": SID,
        "cwd": str(repo),
        "tool_name": "ReportFindings",
        "tool_use_id": tool_use_id,
        "tool_input": {"findings": list(findings)},
    }


def _load(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def test_only_a_successful_pr_creation_that_printed_its_url_counts() -> None:
    opened = pr_trigger.opened_pull_request(
        _created("git push -u origin feature && " + _created()["tool_input"]["command"])
    )
    assert opened == pr_trigger.OpenedPullRequest(
        url=PR_URL, github_host="github.com", slug="acme/shop", number=12, base_ref="main", title="Fix price"
    )
    # A failed create: non-zero exit, or the "already exists" URL on standard error only.
    assert pr_trigger.opened_pull_request(_created(stdout=PR_URL, returnCode=1)) is None
    assert pr_trigger.opened_pull_request(_created(stdout=PR_URL, exit_code=1)) is None
    already = f'a pull request for branch "feature" into branch "main" already exists:\n{PR_URL}\n'
    assert pr_trigger.opened_pull_request(_created(stdout="", stderr=already)) is None
    assert pr_trigger.opened_pull_request(_created(stdout=PR_URL, interrupted=True)) is None
    # Another gh command that prints a pull request URL is not a creation.
    assert pr_trigger.opened_pull_request(_created("gh pr view 12 --json url", stdout=PR_URL)) is None
    assert pr_trigger.opened_pull_request({**_created(), "tool_name": "Read"}) is None

    # lc's MCP shell: a batch, each command's output ending in `[exit N]`.
    batch = {
        "tool_name": "mcp__lc__bash",
        "tool_input": {"command": ["git push -u origin feature", "gh pr create --fill --head feature"]},
        "tool_response": [{"type": "text", "text": f"pushed\n[exit 0]\n{PR_URL}\n[exit 0]"}],
    }
    found = pr_trigger.opened_pull_request(batch)
    assert found is not None and (found.number, found.head_ref, found.base_ref) == (12, "feature", "")
    batch["tool_response"] = [{"type": "text", "text": f"{PR_URL}\n[exit 1]"}]
    assert pr_trigger.opened_pull_request(batch) is None

    # GitHub's MCP server.
    mcp = {
        "tool_name": "mcp__github__create_pull_request",
        "tool_input": {"owner": "acme", "repo": "shop", "base": "main", "head": "acme:feature", "title": "T"},
        "tool_response": {"content": [{"type": "text", "text": json.dumps({"html_url": f"{PR_URL[:-2]}13"})}]},
    }
    found = pr_trigger.opened_pull_request(mcp)
    assert found is not None and (found.number, found.base_ref, found.head_ref) == (13, "main", "feature")
    assert pr_trigger.opened_pull_request({**mcp, "tool_response": {**mcp["tool_response"], "isError": True}}) is None

    # Codex: an argv command and either response shape.
    codex = {"tool_name": "exec_command", "tool_input": {"command": ["bash", "-lc", "gh pr create --fill"]}}
    assert pr_trigger.opened_pull_request({**codex, "tool_response": {"stdout": PR_URL, "exit_code": 0}})
    ok = f"Process exited with code 0\nOutput:\n{PR_URL}\n"
    assert pr_trigger.opened_pull_request({**codex, "tool_response": ok})
    failed = f"Process exited with code 1\nOutput:\n{PR_URL}\n"
    assert pr_trigger.opened_pull_request({**codex, "tool_response": failed}) is None


# --------------------------------------------------------------------------- #
# State: once per head commit, claimed by the next report, pruned
# --------------------------------------------------------------------------- #


def test_a_head_commit_triggers_once_and_old_entries_are_dropped(tmp_path: Path) -> None:
    repo, _base, head = _repo(tmp_path)
    root = tmp_path / "store"
    opened = pr_trigger.opened_pull_request(_created())
    assert opened is not None
    first = pr_trigger.record_pr_trigger(root, opened, host="claude", session_id=SID, cwd=repo)
    assert first is not None and (first.head_sha, first.base_ref, first.repo_root) == (head, "main", str(repo))
    assert first.source_ref == "github:acme/shop#12"
    assert pr_trigger.record_pr_trigger(root, opened, host="claude", session_id=SID, cwd=repo) is None
    # A new head commit on the same pull request is reviewed again.
    _commit(repo, "app.py", "def price():\n    return 3\n", "price is 3")
    assert pr_trigger.record_pr_trigger(root, opened, host="claude", session_id=SID, cwd=repo) is not None
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert pr_trigger.record_pr_trigger(root, opened, host="claude", session_id=SID, cwd=outside) is None

    state = pr_trigger.state_path(root)
    data = json.loads(state.read_text())
    data["pull_requests"][0]["created_at"] = "2000-01-01T00:00:00+00:00"
    state.write_text(json.dumps(data))
    # Any write drops what is past retention; the current head is still known, whoever asks.
    assert pr_trigger.record_pr_trigger(root, opened, host="claude", session_id="other", cwd=repo) is None
    [kept] = json.loads(state.read_text())["pull_requests"]
    assert kept["created_at"] > "2001" and kept["head_sha"] != head


def test_only_the_same_sessions_report_on_the_pr_head_claims_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, _head = _repo(tmp_path)
    root = tmp_path / "store"
    opened = pr_trigger.opened_pull_request(_created())
    assert opened is not None
    assert pr_trigger.claim_pull_request(root, host="claude", session_id=SID, cwd=repo) is None
    pr_trigger.record_pr_trigger(root, opened, host="claude", session_id=SID, cwd=repo)
    assert pr_trigger.claim_pull_request(root, host="claude", session_id="another", cwd=repo) is None
    assert pr_trigger.claim_pull_request(root, host="codex", session_id=SID, cwd=repo) is None
    claimed = pr_trigger.claim_pull_request(root, host="claude", session_id=SID, cwd=repo)
    assert claimed is not None and claimed.number == 12 and claimed.claimed_at
    # Claimed once: the next review in this session is an ordinary one.
    assert pr_trigger.claim_pull_request(root, host="claude", session_id=SID, cwd=repo) is None

    # New commits after the pull request opened: the review is of them, not of the pull request.
    _commit(repo, "app.py", "def price():\n    return 3\n", "price is 3")
    pr_trigger.record_pr_trigger(root, opened, host="claude", session_id=SID, cwd=repo)
    _commit(repo, "app.py", "def price():\n    return 4\n", "price is 4")
    assert pr_trigger.claim_pull_request(root, host="claude", session_id=SID, cwd=repo) is None

    # Too long after it opened.
    pr_trigger.record_pr_trigger(root, opened, host="claude", session_id=SID, cwd=repo)
    later = pr_trigger._now() + pr_trigger.CLAIM_WINDOW + timedelta(minutes=1)
    monkeypatch.setattr(pr_trigger, "_now", lambda: later)
    assert pr_trigger.claim_pull_request(root, host="claude", session_id=SID, cwd=repo) is None


# --------------------------------------------------------------------------- #
# The Claude hooks
# --------------------------------------------------------------------------- #


def _run(hook: Any, payload: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hook.main() == 0
    return capsys.readouterr().out


def test_the_pr_opened_hook_asks_claude_to_review_it_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    groups = json.loads((CLAUDE_HOOKS / "hooks.json").read_text())["hooks"]["PostToolUse"]
    [wired] = [group for group in groups if "review_pr_opened.py" in json.dumps(group)]
    assert {"Bash", "mcp__.*__bash", "mcp__.*__create_pull_request"} == set(wired["matcher"].split("|"))
    assert wired["hooks"][0]["timeout"] <= 10

    repo, _base, _head = _repo(tmp_path)
    hook = _load(CLAUDE_HOOKS / "review_pr_opened.py", "review_pr_opened_hook")
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "store"))
    monkeypatch.delenv("LEMONCROW_IN_REVIEW", raising=False)
    monkeypatch.setattr(hook, "_dormant", lambda: False)
    monkeypatch.setattr(pr_trigger, "local_review_configured", lambda _cwd: True)
    created = {**_created(), "cwd": str(repo)}

    out = json.loads(_run(hook, created, monkeypatch, capsys))
    assert out["systemMessage"] == "lc review: reviewing PR #12…"
    context = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    # lc review posts the findings itself, numbered: Claude's skill does not post them too.
    assert 'skill "code-review" and args "12".' in context and PR_URL in context and "--comment" not in context
    assert "posts them on the pull request as one review" in context
    assert "Do not post on the pull request yourself" in context
    assert "Never put an lc review link in anything posted to GitHub" in context
    assert "127.0.0.1" not in context and "localhost" not in context
    # A background review's hook cannot add context here, so what to do with the findings comes now:
    # fix the ones Claude agrees with without waiting, ask one line before skipping one.
    assert "fix them without waiting for the user" in context and "commit, push to pull request #12" in context
    assert '`lc review finding N fixed --run "<test command>"`' in context
    assert '`Skip N? <one-line reason>. Reply "skip N" or "fix N".`' in context
    assert "`lc review finding N dismissed --reason <incorrect|accepted_risk|duplicate|not_applicable>" in context
    assert "confirmed" not in context and 'reply "fix' not in context
    # The same head commit never triggers twice; a failed create never triggers.
    assert _run(hook, created, monkeypatch, capsys) == ""
    _commit(repo, "app.py", "x = 1\n", "more")
    failed = {**_created(stdout=PR_URL, returnCode=1), "cwd": str(repo)}
    assert _run(hook, failed, monkeypatch, capsys) == ""
    # Silent inside an lc review run, when dormant, with a hosted configuration, outside a checkout.
    monkeypatch.setenv("LEMONCROW_IN_REVIEW", "1")
    assert _run(hook, created, monkeypatch, capsys) == ""
    monkeypatch.delenv("LEMONCROW_IN_REVIEW")
    monkeypatch.setattr(hook, "_dormant", lambda: True)
    assert _run(hook, created, monkeypatch, capsys) == ""
    monkeypatch.setattr(hook, "_dormant", lambda: False)
    monkeypatch.setattr(pr_trigger, "local_review_configured", lambda _cwd: False)
    assert _run(hook, created, monkeypatch, capsys) == ""
    monkeypatch.setattr(pr_trigger, "local_review_configured", lambda _cwd: True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    assert _run(hook, {**created, "cwd": str(outside)}, monkeypatch, capsys) == ""
    assert _run(hook, "not an object", monkeypatch, capsys) == ""


def test_the_report_on_the_pr_says_where_and_hands_claude_the_numbering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, _base, _head = _repo(tmp_path)
    root = tmp_path / "store"
    opened = pr_trigger.opened_pull_request(_created())
    assert opened is not None
    pr_trigger.record_pr_trigger(root, opened, host="claude", session_id=SID, cwd=repo)
    hook = _load(CLAUDE_HOOKS / "review_findings.py", "review_findings_hook_pr")
    monkeypatch.setenv("LEMONCROW_ROOT", str(root))
    monkeypatch.delenv("LEMONCROW_IN_REVIEW", raising=False)
    monkeypatch.setattr(hook, "_dormant", lambda: False)
    named: list[Any] = []
    spawned: list[dict[str, Any]] = []

    def url(payload: dict[str, Any], **_kwargs: Any) -> str:
        named.append(payload.get("pull_request"))
        return URL

    monkeypatch.setattr(reported_findings, "pull_request_review_url", url)
    monkeypatch.setattr(
        reported_findings, "spawn_reported_findings", lambda payload, *, host, store_root: spawned.append(payload)
    )

    out = json.loads(_run(hook, _report(repo, PRICE, TAX), monkeypatch, capsys))
    assert out["systemMessage"] == (
        f"lc review: 2 problems found on PR #12. Claude is fixing them; details are on the PR.\n"
        f"Also in lc review: {URL}"
    )
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    context = out["hookSpecificOutput"]["additionalContext"]
    assert "1. Unvalidated price (app.py:2)\n2. Region ignored (tax.py:1)" in context
    assert "Fix them now, without waiting for the user:" in context
    assert (
        '- Each finding you agree with: fix it, commit, push to the pull request, then record it in one step with `lc review finding N fixed --run "<test command>"`.'
        in context
    )
    assert 'Ask the user one short line, `Skip N? <one-line reason>. Reply "skip N" or "fix N".`' in context
    assert "--reason <incorrect|accepted_risk|duplicate|not_applicable>" in context
    assert "confirmed" not in context
    assert "answers and resolves its comment on the pull request" in context
    assert "Do not post on the pull request yourself" in context
    assert "Never put an lc review link in anything posted to GitHub" in context
    [pull] = named
    assert pull["number"] == 12 and spawned[0]["pull_request"] == pull
    [kept] = json.loads(reported_findings.reports_path(root).read_text())["reports"]
    assert (kept["status"], kept["branch"], kept["repo_root"]) == ("saving", "refs/heads/feature", str(repo))
    assert [(row["number"], row["title"]) for row in kept["findings"]] == [
        (1, "Unvalidated price"),
        (2, "Region ignored"),
    ]

    # The pull request was claimed: a later code review in this session answers no pull request, and lc review
    # leaves it as Claude reported it. The pull request's numbering stays.
    assert _run(hook, _report(repo, PRICE, tool_use_id="toolu_2"), monkeypatch, capsys) == ""
    assert len(named) == 1 and len(spawned) == 1
    [kept] = json.loads(reported_findings.reports_path(root).read_text())["reports"]
    assert len(kept["findings"]) == 2 and kept["pull_request"]["number"] == 12


def test_the_chat_line_in_plain_words(tmp_path: Path) -> None:
    rows = [{"title": "Unvalidated price", "path": "app.py", "line": 2}, {"title": "Region ignored", "path": "tax.py"}]
    assert reported_findings.report_message([], URL, pr_number=12) == (
        f"lc review: no problems found on PR #12.\nAlso in lc review: {URL}"
    )
    # A forked review's own output never reached the user: the line lists it. The Review may still be opening.
    assert reported_findings.report_message(rows, "", pr_number=12, listed=True) == (
        "lc review: 2 problems found on PR #12. Claude is fixing them; details are on the PR.\n"
        "1. Unvalidated price (app.py:2)\n"
        "2. Region ignored (tax.py)"
    )
    assert reported_findings.report_message(rows[:1], URL, pr_number=12, agent="Codex") == (
        f"lc review: 1 problem found on PR #12. Codex is fixing it; details are on the PR.\nAlso in lc review: {URL}"
    )
    assert [reported_findings.agent_name(host) for host in ("claude", "codex", "cursor", "")] == [
        "Claude",
        "Codex",
        "Cursor",
        "The agent",
    ]
    repo, _base, _head = _repo(tmp_path)
    opened = pr_trigger.opened_pull_request(_created())
    assert opened is not None
    pending = pr_trigger.pending_pull_request(opened, host="claude", session_id=SID, cwd=repo)
    assert pending is not None
    assert reported_findings.report_context([], URL, pull_request=pending).endswith("no findings. Nothing to do.")


# --------------------------------------------------------------------------- #
# The worker: the pull request's range, on its Review, numbered
# --------------------------------------------------------------------------- #


def _fake_server(
    monkeypatch: pytest.MonkeyPatch, *, review_id: str = "rev1", revision_id: str = "rv1"
) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def capture(_config: Any, _repo: Path, rng: Any, **kwargs: Any) -> Any:
        calls.append(("capture", rng, kwargs))
        return SimpleNamespace(
            review_id=review_id,
            canonical_url="http://127.0.0.1:1/r/pr",
            response={"revision": {"id": revision_id, "revision_number": 0}},
        )

    monkeypatch.setattr(hosted, "capture_server_review", capture)
    monkeypatch.setattr(
        hosted, "server_review_request", lambda _config, method, path, **_kw: calls.append((method, path)) or {}
    )
    return calls


def test_a_report_on_the_pr_captures_merge_base_to_head_on_the_prs_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = _repo(tmp_path)
    root = tmp_path / "store"
    (repo / "app.py").write_text("uncommitted = True\n")  # never part of the pull request
    opened = pr_trigger.opened_pull_request(_created())
    assert opened is not None
    pending = pr_trigger.record_pr_trigger(root, opened, host="claude", session_id=SID, cwd=repo)
    assert pending is not None

    def no_gh(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("local Git knew the range; gh must not run")

    monkeypatch.setattr(github, "resolve_pull_request", no_gh)
    calls = _fake_server(monkeypatch)
    posted = {"review_id": 77, "url": f"{PR_URL}#pullrequestreview-77", "marker": "<!-- m -->", "error": ""}

    def post(_root: Path, pull: Any, rows: Any, *, host: str, runner: Any) -> Any:
        calls.append(("post", pull.number, [row["title"] for row in rows], host))
        return posted, {rows[0]["key"]: 501}

    monkeypatch.setattr(reported_findings, "post_pull_request_review", post)
    report = {**_report(repo, PRICE), "pull_request": pending.to_wire()}
    reported_findings.note_report(report, host="claude", store_root=root, url="")
    result = reported_findings.record_reported_findings(
        report,
        host="claude",
        store_root=root,
        config=SimpleNamespace(hosted=False),  # type: ignore[arg-type]
    )
    assert result is not None and result.review_id == "rev1"
    # On the pull request first, before the capture: the agent's first fix would move the head away.
    [first, (_, rng, kwargs), *_requests] = calls
    assert first == ("post", 12, ["Unvalidated price"], "claude")
    assert (rng.mode, rng.base_sha, rng.head_sha) == ("commit_range", base, head)
    assert (kwargs["source_ref_override"], kwargs["title_override"]) == ("github:acme/shop#12", "PR #12: Fix price")
    assert rng.title == "PR #12: Fix price"

    # The numbered report now names the Review and each number's AI finding.
    from lemoncrow.core.capabilities.session_comment_capture import reported_findings_rows
    from lemoncrow.pro.capabilities.review.session_comments import reported_annotation_id

    [kept] = json.loads(reported_findings.reports_path(root).read_text())["reports"]
    [row] = reported_findings_rows([PRICE])
    assert (kept["status"], kept["review_id"], kept["url"]) == ("saved", "rev1", "http://127.0.0.1:1/r/pr")
    assert kept["findings"][0]["annotation_id"] == reported_annotation_id("rev1", "claude", SID, row["key"])
    # ... and where it was posted, so each answer goes to its comment.
    assert (kept["github"], kept["findings"][0]["comment_id"]) == (posted, 501)

    # A report that answers no pull request is not saved, posted or captured.
    calls.clear()
    plain = reported_findings.record_reported_findings(
        _report(repo, PRICE),
        host="claude",
        store_root=root,
        config=SimpleNamespace(hosted=False),  # type: ignore[arg-type]
    )
    assert plain is None and not calls


class _PullRequestOnGitHub:
    """GitHub as the worker's `gh` sees pull request #12: its head, its one changed file, the reviews posted."""

    def __init__(self, head: str) -> None:
        self.head = head
        self.reviews: list[dict[str, Any]] = []
        self.posted: list[dict[str, Any]] = []

    def __call__(self, args: Sequence[str], stdin: str | None) -> str:
        api = "repos/acme/shop/pulls/12"
        if list(args) == ["api", api]:
            return json.dumps(
                {"head": {"sha": self.head}, "user": {"login": "pk"}, "state": "open", "html_url": PR_URL}
            )
        if list(args) == ["api", "user"]:
            return json.dumps({"login": "pk"})
        if args[-1] == f"{api}/files?per_page=100":
            patch = "@@ -1,2 +1,2 @@\n def price():\n-    return 1\n+    return 2"
            return json.dumps([[{"filename": "app.py", "patch": patch}]])
        if args[-1] == f"{api}/reviews?per_page=100":
            return json.dumps([self.reviews])
        if list(args) == ["api", "--method", "POST", f"{api}/reviews", "--input", "-"]:
            payload = json.loads(stdin or "{}")
            self.posted.append(payload)
            review = {"id": 77, "body": payload["body"], "user": {"login": "pk"}, "html_url": f"{PR_URL}#r77"}
            self.reviews.append({**review, "commit_id": payload["commit_id"], "state": "COMMENTED"})
            return json.dumps(review)
        if args[-1] == f"{api}/reviews/77/comments?per_page=100":
            comments = self.posted[0]["comments"]
            return json.dumps([[{**comment, "id": 500 + n} for n, comment in enumerate(comments, 1)]])
        raise AssertionError(f"unexpected gh call: {args}")


def test_the_worker_posts_one_numbered_review_on_the_pr(tmp_path: Path) -> None:
    from lemoncrow.core.capabilities.session_comment_capture import reported_findings_rows

    repo, _base, head = _repo(tmp_path)
    opened = pr_trigger.opened_pull_request(_created())
    assert opened is not None
    pending = pr_trigger.pending_pull_request(opened, host="claude", session_id=SID, cwd=repo)
    assert pending is not None and pending.head_sha == head
    rows = reported_findings_rows([PRICE, TAX])
    on_github = _PullRequestOnGitHub(head)

    fields, comments = reported_findings.post_pull_request_review(repo, pending, rows, host="claude", runner=on_github)
    assert (fields["review_id"], fields["url"], fields["error"]) == (77, f"{PR_URL}#r77", "")
    assert fields["marker"].startswith("<!-- lemoncrow:op=report-") and comments == {rows[0]["key"]: 501}
    [payload] = on_github.posted
    assert (payload["commit_id"], payload["event"]) == (head, "COMMENT")
    # Finding 1 sits on a changed line: its own comment, numbered. Finding 2 (tax.py is not in the pull
    # request) is quoted in the review's list, the one place it shows.
    [inline] = payload["comments"]
    assert (inline["path"], inline["line"], inline["side"]) == ("app.py", 2, "RIGHT")
    assert inline["body"].startswith("**1. Unvalidated price**\n\nprice() returns 2")
    assert payload["body"].startswith("**Claude's code review: 2 findings.** Claude fixes each one it agrees with")
    assert "\n1. **Open** · Unvalidated price `app.py:2`\n2. **Open** · Region ignored `tax.py:1`\n" in payload["body"]
    assert "   > tax() ignores the region." in payload["body"] and payload["body"].endswith(fields["marker"])
    assert "127.0.0.1" not in json.dumps(payload) and "localhost" not in json.dumps(payload)

    # A retry adopts the review the first attempt made: nothing is posted twice.
    again, _comments = reported_findings.post_pull_request_review(repo, pending, rows, host="claude", runner=on_github)
    assert again == fields and len(on_github.posted) == 1

    # The pull request moved on, or gh is missing: the error is recorded, never raised.
    moved, none = reported_findings.post_pull_request_review(
        repo, pending, rows, host="claude", runner=_PullRequestOnGitHub("f" * 40)
    )
    assert (moved["review_id"], none) == (0, {}) and "has new commits" in moved["error"]

    def no_gh(_args: Any, _stdin: Any) -> str:
        raise github.GitHubCliError("`gh` is not signed in; run `gh auth login`")

    signed_out, _none = reported_findings.post_pull_request_review(repo, pending, rows, host="claude", runner=no_gh)
    assert signed_out["error"] == "`gh` is not signed in; run `gh auth login`"


def test_without_the_base_locally_the_range_comes_from_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, base, head = _repo(tmp_path, remote=False)
    opened = pr_trigger.opened_pull_request(_created())
    assert opened is not None
    pending = pr_trigger.pending_pull_request(opened, host="claude", session_id=SID, cwd=repo)
    assert pending is not None
    asked: list[str] = []

    def gh(_root: Path, spec: str) -> Any:
        asked.append(spec)
        return SimpleNamespace(head_sha=head, merge_base_sha=base, review_title="PR #12: From GitHub")

    monkeypatch.setattr(github, "resolve_pull_request", gh)
    rng, title = reported_findings.pull_request_range(repo, pending)
    assert asked == [PR_URL] and (rng.base_sha, rng.head_sha, title) == (base, head, "PR #12: From GitHub")


def test_a_failed_save_is_what_lc_review_finding_reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _base, _head = _repo(tmp_path)
    root = tmp_path / "store"
    report = _report(repo, PRICE)
    reported_findings.note_report(report, host="claude", store_root=root, url="")

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("the Review server is not running")

    monkeypatch.setattr(reported_findings, "record_reported_findings", boom)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(report)))
    assert reported_findings.main(["--host", "claude", "--store-root", str(root)]) == 1
    sink = io.StringIO()
    assert finding_main(["1", "confirmed", "--repo-root", str(repo)], sink=sink, store_root=root) == 1
    assert sink.getvalue() == "lc review could not save that code review: the Review server is not running\n"


# --------------------------------------------------------------------------- #
# lc review finding: the answer given in chat, recorded on the Review
# --------------------------------------------------------------------------- #


class _Server:
    """The Reader API calls `lc review finding` makes, answered and recorded."""

    def __init__(self, *, evidence: list[dict[str, Any]] | None = None, draft: bool = False) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.evidence = evidence or []
        self.draft = draft

    def __call__(self, method: str, path: str, body: Any) -> dict[str, Any]:
        self.calls.append((method, path, body))
        if path == "/api/reviews/rev1/annotations" and method == "GET":
            return {
                "annotations": [
                    {
                        "id": "ann-1",
                        "path": "app.py",
                        "start_line": 2,
                        "end_line": 2,
                        "title": "Unvalidated price",
                        "body": "price() returns 2 without validating the caller's tier.",
                        "source": "ai_review",
                        "finding_disposition": "",
                    },
                    {"id": "dsc-2", "path": "", "title": "Elsewhere", "body": "x"},
                ]
            }
        if path == "/api/reviews/rev1":
            return {"revision": {"id": "rv9", "revision_number": 3}, "session": {"source_ref": "github:acme/shop#12"}}
        if path == "/api/reviews/rev1/evidence":
            return {"evidence": self.evidence}
        if method == "PATCH" and self.draft:
            self.draft = False
            raise ClientError(ErrorCode.UNKNOWN, "Running drafts are read-only; create a revision before commenting")
        return {}


def _saved(tmp_path: Path, repo: Path, **overrides: Any) -> Path:
    root = tmp_path / "store"
    entry = {
        "source_id": "toolu_1",
        "host": "claude",
        "session_id": SID,
        "repo_root": str(repo),
        "branch": "refs/heads/feature",
        "url": URL,
        "pull_request": {"number": 12, "url": PR_URL},
        "status": "saved",
        "review_id": "rev1",
        "revision_id": "rv1",
        "error": "",
        "created_at": "2026-11-23T00:00:00+00:00",
        "findings": [
            {"number": 1, "title": "Unvalidated price", "path": "app.py", "line": 2, "annotation_id": "ann-1"},
            {"number": 2, "title": "Elsewhere", "path": "billing.py", "line": 0, "annotation_id": "ann-2"},
        ],
        **overrides,
    }
    path = reported_findings.reports_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "reports": [entry]}))
    return root


def _finding(
    arguments: list[str],
    repo: Path,
    root: Path,
    server: _Server,
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent_env: str = "",
    **kwargs: Any,
) -> tuple[int, str]:
    """``lc review finding ARGUMENTS`` from a person's shell, or from an agent's when ``agent_env`` names its variable."""
    for name, _host in AGENT_SESSION_ENVS:
        monkeypatch.delenv(name, raising=False)
    if agent_env:
        monkeypatch.setenv(agent_env, "1")
    monkeypatch.setattr("lemoncrow_client.review_terminal.session_request", lambda _config: server)
    sink = io.StringIO()
    code = finding_main(
        [*arguments, "--repo-root", str(repo)],
        sink=sink,
        store_root=root,
        load_config=lambda **_kwargs: SimpleNamespace(hosted=False),
        **kwargs,
    )
    return code, sink.getvalue()


def _patches(server: _Server) -> list[tuple[str, Any]]:
    return [(path, body) for method, path, body in server.calls if method in {"PATCH", "POST"}]


def test_fix_and_skip_answers_become_finding_decisions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _base, _head = _repo(tmp_path)
    root = _saved(tmp_path, repo)

    server = _Server()
    code, out = _finding(["1", "confirmed"], repo, root, server, monkeypatch)
    assert (code, out) == (0, "Finding 1 is a real problem (confirmed): Unvalidated price\n")
    assert _patches(server) == [
        (
            "/api/annotations/ann-1",
            {
                "finding_disposition": "confirmed_by_reviewer",
                "finding_disposition_reason": "",
                "finding_disposition_evidence": [],
                "expected_revision_id": "rv9",
            },
        )
    ]

    server = _Server()
    code, out = _finding(
        ["1", "dismissed", "--reason", "accepted_risk", "--note", "intended"], repo, root, server, monkeypatch
    )
    assert code == 0 and out.startswith("Finding 1 is skipped (a risk the author accepts): Unvalidated price")
    [(patched, decision), (replied, reply)] = _patches(server)
    assert patched == "/api/annotations/ann-1" and decision["finding_disposition_reason"] == "accepted_risk"
    assert replied == "/api/reviews/rev1/annotations"
    assert (reply["parent_id"], reply["body"], reply["range"], reply["file_level"]) == (
        "ann-1",
        "intended",
        {"start_line": 2, "end_line": 2},
        False,
    )

    # A finding kept as a Review comment has no decision; nothing is written for any number named with it.
    server = _Server()
    code, out = _finding(["1", "2", "confirmed"], repo, root, server, monkeypatch)
    assert code == 1 and "finding 2 is not on a line of this change" in out and not _patches(server)
    code, out = _finding(["5", "confirmed"], repo, root, _Server(), monkeypatch)
    assert (code, out) == (1, "there is no finding 5; the last code review has 2 (1-2)\n")

    # Usage errors write nothing either.
    for wrong in (["1", "dismissed"], ["1", "confirmed", "--reason", "incorrect"], ["confirmed"], ["one"]):
        code, _out = _finding(wrong, repo, root, _Server(), monkeypatch)
        assert code == 2, wrong


def test_fixed_needs_a_passing_check_on_the_latest_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _base, _head = _repo(tmp_path)
    root = _saved(tmp_path, repo)
    stale = {"id": "ev0", "revision_id": "rv1", "kind": "verification", "verification_status": "PASS"}
    failing = {"id": "ev1", "revision_id": "rv9", "kind": "verification", "verification_status": "FAIL"}
    server = _Server(evidence=[stale, failing])
    code, out = _finding(["1", "fixed"], repo, root, server, monkeypatch)
    assert code == 1 and not _patches(server)
    assert 'Commit the fix and push it, then run `lc review finding 1 fixed --run "<test command>"`' in out

    passing = {"id": "ev2", "revision_id": "rv9", "kind": "verification", "verification_status": "PASS"}
    # The latest Snapshot is a running draft: it is saved first, as the Reader's button would.
    server = _Server(evidence=[stale, failing, passing], draft=True)
    code, out = _finding(["1", "fixed"], repo, root, server, monkeypatch)
    assert (code, out.splitlines()[0]) == (0, "Finding 1 is fixed: Unvalidated price")
    assert _patches(server) == [
        ("/api/annotations/ann-1", server.calls[-1][2]),
        ("/api/reviews/rev1/revisions/3/publish", {}),
        ("/api/annotations/ann-1", server.calls[-1][2]),
    ]
    assert server.calls[-1][2]["finding_disposition_evidence"] == ["ev2"]


def test_a_decision_from_an_agents_shell_is_the_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _base, _head = _repo(tmp_path)
    root = _saved(tmp_path, repo)
    server = _Server()
    arguments = ["1", "dismissed", "--reason", "incorrect", "--json"]
    code, out = _finding(arguments, repo, root, server, monkeypatch, agent_env="CLAUDECODE")
    assert code == 0
    [(patched, decision)] = _patches(server)
    assert patched == "/api/annotations/ann-1"
    assert (decision["actor"], decision["agent_host"], decision["finding_disposition"]) == (
        "agent",
        "claude",
        "dismissed",
    )
    assert [item["actor"] for item in json.loads(out)["decisions"]] == ["agent"]

    server = _Server()
    code, out = _finding(["1", "open", "--json"], repo, root, server, monkeypatch, agent_env="CODEX_SESSION_ID")
    assert code == 0 and _patches(server)[0][1]["agent_host"] == "codex"
    # Confirming, and a reply in the reviewer's name, stay the reviewer's: refused before anything is sent.
    for theirs, said in (
        (["1", "confirmed"], "confirming a finding is the reviewer's call"),
        (["1", "dismissed", "--reason", "incorrect", "--note", "upstream"], "--note adds a reply in the reviewer"),
    ):
        server = _Server()
        code, out = _finding(theirs, repo, root, server, monkeypatch, agent_env="CLAUDECODE")
        assert code == 2 and out.startswith(said) and not server.calls, theirs
    # No flag lets an agent's shell record the decision as the person's.
    for claim in (["--actor", "human"], ["--actor=human"]):
        server = _Server()
        code, _out = _finding(["1", "fixed", *claim], repo, root, server, monkeypatch, agent_env="CLAUDECODE")
        assert code == 2 and not _patches(server)
    # A person's shell sends no actor: the server records the reviewer.
    server = _Server()
    assert _finding(["1", "confirmed"], repo, root, server, monkeypatch)[0] == 0
    assert "actor" not in _patches(server)[0][1]


class _Check:
    """The capture and the command `fixed --run` uses: one Snapshot, one run, both recorded."""

    def __init__(self, *, exit_code: int = 0, review_id: str = "rev1", revision_id: str = "rv9") -> None:
        self.captured: list[tuple[Any, str]] = []
        self.argv: list[list[str]] = []
        self.exit_code = exit_code
        self.response = {"review": {"id": review_id}, "revision": {"id": revision_id, "revision_number": 3}}
        self.output = "".join(f"tests/test_app.py::test_{n} PASSED\n" for n in range(60)) + "E   assert 2 == 1\n"

    def capture(self, _config: Any, rng: Any, *, source_ref: str) -> dict[str, Any]:
        self.captured.append((rng, source_ref))
        return self.response

    def __call__(self, argv: Any, cwd: Path) -> tuple[int, str]:
        self.argv.append(list(argv))
        return self.exit_code, self.output


class _CheckServer(_Server):
    def __call__(self, method: str, path: str, body: Any) -> dict[str, Any]:
        answer = super().__call__(method, path, body)
        return {"evidence": {"id": "ev7"}} if path.endswith("/verification") else answer


def _run_fixed(
    arguments: list[str], repo: Path, root: Path, monkeypatch: pytest.MonkeyPatch, check: _Check, **kwargs: Any
) -> tuple[int, str, _CheckServer]:
    server = _CheckServer()
    code, out = _finding(arguments, repo, root, server, monkeypatch, run=check, capture=check.capture, **kwargs)
    return code, out, server


def _decisions(server: _Server) -> list[tuple[str, Any]]:
    return [(path, body) for method, path, body in server.calls if method == "PATCH"]


def test_fixed_run_checks_the_new_snapshot_and_marks_only_on_a_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, base, head = _repo(tmp_path)
    pull = {"number": 12, "url": PR_URL, "slug": "acme/shop", "base_ref": "main", "title": "Fix price"}
    root = _saved(tmp_path, repo, pull_request=pull)
    arguments = ["1", "fixed", "--run", "pytest -q"]
    check = _Check()
    code, out, server = _run_fixed(arguments, repo, root, monkeypatch, check, agent_env="CLAUDECODE")
    assert (code, out.splitlines()[:2]) == (
        0,
        ["Tests pass: `pytest -q` exited 0 after 0.0s.", "Finding 1 is fixed: Unvalidated price"],
    )
    # The pull request's range at HEAD, on the pull request's own Review, then the command in the checkout.
    [(rng, source_ref)] = check.captured
    assert (rng.mode, rng.base_sha, rng.head_sha, rng.title) == ("commit_range", base, head, "PR #12: Fix price")
    assert source_ref == "github:acme/shop#12" and check.argv == [["pytest", "-q"]]
    writes = [(method, path) for method, path, _body in server.calls if method != "GET"]
    assert writes == [
        ("POST", "/api/reviews/rev1/revisions/3/publish"),
        ("POST", "/api/reviews/rev1/verification"),
        ("PATCH", "/api/annotations/ann-1"),
    ]
    recorded = next(body for _method, path, body in server.calls if path.endswith("/verification"))
    assert (recorded["revision_id"], recorded["name"], recorded["status"], recorded["source_ref"]) == (
        "rv9",
        "tests",
        "PASS",
        "pytest -q",
    )
    [(_patched, decision)] = _decisions(server)
    assert decision["finding_disposition_evidence"] == ["ev7"] and decision["actor"] == "agent"
    # Nothing was read from the evidence list: the check's own record is the proof.
    assert not [call for call in server.calls if call[1].endswith("/evidence")]

    # A failing run marks nothing and shows the end of its output.
    code, out, server = _run_fixed(arguments, repo, root, monkeypatch, _Check(exit_code=1))
    assert code == 1 and not _decisions(server)
    lines = out.splitlines()
    assert lines[0] == "Tests fail: `pytest -q` exited 1 after 0.0s." and lines[-2] == "E   assert 2 == 1"
    assert lines[-1] == "Nothing was marked fixed. Fix the failure, then run this again."
    assert len(lines) == 1 + 40 + 1
    code, out, _server = _run_fixed([*arguments, "--json"], repo, root, monkeypatch, _Check(exit_code=1))
    failed = json.loads(out)
    assert code == 1 and failed["check"]["status"] == "FAIL" and failed["output_tail"].endswith("assert 2 == 1")

    # A Snapshot on another Review runs nothing; a check on a Snapshot older than the latest proves nothing.
    elsewhere = _Check(review_id="rev2")
    code, out, server = _run_fixed(arguments, repo, root, monkeypatch, elsewhere)
    assert code == 1 and "the Snapshot went to another Review" in out and not elsewhere.argv
    assert not _decisions(server) and not [call for call in server.calls if call[0] == "POST"]
    code, out, server = _run_fixed(arguments, repo, root, monkeypatch, _Check(revision_id="rv8"))
    assert code == 1 and "a newer Snapshot arrived while the check ran" in out and not _decisions(server)

    # A command that edits the checkout tested source nobody captured: UNKNOWN, and nothing is marked.
    class _Edits(_Check):
        def __call__(self, argv: Any, cwd: Path) -> tuple[int, str]:
            (cwd / "scratch.py").write_text("x = 1\n")
            return super().__call__(argv, cwd)

    code, out, server = _run_fixed([*arguments, "--json"], repo, root, monkeypatch, _Edits())
    (repo / "scratch.py").unlink()
    assert code == 1 and json.loads(out)["check"]["status"] == "UNKNOWN" and not _decisions(server)
    # An unknown number never captures or runs anything.
    unknown = _Check()
    code, out, _server = _run_fixed(["5", "fixed", "--run", "pytest -q"], repo, root, monkeypatch, unknown)
    assert code == 1 and not unknown.argv and not unknown.captured

    # The fix must be the pull request's head: committed, and pushed when origin has the branch.
    (repo / "app.py").write_text("def price():\n    return 3\n")
    dirty = _Check()
    code, out, _server = _run_fixed(arguments, repo, root, monkeypatch, dirty)
    assert code == 1 and out.startswith("commit the fix first") and not dirty.captured
    _git(repo, "checkout", "--", "app.py")
    _git(repo, "update-ref", "refs/remotes/origin/feature", base)
    unpushed = _Check()
    code, out, _server = _run_fixed(arguments, repo, root, monkeypatch, unpushed)
    assert code == 1 and out.startswith("push the fix first") and not unpushed.captured
    _git(repo, "update-ref", "refs/remotes/origin/feature", head)
    assert _run_fixed(arguments, repo, root, monkeypatch, _Check())[0] == 0

    # --run goes with fixed alone, names a command, and replaces --evidence.
    for wrong_usage in (
        ["1", "confirmed", "--run", "pytest -q"],
        ["1", "fixed", "--run", " "],
        ["1", "fixed", "--run", "pytest -q", "--evidence", "ev2"],
    ):
        code, _out, _server = _run_fixed(wrong_usage, repo, root, monkeypatch, _Check())
        assert code == 2, wrong_usage


class _BranchServer(_CheckServer):
    """A branch's own Review, not a pull request's."""

    def __call__(self, method: str, path: str, body: Any) -> dict[str, Any]:
        if path == "/api/reviews/rev1":
            self.calls.append((method, path, body))
            return {"revision": {"id": "rv9", "revision_number": 3}, "session": {"source_ref": "client:working_tree"}}
        return super().__call__(method, path, body)


def test_fixed_run_needs_a_pull_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _base, _head = _repo(tmp_path)
    root = _saved(tmp_path, repo, pull_request=None)
    check, server = _Check(), _BranchServer()
    code, out = _finding(
        ["1", "fixed", "--run", "pytest -q"], repo, root, server, monkeypatch, run=check, capture=check.capture
    )
    assert code == 1 and out.startswith("this code review is not on a pull request")
    assert not check.captured and not check.argv and not _decisions(server)


class _GitHub:
    """`gh` as `lc review finding` calls it: the pull request's one review thread; every call recorded."""

    def __init__(self, *, resolved: bool = False, fail: str = "") -> None:
        self.calls: list[tuple[list[str], Any]] = []
        self.resolved = resolved
        self.fail = fail

    def __call__(self, args: Sequence[str], stdin: str | None) -> str:
        body = json.loads(stdin) if stdin else None
        self.calls.append((list(args), body))
        if self.fail:
            raise GitHubError(self.fail)
        if list(args[:2]) == ["api", "graphql"] and body["query"].startswith("query"):
            thread = {"id": "T1", "isResolved": self.resolved, "comments": {"nodes": [{"fullDatabaseId": "501"}]}}
            threads = {"pageInfo": {"hasNextPage": False}, "nodes": [thread]}
            return json.dumps({"data": {"repository": {"pullRequest": {"reviewThreads": threads}}}})
        return json.dumps({"id": 1})

    def writes(self) -> list[tuple[str, Any]]:
        return [
            (args[3] if args[1] == "--method" else body["query"].split("(")[1].split("{")[1], body)
            for args, body in self.calls
            if args[1] == "--method" or not body["query"].startswith("query")
        ]


def test_each_decision_is_answered_on_the_pull_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _base, head = _repo(tmp_path)
    pull = {"number": 12, "url": PR_URL, "slug": "acme/shop", "github_host": "github.com", "base_ref": "main"}
    posted = {"review_id": 77, "url": f"{PR_URL}#r77", "marker": "<!-- lemoncrow:op=report-x -->", "error": ""}
    findings = [
        {"number": 1, "title": "Unvalidated price", "path": "app.py", "line": 2, "annotation_id": "ann-1"},
        {"number": 2, "title": "Elsewhere", "path": "billing.py", "line": 0, "annotation_id": "ann-2"},
    ]
    findings[0]["comment_id"], findings[1]["body"] = 501, "billing() rounds twice."
    root = _saved(tmp_path, repo, pull_request=pull, github=posted, findings=findings)
    gh, asked = _GitHub(), []

    def github(repo_root: Path, host: str) -> _GitHub:
        asked.append((repo_root, host))
        return gh

    arguments = ["1", "fixed", "--run", "pytest -q"]
    code, out, _server = _run_fixed(arguments, repo, root, monkeypatch, _Check(), agent_env="CLAUDECODE", github=github)
    assert (code, out.splitlines()[-1], asked) == (0, "Answered on PR #12.", [(repo, "github.com")])
    api = "repos/acme/shop/pulls/12"
    body = (
        "**Claude's code review: 2 findings.** Claude fixes each one it agrees with and answers it here; skipping "
        "one needs the author's OK.\n\n"
        "1. **Fixed** · Unvalidated price `app.py:2`\n"
        "2. **Open** · Elsewhere `billing.py`\n"
        "   > billing() rounds twice.\n\n"
        "<!-- lemoncrow:op=report-x -->"
    )
    # The finding's comment is answered and resolved; the review's list shows where every finding stands.
    assert gh.writes() == [
        (f"{api}/comments/501/replies", {"body": f"Claude fixed this in {head[:7]}; tests pass."}),
        ("resolveReviewThread", {"query": gh.calls[2][1]["query"], "variables": {"id": "T1"}}),
        (f"{api}/reviews/77", {"body": body}),
    ]

    # A skip the user agreed to: answered with the reason, in plain words, and resolved.
    gh = _GitHub()
    code, out = _finding(
        ["1", "dismissed", "--reason", "incorrect"],
        repo,
        root,
        _Server(),
        monkeypatch,
        agent_env="CLAUDECODE",
        github=lambda _root, _host: gh,
    )
    writes = gh.writes()
    assert code == 0 and writes[0][1] == {"body": "Skipped, with the author's OK: not a real problem."}
    assert writes[1][0] == "resolveReviewThread"
    assert "1. **Skipped (not a real problem)** · Unvalidated price" in writes[2][1]["body"]

    # Opened again: its thread is unresolved.
    gh = _GitHub(resolved=True)
    code, out = _finding(["1", "open"], repo, root, _Server(), monkeypatch, github=lambda _root, _host: gh)
    assert code == 0 and [name for name, _body in gh.writes()][1:] == ["unresolveReviewThread", f"{api}/reviews/77"]

    # GitHub refused: the decision stands, and the line says the pull request was not updated.
    server = _Server()
    failing = _GitHub(fail="HTTP 404: Not Found")
    code, out = _finding(["1", "confirmed"], repo, root, server, monkeypatch, github=lambda _root, _host: failing)
    assert (code, out.splitlines()[-1]) == (0, "PR #12 was not updated: HTTP 404: Not Found") and _patches(server)

    # Never posted (gh was signed out when the review was saved): nothing is asked of GitHub.
    root = _saved(tmp_path, repo, pull_request=pull, github={"review_id": 0, "error": "`gh` is not signed in"})
    unused = _GitHub()
    code, out = _finding(["1", "confirmed"], repo, root, _Server(), monkeypatch, github=lambda _root, _host: unused)
    assert (code, out.splitlines()[-1]) == (0, "PR #12 was not updated: `gh` is not signed in") and not unused.calls


def test_the_list_and_why_n_read_the_saved_numbering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo, _base, _head = _repo(tmp_path)
    root = _saved(tmp_path, repo)
    code, out = _finding([], repo, root, _Server(), monkeypatch)
    assert code == 0 and out.splitlines() == [
        f"lc review: 2 findings on PR #12 · {URL}",
        "1. Unvalidated price (app.py:2) · open",
        "2. Elsewhere (billing.py) · a comment on the Review (not on a changed line)",
        'Fix the ones you agree with, commit and push, then `lc review finding N fixed --run "<test command>"`. '
        'Ask before skipping one: Skip N? <reason>. Reply "skip N" or "fix N".',
    ]
    code, out = _finding(["1"], repo, root, _Server(), monkeypatch)
    assert code == 0 and "price() returns 2 without validating the caller's tier." in out
    assert out.startswith("Finding 1: Unvalidated price (app.py:2) · open")

    # Once every finding is answered, the list asks for nothing more.
    class _Answered(_Server):
        def __call__(self, method: str, path: str, body: Any) -> dict[str, Any]:
            answer = super().__call__(method, path, body)
            for annotation in answer.get("annotations", []):
                annotation["finding_disposition"] = "fixed"
            return answer

    one = _saved(tmp_path, repo, findings=[{"number": 1, "title": "Unvalidated price", "annotation_id": "ann-1"}])
    code, out = _finding([], repo, one, _Answered(), monkeypatch)
    assert code == 0 and out.splitlines()[1:] == ["1. Unvalidated price · fixed", "Every finding is answered."]

    # Still saving: the list needs no server; a decision waits for the save.
    root = _saved(tmp_path, repo, status="saving", review_id="")
    code, out = _finding([], repo, root, _Server(), monkeypatch)
    assert code == 0 and "(still saving)" in out
    waits: list[float] = []
    code, out = _finding(["1", "confirmed"], repo, root, _Server(), monkeypatch, sleep=waits.append)
    assert code == 1 and out.endswith("lc review is still saving that code review; try again in a minute\n")
    assert waits and sum(waits) >= 60

    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q")
    code, out = _finding([], other, root, _Server(), monkeypatch)
    assert (code, out) == (
        1,
        "no pull request review has been saved for this branch yet; lc review saves one when the agent opens a "
        "pull request\n",
    )


def test_both_lc_programs_carry_the_finding_command(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow_client.cli import main as client_main

    assert {"finding", "report-findings"} <= set(REVIEW_SUBCOMMANDS)
    sink = io.StringIO()
    assert client_main(["review", "finding", "--help"], stdout=sink) == 0
    assert sink.getvalue().startswith("Usage: lc review finding [N ...] [confirmed|dismissed|fixed|open]")
    assert "--run CMD" in sink.getvalue() and "recorded as the agent's, never as yours" in sink.getvalue()
    result = CliRunner().invoke(REVIEW_SUBCOMMANDS["finding"], ["--help"])
    assert result.exit_code == 0 and "Usage: lc review finding" in result.output

    # The source checkout's `lc` runs the same command, under the same store root.
    seen: dict[str, Any] = {}

    def fake_main(arguments: list[str], **kwargs: Any) -> int:
        seen.update(kwargs, arguments=arguments)
        return 0

    monkeypatch.setattr("lemoncrow_client.review_findings.finding_main", fake_main)
    result = CliRunner().invoke(REVIEW_SUBCOMMANDS["finding"], ["1", "fixed", "--run", "x"], obj={"root": "/s"})
    assert result.exit_code == 0 and seen["arguments"] == ["1", "fixed", "--run", "x"]
    assert seen["store_root"] == Path("/s") and "review_cli" not in seen


# --------------------------------------------------------------------------- #
# Codex: the same flow through report-findings
# --------------------------------------------------------------------------- #


def test_codex_is_asked_to_review_post_and_report_the_pr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    groups = json.loads((CODEX_HOOKS / "hooks.json").read_text())["hooks"]["PostToolUse"]
    [wired] = [group["hooks"][0] for group in groups if "review_pr_opened.py" in json.dumps(group)]
    assert wired["command"].startswith("LEMONCROW_AGENT=codex ") and "${PLUGIN_ROOT}/hooks/" in wired["command"]

    repo, _base, head = _repo(tmp_path)
    hook = _load(CODEX_HOOKS / "review_pr_opened.py", "codex_review_pr_opened_hook")
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path / "store"))
    monkeypatch.delenv("LEMONCROW_IN_REVIEW", raising=False)
    monkeypatch.setattr(hook, "_dormant", lambda: False)
    monkeypatch.setattr(pr_trigger, "local_review_configured", lambda _cwd: True)
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "c1",
        "cwd": str(repo),
        "tool_name": "shell",
        "tool_input": {"command": ["bash", "-lc", "gh pr create --fill --base main"]},
        "tool_response": {"stdout": f"{PR_URL}\n", "exit_code": 0},
    }
    out = json.loads(_run(hook, payload, monkeypatch, capsys))
    assert set(out) == {"systemMessage", "hookSpecificOutput"}
    assert set(out["hookSpecificOutput"]) == {"hookEventName", "additionalContext"}
    context = out["hookSpecificOutput"]["additionalContext"]
    assert f"git diff origin/main...{head}" in context
    # lc review posts the findings on the pull request; Codex only hands them over.
    assert "gh api" not in context and "posts them on the pull request as one numbered review" in context
    assert f"report-findings --host codex --session-id c1 --pr {PR_URL} <file>" in context
    assert "4. Then fix them without waiting for the user." in context
    assert '`lc review finding N fixed --run "<test command>"`' in context
    assert "Never put an lc review link in anything posted to GitHub" in context
    assert _run(hook, payload, monkeypatch, capsys) == ""


def test_report_findings_saves_a_codex_review_on_the_prs_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, _base, _head = _repo(tmp_path)
    root = tmp_path / "store"
    opened = pr_trigger.opened_pull_request(_created())
    assert opened is not None
    pr_trigger.record_pr_trigger(root, opened, host="codex", session_id="c1", cwd=repo)
    spawned: list[tuple[dict[str, Any], str]] = []
    monkeypatch.setattr(reported_findings, "pull_request_review_url", lambda *_args, **_kwargs: URL)
    monkeypatch.setattr(
        reported_findings,
        "spawn_reported_findings",
        lambda payload, *, host, store_root: spawned.append((payload, host)),
    )
    runner = CliRunner()
    arguments = ["--host", "codex", "--session-id", "c1", "--pr", PR_URL, "--repo-root", str(repo), "-"]
    result = runner.invoke(report_findings_cmd, arguments, input=json.dumps([PRICE]), obj={"root": str(root)})
    assert result.exit_code == 0, result.output
    lines = result.output.splitlines()
    assert lines[:2] == [
        "lc review: 1 problem found on PR #12. Codex is fixing it; details are on the PR.",
        f"Also in lc review: {URL}",
    ]
    assert "1. Unvalidated price (app.py:2)" in lines
    assert any('`lc review finding N fixed --run "<test command>"`' in line for line in lines)
    [(payload, host)] = spawned
    assert host == "codex" and payload["pull_request"]["number"] == 12 and payload["tool_use_id"].startswith("cli:")
    [kept] = json.loads(reported_findings.reports_path(root).read_text())["reports"]
    assert (kept["host"], kept["session_id"], kept["status"]) == ("codex", "c1", "saving")

    # Not yet claimed by a hook (a person ran it): --pr alone still binds the pull request.
    spawned.clear()
    other = ["--host", "codex", "--session-id", "c2", "--pr", PR_URL, "--repo-root", str(repo), "-"]
    assert runner.invoke(report_findings_cmd, other, input="[]", obj={"root": str(root)}).exit_code == 0
    assert spawned[0][0]["pull_request"]["number"] == 12
    bad = runner.invoke(
        report_findings_cmd, ["--repo-root", str(repo), "-"], input="{not json", obj={"root": str(root)}
    )
    assert bad.exit_code == 1 and "not JSON" in bad.output
    wrong = runner.invoke(
        report_findings_cmd, ["--pr", "12", "--repo-root", str(repo), "-"], input="[]", obj={"root": str(root)}
    )
    assert wrong.exit_code == 1 and "--pr takes the pull request's URL" in wrong.output
    # A code review of no pull request is not saved.
    spawned.clear()
    none = runner.invoke(report_findings_cmd, ["--repo-root", str(repo), "-"], input="[]", obj={"root": str(root)})
    assert none.exit_code == 1 and "of a pull request only: pass --pr" in none.output and not spawned
