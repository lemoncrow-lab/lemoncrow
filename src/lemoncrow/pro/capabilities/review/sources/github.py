"""The GitHub pull-request source: a PR on github.com -> a local commit range.

A pull request is reviewed exactly like any other commit range; this module only
answers *which* range and *which* durable Review it belongs to. It talks to
GitHub through the user's own ``gh`` executable and stores nothing: no token,
no refs in the user's repository (the fetch lands in ``FETCH_HEAD`` only).

The diff base is ``merge-base(base-branch tip, PR head)`` -- GitHub's "Files
changed" view -- so a line number published back to the PR lands on the same
hunk the reviewer read here.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GITHUB_SOURCE_PREFIX = "github:"

_GH_TIMEOUT_S = 60.0
_FETCH_TIMEOUT_S = 600.0

_PR_URL = re.compile(r"^https?://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>\d+)(?:[/?#].*)?$")
_PR_SHORT = re.compile(r"^(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<number>\d+)$")
_PR_NUMBER = re.compile(r"^#?(?P<number>\d+)$")
_SOURCE_REF = re.compile(r"^github:(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)#(?P<number>\d+)$")


class GitHubCliError(RuntimeError):
    """``gh`` is missing, unauthenticated, or GitHub refused the request."""


GhRunner = Callable[[Sequence[str], str | None], str]


def run_gh(args: Sequence[str], stdin: str | None = None, *, cwd: Path | None = None) -> str:
    """Run ``gh`` and return stdout; every failure becomes an actionable error."""

    cli = shutil.which("gh")
    if cli is None:
        raise GitHubCliError("GitHub CLI `gh` is not installed; install it and run `gh auth login`")
    try:
        completed = subprocess.run(
            [cli, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
            cwd=str(cwd) if cwd is not None else None,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise GitHubCliError(f"`gh {' '.join(args[:2])}` timed out after {_GH_TIMEOUT_S:.0f}s") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        if "auth login" in detail or "not logged" in detail.lower():
            raise GitHubCliError("`gh` is not signed in; run `gh auth login`")
        raise GitHubCliError(detail or f"`gh {' '.join(args[:2])}` failed with exit code {completed.returncode}")
    return completed.stdout


def gh_json(runner: GhRunner, args: Sequence[str], stdin: str | None = None) -> Any:
    raw = runner(args, stdin)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise GitHubCliError(f"`gh {' '.join(args[:2])}` returned invalid JSON") from exc


def gh_paginated(runner: GhRunner, endpoint: str) -> list[dict[str, Any]]:
    """Every item of a paginated GitHub list endpoint, flattened across pages."""

    pages = gh_json(runner, ["api", "--paginate", "--slurp", endpoint])
    if not isinstance(pages, list):
        raise GitHubCliError(f"GitHub returned an unexpected shape for {endpoint}")
    items: list[dict[str, Any]] = []
    for page in pages:
        rows = page if isinstance(page, list) else [page]
        items.extend(row for row in rows if isinstance(row, dict))
    return items


@dataclass(frozen=True)
class PullRequestRef:
    """The durable identity of one pull request."""

    owner: str
    repo: str
    number: int
    host: str = "github.com"

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    @property
    def source_ref(self) -> str:
        """One Review per PR: every new head becomes a revision of it."""
        return f"{GITHUB_SOURCE_PREFIX}{self.slug}#{self.number}"

    @property
    def url(self) -> str:
        return f"https://{self.host}/{self.slug}/pull/{self.number}"

    @property
    def api_path(self) -> str:
        return f"repos/{self.slug}/pulls/{self.number}"


def parse_pr_source_ref(source_ref: str) -> PullRequestRef | None:
    match = _SOURCE_REF.match(source_ref.strip())
    if match is None:
        return None
    return PullRequestRef(owner=match["owner"], repo=match["repo"], number=int(match["number"]))


@dataclass(frozen=True)
class PullRequestSource:
    """What `lc review --pr` resolved: the PR plus the exact range to capture."""

    ref: PullRequestRef
    title: str
    author: str
    state: str
    head_sha: str
    base_ref: str
    base_sha: str
    merge_base_sha: str
    remote: str

    @property
    def review_title(self) -> str:
        return f"PR #{self.ref.number}: {self.title}" if self.title else f"PR #{self.ref.number}"


def _gh_pr_args(spec: str) -> list[str]:
    text = spec.strip()
    short = _PR_SHORT.match(text)
    if short is not None:
        return ["pr", "view", short["number"], "--repo", f"{short['owner']}/{short['repo']}"]
    if _PR_URL.match(text) is not None or _PR_NUMBER.match(text) is not None:
        return ["pr", "view", text.lstrip("#")]
    raise ValueError(f"--pr expects a PR number, OWNER/REPO#NUMBER, or a pull request URL; got {spec!r}")


_PR_FIELDS = "number,url,title,author,state,headRefOid,baseRefName,baseRefOid"


def _remote_slug(url: str) -> tuple[str, str] | None:
    """``(host, owner/repo)`` for a git remote URL, lower-cased; None if not a host/path URL."""

    text = url.strip()
    scp = re.match(r"^(?:[\w.-]+@)?(?P<host>[\w.-]+):(?P<path>[^/].*)$", text)
    if "://" in text:
        rest = text.split("://", 1)[1]
        host_part, _, path = rest.partition("/")
        host = host_part.rsplit("@", 1)[-1].split(":", 1)[0]
    elif scp is not None:
        host, path = scp["host"], scp["path"]
    else:
        return None
    path = path.strip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if path.count("/") != 1:
        return None
    return host.lower(), path.lower()


def _git(repo_root: Path, *args: str, timeout: float = _GH_TIMEOUT_S) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def find_remote(repo_root: Path, ref: PullRequestRef) -> str:
    """The remote of this clone that points at the PR's base repository."""

    # The configured URLs, before any `insteadOf` rewrite: they name the
    # repository the user meant, whatever transport reaches it.
    listed = _git(repo_root, "config", "--get-regexp", r"^remote\..*\.url$")
    wanted = (ref.host.lower(), ref.slug.lower())
    names: list[str] = []
    for line in listed.stdout.splitlines():
        key, _, url = line.partition(" ")
        name = key[len("remote.") : -len(".url")]
        if name and _remote_slug(url) == wanted and name not in names:
            names.append(name)
    if not names:
        raise ValueError(
            f"no git remote here points at {ref.host}/{ref.slug}; run `lc review --pr` inside a clone of {ref.slug}"
        )
    return "origin" if "origin" in names else names[0]


