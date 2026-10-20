"""Import another reviewer's comments on a pull request, as a baseline to compare findings against.

The first use is Greptile: the owner's churned customer still has Greptile's comments on their pull requests, so those
pull requests are a ready-made comparison. Two sources, both normalised to :class:`CompetitorComment`:

* **GitHub's API** for a pull request: its inline review comments, its conversation comments and its reviews, through
  ``gh`` (which also reaches private repositories the signed-in user can read) or, for a public pull request when ``gh``
  is missing or signed out, a plain unauthenticated request (``GITHUB_TOKEN``/``GH_TOKEN`` is used when set).
* **A JSON export**: GitHub's own REST shape, or the normalised shape this module writes, with an optional
  ``verdict`` per comment for a person who has judged it.

A comment is Greptile's when its author is one of :data:`GREPTILE_LOGINS` (change them with ``--competitor-login`` if the
GitHub App shows a different login on your pull requests: the first import prints the authors it saw). Replies in a
thread are not findings and are dropped. A comment with no file and line (a summary) is kept, counted, and never matched
to a finding, because there is nothing to put beside it.

Nothing here calls a model, and a competitor comment is never ground truth. See :mod:`scoring` for how it is compared.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from lemoncrow.pro.capabilities.review.evaluation.scoring import Candidate
from lemoncrow.pro.capabilities.review.sources.github import GhRunner, GitHubCliError, gh_paginated, run_gh

#: Logins Greptile's GitHub App comments as. To be confirmed on a real pull request: the importer reports the authors it
#: saw so a wrong guess is visible at once.
GREPTILE_LOGINS: Final[frozenset[str]] = frozenset(
    {"greptile-apps[bot]", "greptile-apps-staging[bot]", "greptileai[bot]", "greptile[bot]"}
)
EXPORT_SCHEMA: Final[int] = 1
VERDICTS: Final[tuple[str, ...]] = ("valid", "invalid")
_SEVERITY = re.compile(r"\b(P[0-3]|critical|high|medium|low|nit(?:pick)?)\b", re.IGNORECASE)
_API: Final[str] = "https://api.github.com"
_MAX_PAGES: Final[int] = 20


class CompetitorError(ValueError):
    """The comments could not be read, or are not the competitor's."""


@dataclass(frozen=True, slots=True)
class CompetitorComment:
    id: str
    author: str
    path: str
    line: int
    end_line: int
    body: str
    url: str = ""
    created_at: str = ""
    kind: str = "inline"  # inline | summary
    severity: str = ""
    verdict: str = ""  # "" until a person judges it: valid | invalid

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "author": self.author,
            "path": self.path,
            "line": self.line,
            "end_line": self.end_line,
            "body": self.body,
            "url": self.url,
            "created_at": self.created_at,
            "kind": self.kind,
            "severity": self.severity,
            "verdict": self.verdict,
        }

    def candidate(self) -> Candidate:
        return Candidate(
            id=f"comment-{self.id}",
            path=self.path,
            start_line=self.line,
            end_line=max(self.line, self.end_line),
            text=self.body,
            severity=self.severity,
            title=self.body.strip().splitlines()[0][:120] if self.body.strip() else "",
        )


def _int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _login(raw: Mapping[str, Any]) -> str:
    user = raw.get("user")
    if isinstance(user, Mapping):
        return str(user.get("login") or "")
    return str(raw.get("author") or raw.get("login") or "")


def _is_competitor(login: str, logins: Collection[str]) -> bool:
    return login.lower() in {item.lower() for item in logins}


def _comment(raw: Mapping[str, Any], *, default_kind: str) -> CompetitorComment | None:
    path = str(raw.get("path") or "").lstrip("./") if raw.get("path") else ""
    line = _int(raw.get("line")) or _int(raw.get("original_line")) or _int(raw.get("end_line"))
    first = _int(raw.get("start_line")) or _int(raw.get("original_start_line")) or line
    body = str(raw.get("body") or "")
    if not body.strip():
        return None
    inline = bool(path) and line > 0
    hint = _SEVERITY.search(body)
    verdict = str(raw.get("verdict") or "").lower()
    return CompetitorComment(
        id=str(raw.get("id") or raw.get("comment_id") or ""),
        author=_login(raw),
        path=path if inline else "",
        line=min(first, line) if inline else 0,
        end_line=line if inline else 0,
        body=body,
        url=str(raw.get("html_url") or raw.get("url") or ""),
        created_at=str(raw.get("created_at") or ""),
        kind="inline" if inline else (default_kind if default_kind != "inline" else "summary"),
        severity=str(raw.get("severity") or (hint.group(1) if hint else "")),
        verdict=verdict if verdict in VERDICTS else "",
    )


