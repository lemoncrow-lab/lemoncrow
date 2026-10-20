"""Turn a list of pull request URLs into a benchmark corpus, with the other reviewer's comments beside each one.

This is the first-run path for a customer who still has Greptile's comments on their pull requests: give it their PR
URLs and it writes a corpus file (pinned head and merge base, from GitHub's API) and, for each pull request, the
competitor's comments as JSON. Nothing is labelled, because nothing is known: the pull requests yield cost, findings and
agreement with the other reviewer, and a person adds labels later (``derive-label``) for the ones whose bugs they know.

A customer's pull requests are customer data. The corpus is an ``organization`` corpus: it needs the organization's
name, who approved using its pull requests and a reference to that approval, and it is written outside this checkout.
"""

from __future__ import annotations

import json
import re
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import yaml

from lemoncrow.pro.capabilities.review.evaluation.competitor import (
    GREPTILE_LOGINS,
    CompetitorError,
    export_comments,
    fetch_pull_comments,
)
from lemoncrow.pro.capabilities.review.sources.github import GhRunner, GitHubCliError, gh_json

_PR_URL = re.compile(r"^https?://(?P<host>[^/]+)/(?P<repo>[^/]+/[^/]+)/pull/(?P<number>\d+)")
_PR_SHORT = re.compile(r"^(?P<repo>[\w.-]+/[\w.-]+)#(?P<number>\d+)$")
_SAFE = re.compile(r"[^a-z0-9._-]+")
HEADER: Final[str] = """\
# A benchmark corpus written by `lc dev eval review init-corpus`.
#
# It holds pull requests as they were, pinned by commit, and each one's comments from the other reviewer. Nothing is
# labelled: a pull request with no labels yields cost, findings by severity and agreement with the other reviewer, but no
# catch or noise rate. Add labels for bugs you KNOW a later commit fixed (`lc dev eval review derive-label`), and nothing
# else. See docs/reference/review-benchmark.md.
#
"""


class InitError(RuntimeError):
    """The corpus could not be written, for a reason the person running it can act on."""


@dataclass(frozen=True, slots=True)
class ResolvedPr:
    repository: str
    number: int
    title: str
    state: str
    head_sha: str
    base_sha: str
    merge_base_found: bool

    @property
    def pr_id(self) -> str:
        return _SAFE.sub("-", f"{self.repository.split('/')[-1]}-{self.number}".lower()).strip("-")


def parse_spec(spec: str) -> tuple[str, int]:
    match = _PR_URL.match(spec.strip()) or _PR_SHORT.match(spec.strip())
    if match is None:
        raise InitError(f"{spec!r} is not a pull request URL or OWNER/REPO#N")
    return match["repo"], int(match["number"])


def resolve_pr(runner: GhRunner, spec: str) -> ResolvedPr:
    """The pull request's final head and the merge base GitHub shows for it (what a reviewer was asked to look at)."""

    repository, number = parse_spec(spec)
    try:
        pull = gh_json(runner, ["api", f"repos/{repository}/pulls/{number}"])
    except GitHubCliError as exc:
        raise InitError(f"{repository}#{number}: {exc}") from exc
    head = str((pull.get("head") or {}).get("sha") or "")
    base = str((pull.get("base") or {}).get("sha") or "")
    if not head or not base:
        raise InitError(f"{repository}#{number}: GitHub returned no head or base commit")
    merge_base = ""
    try:
        compare = gh_json(runner, ["api", f"repos/{repository}/compare/{base}...{head}"])
        merge_base = str((compare.get("merge_base_commit") or {}).get("sha") or "")
    except GitHubCliError:
        merge_base = ""
    return ResolvedPr(
        repository=repository,
        number=number,
        title=str(pull.get("title") or ""),
        state="merged" if pull.get("merged") else str(pull.get("state") or ""),
        head_sha=head,
        base_sha=merge_base or base,
        merge_base_found=bool(merge_base),
    )


@dataclass(frozen=True, slots=True)
class InitResult:
    corpus_path: Path
    entries: tuple[str, ...]
    comment_files: tuple[Path, ...]
    notes: tuple[str, ...]


def init_corpus(
    runner: GhRunner,
    specs: Sequence[str],
    *,
    out: Path,
    corpus_id: str,
    organization: str,
    approved_by: str,
    reference: str,
    approved_at: str,
    logins: Collection[str] | None = GREPTILE_LOGINS,
    fetch_comments: bool = True,
    description: str = "",
) -> InitResult:
    """Write ``out`` (the corpus) and ``<out dir>/greptile/<id>.json`` for each pull request in *specs*."""

    if not specs:
        raise InitError("give at least one pull request")
    if not all((organization, approved_by, reference)):
        raise InitError("a customer's pull requests need --organization, --approved-by and --reference")
    notes: list[str] = []
    files: list[Path] = []
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for spec in specs:
        resolved = resolve_pr(runner, spec)
        if resolved.pr_id in seen:
            notes.append(f"{spec}: listed twice; kept once")
            continue
        seen.add(resolved.pr_id)
        if not resolved.merge_base_found:
            notes.append(f"{resolved.pr_id}: no merge base from GitHub; the base branch tip at review time is used")
        entry: dict[str, Any] = {
            "id": resolved.pr_id,
            "repository": resolved.repository,
            "pull": resolved.number,
            "title": resolved.title,
            "license": "proprietary",
            "provenance": {
                "source": "organization_review",
                "repository": f"https://github.com/{resolved.repository}",
                "commit": resolved.head_sha,
                "note": f"{resolved.state} pull request; its comments are the other reviewer's own",
            },
            "base_sha": resolved.base_sha,
            "head_sha": resolved.head_sha,
            "labelled": False,
        }
        if fetch_comments:
            try:
                comments = fetch_pull_comments(resolved.repository, resolved.number, logins=logins, runner=runner)
            except CompetitorError as exc:
                notes.append(f"{resolved.pr_id}: {exc}")
            else:
                target = out.parent / "greptile" / f"{resolved.pr_id}.json"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps(
                        export_comments(comments, source=f"github:{resolved.repository}#{resolved.number}"),
                        indent=2,
                        ensure_ascii=False,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                files.append(target)
                entry["competitor"] = {"greptile": {"comments_file": f"greptile/{resolved.pr_id}.json"}}
                inline = sum(1 for comment in comments if comment.kind == "inline")
                notes.append(f"{resolved.pr_id}: {inline} inline and {len(comments) - inline} summary comments")
        entries.append(entry)
    document: Mapping[str, Any] = {
        "version": 1,
        "id": corpus_id,
        "visibility": "organization",
        "organization": organization,
        "opt_in": {
            "organization": organization,
            "approved_by": approved_by,
            "approved_at": approved_at,
            "reference": reference,
        },
        "description": description or f"{organization}'s pull requests, for the cost and agreement benchmark.",
        "line_tolerance": 3,
        "match": "either",
        "prs": entries,
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        HEADER + yaml.safe_dump(dict(document), sort_keys=False, width=110, allow_unicode=True), encoding="utf-8"
    )
    return InitResult(out, tuple(entry["id"] for entry in entries), tuple(files), tuple(notes))


__all__ = ["HEADER", "InitError", "InitResult", "ResolvedPr", "init_corpus", "parse_spec", "resolve_pr"]