def pull_request_ref(repo_root: Path, spec: str, *, runner: GhRunner | None = None) -> PullRequestRef:
    """Identify the PR *spec* names without fetching anything."""

    text = spec.strip()
    url = _PR_URL.match(text)
    if url is not None:
        return PullRequestRef(url["owner"], url["repo"], int(url["number"]), url["host"].lower())
    short = _PR_SHORT.match(text)
    if short is not None:
        return PullRequestRef(short["owner"], short["repo"], int(short["number"]))
    run: GhRunner = runner or (lambda args, stdin: run_gh(args, stdin, cwd=repo_root))
    view = gh_json(run, [*_gh_pr_args(text), "--json", "url"])
    resolved = _PR_URL.match(str(view.get("url") or "")) if isinstance(view, dict) else None
    if resolved is None:
        raise GitHubCliError(f"`gh pr view` returned no pull request URL for {spec!r}")
    return PullRequestRef(resolved["owner"], resolved["repo"], int(resolved["number"]), resolved["host"].lower())


def resolve_pull_request(
    repo_root: Path,
    spec: str,
    *,
    runner: GhRunner | None = None,
) -> PullRequestSource:
    """Resolve *spec*, fetch the PR head and base into this clone, find the merge base."""

    run: GhRunner = runner or (lambda args, stdin: run_gh(args, stdin, cwd=repo_root))
    view = gh_json(run, [*_gh_pr_args(spec), "--json", _PR_FIELDS])
    if not isinstance(view, dict):
        raise GitHubCliError("`gh pr view` returned an unexpected shape")
    url_match = _PR_URL.match(str(view.get("url") or ""))
    if url_match is None:
        raise GitHubCliError(f"`gh pr view` returned no pull request URL for {spec!r}")
    ref = PullRequestRef(
        owner=url_match["owner"],
        repo=url_match["repo"],
        number=int(url_match["number"]),
        host=url_match["host"].lower(),
    )
    head_sha = str(view.get("headRefOid") or "")
    base_sha = str(view.get("baseRefOid") or "")
    base_ref = str(view.get("baseRefName") or "")
    if not head_sha or not base_sha or not base_ref:
        raise GitHubCliError(f"GitHub did not report the head/base of {ref.url}")

    remote = find_remote(repo_root, ref)
    fetched = _git(
        repo_root,
        "fetch",
        "--no-tags",
        "--quiet",
        remote,
        f"refs/pull/{ref.number}/head",
        f"refs/heads/{base_ref}",
        timeout=_FETCH_TIMEOUT_S,
    )
    if fetched.returncode != 0:
        raise ValueError(f"git fetch of {ref.url} from {remote!r} failed: {fetched.stderr.strip()}")
    for sha, what in ((head_sha, "head"), (base_sha, "base")):
        if _git(repo_root, "cat-file", "-e", f"{sha}^{{commit}}").returncode != 0:
            raise ValueError(
                f"the PR {what} moved while it was being fetched ({sha[:12]} is not in this clone); run it again"
            )
    merge_base = _git(repo_root, "merge-base", base_sha, head_sha)
    merge_base_sha = merge_base.stdout.strip()
    if merge_base.returncode != 0 or not merge_base_sha:
        raise ValueError(f"{ref.url} shares no history with {base_ref}; nothing to diff against")
    author = view.get("author")
    return PullRequestSource(
        ref=ref,
        title=str(view.get("title") or ""),
        author=str(author.get("login") or "") if isinstance(author, dict) else "",
        state=str(view.get("state") or ""),
        head_sha=head_sha,
        base_ref=base_ref,
        base_sha=base_sha,
        merge_base_sha=merge_base_sha,
        remote=remote,
    )


__all__ = [
    "GITHUB_SOURCE_PREFIX",
    "GhRunner",
    "GitHubCliError",
    "PullRequestRef",
    "PullRequestSource",
    "find_remote",
    "gh_json",
    "gh_paginated",
    "parse_pr_source_ref",
    "pull_request_ref",
    "resolve_pull_request",
    "run_gh",
]