def parse_comments(
    payload: object,
    *,
    logins: Collection[str] | None = GREPTILE_LOGINS,
    default_kind: str = "inline",
) -> list[CompetitorComment]:
    """Normalise a list of GitHub comment objects, or an export of them, keeping only the competitor's root comments.

    With *logins* ``None`` every comment is kept (a file the person already filtered). Otherwise a payload whose
    comments carry authors but none of them are *logins* is an error naming the authors seen, not an empty result.
    """

    if isinstance(payload, Mapping):
        schema = payload.get("schema")
        if schema is not None and schema != EXPORT_SCHEMA:
            raise CompetitorError(f"unsupported export schema {schema!r}")
        rows = payload.get("comments")
        if rows is None:
            raise CompetitorError("an export is a list of comments or an object with a 'comments' list")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise CompetitorError("an export is a list of comments or an object with a 'comments' list")
    parsed: list[tuple[str, CompetitorComment]] = []
    seen: dict[str, int] = {}
    for item in rows:
        if not isinstance(item, Mapping) or item.get("in_reply_to_id"):
            continue
        comment = _comment(item, default_kind=default_kind)
        if comment is None:
            continue
        seen[comment.author] = seen.get(comment.author, 0) + 1
        parsed.append((comment.author, comment))
    if logins is None:
        kept = [comment for _author, comment in parsed]
    else:
        authored = [(author, comment) for author, comment in parsed if author]
        kept = [comment for author, comment in authored if _is_competitor(author, logins)]
        if authored and not kept:
            authors = ", ".join(f"{author} ({count})" for author, count in sorted(seen.items()) if author)
            raise CompetitorError(
                f"none of these comments are by {', '.join(sorted(logins))}; authors seen: {authors}. "
                "Pass the competitor's login with --competitor-login."
            )
        kept += [comment for author, comment in parsed if not author]
    unique: dict[str, CompetitorComment] = {}
    for comment in kept:
        key = comment.id or f"{comment.path}:{comment.line}:{hash(comment.body)}"
        unique.setdefault(key, comment)
    return sorted(unique.values(), key=lambda c: (c.path, c.line, c.id))


def load_comments(path: Path, *, logins: Collection[str] | None = GREPTILE_LOGINS) -> list[CompetitorComment]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CompetitorError(f"{path}: {exc}") from exc
    return parse_comments(payload, logins=logins)


def export_comments(
    comments: Sequence[CompetitorComment], *, source: str, competitor: str = "greptile"
) -> dict[str, Any]:
    """The normalised JSON export: what ``import-greptile`` writes and the benchmark reads."""

    return {
        "schema": EXPORT_SCHEMA,
        "competitor": competitor,
        "source": source,
        "note": 'Comments as the competitor posted them. Add "verdict": "valid" or "invalid" to judge one.',
        "comments": [comment.to_wire() for comment in comments],
    }


# --------------------------------------------------------------------------- #
# fetching
# --------------------------------------------------------------------------- #

Opener = Callable[[urllib.request.Request], Any]


def _http_pages(url: str, *, token: str, opener: Opener) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    next_url: str | None = url
    for _page in range(_MAX_PAGES):
        if not next_url:
            break
        request = urllib.request.Request(
            next_url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "lemoncrow-review-benchmark",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
        )
        try:
            with opener(request) as response:
                body = json.loads(response.read().decode("utf-8"))
                link = str(response.headers.get("Link") or "")
        except urllib.error.HTTPError as exc:
            raise CompetitorError(f"GitHub answered {exc.code} for {next_url}") from exc
        except (urllib.error.URLError, ValueError, OSError) as exc:
            raise CompetitorError(f"could not read {next_url}: {exc}") from exc
        if isinstance(body, list):
            items.extend(item for item in body if isinstance(item, dict))
        match = re.search(r'<([^>]+)>;\s*rel="next"', link)
        next_url = match.group(1) if match else None
    return items


def fetch_pull_comments(
    repository: str,
    number: int,
    *,
    logins: Collection[str] | None = GREPTILE_LOGINS,
    runner: GhRunner | None = None,
    opener: Opener | None = None,
) -> list[CompetitorComment]:
    """The competitor's comments on one pull request: inline review comments, conversation comments and reviews.

    *runner* is ``gh`` by default. When ``gh`` is missing or signed out the same three lists are read over plain HTTPS,
    which works for a public repository.
    """

    # lc-debt: only run against a fake ``gh`` and a fake HTTP opener; the first live fetch is the first real test of the
    # three endpoints and the login filter. Retire it once a customer's pull request has been imported for real.
    endpoints = (
        (f"repos/{repository}/pulls/{number}/comments", "inline"),
        (f"repos/{repository}/issues/{number}/comments", "summary"),
        (f"repos/{repository}/pulls/{number}/reviews", "summary"),
    )
    rows: list[CompetitorComment] = []
    run = runner or (lambda args, stdin: run_gh(args, stdin))
    use_http = False
    for endpoint, kind in endpoints:
        try:
            raw = gh_paginated(run, endpoint)
        except GitHubCliError as exc:
            if runner is not None:
                raise CompetitorError(str(exc)) from exc
            use_http = True
            break
        rows.extend(parse_comments(raw, logins=None, default_kind=kind))
    if use_http:
        rows = []
        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
        open_url = opener or urllib.request.urlopen
        for endpoint, kind in endpoints:
            raw = _http_pages(f"{_API}/{endpoint}?per_page=100", token=token, opener=open_url)
            rows.extend(parse_comments(raw, logins=None, default_kind=kind))
    if logins is None:
        return rows
    authored = [comment for comment in rows if comment.author]
    kept = [comment for comment in rows if _is_competitor(comment.author, logins)]
    if authored and not kept:
        counts: dict[str, int] = {}
        for comment in authored:
            counts[comment.author] = counts.get(comment.author, 0) + 1
        authors = ", ".join(f"{author} ({count})" for author, count in sorted(counts.items()))
        raise CompetitorError(
            f"none of the comments on {repository}#{number} are by {', '.join(sorted(logins))}; authors seen: "
            f"{authors}. Pass the competitor's login with --competitor-login."
        )
    return kept


__all__ = [
    "EXPORT_SCHEMA",
    "GREPTILE_LOGINS",
    "VERDICTS",
    "CompetitorComment",
    "CompetitorError",
    "export_comments",
    "fetch_pull_comments",
    "load_comments",
    "parse_comments",
]
