"""``lc review finding``: the numbered findings of a pull request's code review, and the answer to each.

When a coding agent opens a pull request, its own code review (Claude's ``/code-review``, or
``lc review report-findings`` from Codex) is saved on the pull request's Review and posted on the pull request
as one review, numbered in report order. ``<state>/review/reported-findings.json`` (written by
``lemoncrow.pro.capabilities.review.reported_findings``) keeps that numbering per checkout and branch, with each
finding's inline comment, so ``N`` here is the number the chat and the pull request show. A decision goes
through the Reader's own disposition route, then is answered on the pull request: a reply on the finding's
comment, its thread resolved (or reopened), and the review's numbered list brought up to date. Run inside a
coding agent's session a decision is recorded as that agent's (``actor: agent``), never as the reviewer's.
``fixed --run`` captures the pull request's head as the Review's new Snapshot, runs the command on it and
records the result as its ``tests`` check, and marks nothing unless it passes. Standard library only (GitHub is
reached through ``gh``): the installed ``lc`` and a source checkout's ``lc`` both run this.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, TextIO
from urllib.parse import quote

from .errors import ClientError

if TYPE_CHECKING:
    from .config import ClientConfig
    from .review import ReviewRange

__all__ = [
    "DECISIONS",
    "REASONS",
    "REASON_WORDS",
    "USAGE",
    "agent_name",
    "finding_main",
    "latest_report",
    "pull_request_review_body",
]

#: What the user says, and the disposition the Review stores for it.
DECISIONS: Final[dict[str, str]] = {
    "confirmed": "confirmed_by_reviewer",
    "dismissed": "dismissed",
    "fixed": "fixed",
    "open": "open",
}
#: Mirrors ``session_models.FINDING_DISMISSAL_REASONS`` (without the empty value).
REASONS: Final[tuple[str, ...]] = ("incorrect", "accepted_risk", "duplicate", "not_applicable")
_SAVE_WAIT_S: Final = 120.0
_DRAFT_REFUSAL: Final = "Running drafts are read-only"
#: The check ``fixed --run`` records, and how much of a failing run's output is shown.
_CHECK_NAME: Final = "tests"
_TAIL_LINES: Final = 40
_DONE: Final[dict[str, str]] = {
    "confirmed": "is a real problem (confirmed)",
    "dismissed": "is skipped",
    "fixed": "is fixed",
    "open": "is open again",
}
#: How a skip's reason reads in the chat and on the pull request.
REASON_WORDS: Final[dict[str, str]] = {
    "incorrect": "not a real problem",
    "accepted_risk": "a risk the author accepts",
    "duplicate": "a duplicate of another finding",
    "not_applicable": "does not apply here",
}
_CHECK_WORDS: Final[dict[str, str]] = {
    "PASS": "Tests pass",
    "FAIL": "Tests fail",
    "UNKNOWN": "Tests inconclusive",
    "NOT_RUN": "Tests did not run",
}
_AGENT_NAMES: Final[dict[str, str]] = {"claude": "Claude", "codex": "Codex"}


def agent_name(host: str) -> str:
    """The coding agent as the chat and the pull request name it: ``claude`` is Claude."""
    return _AGENT_NAMES.get(host, host.capitalize() if host else "The agent")


USAGE: Final = """\
Usage: lc review finding [N ...] [confirmed|dismissed|fixed|open] [options]

The findings of this pull request's code review, numbered as the chat and the pull request show them.

  lc review finding                  list them, with what was decided so far
  lc review finding 2                show finding 2 in full
  lc review finding 1 2 fixed --run "<test command>"
                                     fixed, committed and pushed: run the tests on the pull request's new
                                     head, then mark them fixed only if they pass
  lc review finding 3 dismissed --reason accepted_risk
                                     the user said to skip it; say why
  lc review finding 1 2 confirmed    real problems; keep them open until fixed
  lc review finding 1 fixed          fixed, proven by a passing check already on the latest Snapshot
  lc review finding 1 open           take a decision back

Options:
  --run CMD       With fixed: capture the pull request's head commit as the Review's new Snapshot and run
                  CMD in the checkout as its "tests" check. A failing run marks nothing and prints the end
                  of its output.
  --reason R      Why a finding is skipped: incorrect, accepted_risk, duplicate or not_applicable.
  --note TEXT     Your own words, added to the finding as your reply (not from an agent's session).
  --evidence ID   The check that proves a fix (default: every passing check on the latest Snapshot).
  --repo-root P   The repository. Default: the current one.
  --json          One JSON document on standard output.

Each decision is answered on the pull request too: a reply on the finding's comment, which is resolved when
fixed or skipped. N may also be the finding's id on the Review (ann-...). Run inside a coding agent's session,
a decision is recorded as the agent's, never as yours, and only fixed, dismissed or open (no --note).
"""

Request = Callable[[str, str, "Mapping[str, Any] | None"], Mapping[str, Any]]
#: Runs one command line from a directory: ``(exit status, its output)``.
CheckRunner = Callable[[Sequence[str], Path], "tuple[int, str]"]
#: ``capture(config, range, source_ref=...)``: the server's answer to one new Snapshot.
SnapshotCapture = Callable[..., Mapping[str, Any]]
#: ``gh ARGS`` with optional standard input: its standard output, or :class:`GitHubError`.
GitHubCall = Callable[[Sequence[str], "str | None"], str]
#: The ``gh`` of one checkout and GitHub host.
GitHubFor = Callable[[Path, str], GitHubCall]


class _Refusal(Exception):
    """A plain-language reason this command did nothing."""


class GitHubError(Exception):
    """A ``gh`` call that failed, in one line."""


def _store_root(environment: Mapping[str, str]) -> Path:
    for name in ("LEMONCROW_ROOT", "LEMONCROW_STORE_ROOT", "LEMONCROW_HOME"):
        value = environment.get(name, "").strip()
        if value:
            return Path(value).expanduser()
    return Path(environment.get("HOME", str(Path.home()))) / ".lemoncrow"


def _git(cwd: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def latest_report(store_root: Path, repo_root: Path, branch: str) -> dict[str, Any] | None:
    """The newest saved code review for this checkout and branch (``refs/heads/<name>``, ``""`` detached)."""

    try:
        data = json.loads((store_root / "review" / "reported-findings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    raw = data.get("reports") if isinstance(data, dict) else None
    entries = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
    root = str(repo_root)
    exact = [item for item in entries if item.get("repo_root") == root and item.get("branch") == branch]
    # Detached (a paused rebase, say): the checkout's newest report.
    found = exact or ([item for item in entries if item.get("repo_root") == root] if not branch else [])
    return max(found, key=lambda item: str(item.get("created_at") or "")) if found else None


def _parse(arguments: Sequence[str]) -> tuple[list[str], str, dict[str, Any]]:
    targets: list[str] = []
    decision = ""
    options: dict[str, Any] = {"evidence": []}
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        name, has_inline, inline = argument.partition("=")
        if name in {"--reason", "--note", "--evidence", "--repo-root", "--run"}:
            if not has_inline:
                index += 1
                if index >= len(arguments):
                    raise _Refusal(f"{name} needs a value")
            value = inline if has_inline else arguments[index]
            if name == "--evidence":
                options["evidence"].append(value)
            else:
                options[name.removeprefix("--").replace("-", "_")] = value
        elif argument == "--json":
            options["json"] = True
        elif argument.startswith("-"):
            raise _Refusal(f"unknown option {argument}; run `lc review finding --help`")
        elif argument in DECISIONS:
            if decision:
                raise _Refusal("give one decision: confirmed, dismissed, fixed or open")
            decision = argument
        elif argument.isdigit() or argument.startswith("ann-"):
            if decision:
                raise _Refusal("put the finding numbers before the decision: `lc review finding 1 2 confirmed`")
            targets.append(argument)
        else:
            raise _Refusal(
                f"{argument!r} is neither a finding number nor a decision (confirmed, dismissed, fixed, open)"
            )
        index += 1
    reason = str(options.get("reason") or "")
    if decision and not targets:
        raise _Refusal(f"say which finding: `lc review finding 1 {decision}`")
    if decision == "dismissed" and reason not in REASONS:
        raise _Refusal("a dismissed finding needs --reason incorrect, accepted_risk, duplicate or not_applicable")
    if reason and decision != "dismissed":
        raise _Refusal("--reason only goes with dismissed")
    if options["evidence"] and decision != "fixed":
        raise _Refusal("--evidence only goes with fixed")
    if "run" in options and (decision != "fixed" or not str(options["run"]).strip()):
        raise _Refusal(
            '--run goes with fixed and names the test command: `lc review finding 1 fixed --run "pytest -q"`'
        )
    if "run" in options and options["evidence"]:
        raise _Refusal("give --run or --evidence, not both: --run records its own evidence")
    if "note" in options and not decision:
        raise _Refusal("--note goes with a decision: `lc review finding 3 dismissed --reason ... --note TEXT`")
    return targets, decision, options


def _wait_until_saved(
    report: dict[str, Any], reload: Callable[[], dict[str, Any] | None], sink: TextIO, sleep: Callable[[float], None]
) -> dict[str, Any]:
    waited = 0.0
    while report.get("status") == "saving" and waited < _SAVE_WAIT_S:
        if not waited:
            sink.write("lc review is still saving that code review; waiting for it...\n")
        sleep(1.0)
        waited += 1.0
        report = reload() or report
    if report.get("status") == "saving":
        raise _Refusal("lc review is still saving that code review; try again in a minute")
    if report.get("status") != "saved" or not report.get("review_id"):
        error = str(report.get("error") or "it stopped before the Review had a Snapshot")
        raise _Refusal(f"lc review could not save that code review: {error}")
    return report


def _pr_number(report: Mapping[str, Any]) -> int:
    pull = report.get("pull_request")
    number = pull.get("number") if isinstance(pull, dict) else 0
    return number if isinstance(number, int) else 0


def _label(finding: Mapping[str, Any]) -> str:
    path, line = str(finding.get("path") or ""), int(finding.get("line") or 0)
    where = f"{path}:{line}" if path and line else path
    return str(finding.get("title") or "Finding") + (f" ({where})" if where else "")


def _state(annotation: Mapping[str, Any]) -> str:
    value = str(annotation.get("finding_disposition") or "open")
    reason = str(annotation.get("finding_disposition_reason") or "")
    words = {"confirmed_by_reviewer": "confirmed", "superseded_stale": "outdated", "dismissed": "skipped"}.get(
        value, value
    )
    return f"{words} ({REASON_WORDS.get(reason, reason)})" if reason else words


class _Review:
    """The saved report's Review, read through the Reader API."""

    def __init__(self, request: Request, report: Mapping[str, Any]) -> None:
        self.request = request
        self.report = report
        self.review_id = str(report["review_id"])
        self.path = f"/api/reviews/{quote(self.review_id, safe='')}"
        self._annotations: dict[str, Mapping[str, Any]] | None = None

    def annotations(self) -> dict[str, Mapping[str, Any]]:
        if self._annotations is None:
            payload = self.request("GET", f"{self.path}/annotations", None)
            raw = payload.get("annotations")
            rows = [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []
            self._annotations = {str(item.get("id") or ""): item for item in rows}
        return self._annotations

    def revision(self) -> tuple[str, int, Mapping[str, Any]]:
        overview = self.request("GET", self.path, None)
        raw_revision = overview.get("revision")
        raw_session = overview.get("session")
        revision = raw_revision if isinstance(raw_revision, Mapping) else {}
        session = raw_session if isinstance(raw_session, Mapping) else {}
        number = revision.get("revision_number")
        return str(revision.get("id") or ""), number if isinstance(number, int) else -1, session

    def target(self, value: str) -> tuple[int, Mapping[str, Any]]:
        """``(number, annotation)`` of one target the user named; the number is 0 for an id."""

        findings = [item for item in self.report.get("findings") or [] if isinstance(item, Mapping)]
        if value.startswith("ann-"):
            found = self.annotations().get(value)
            if found is None:
                raise _Refusal(f"there is no finding {value} on this Review")
            return 0, found
        number = int(value)
        if not 1 <= number <= len(findings):
            count = len(findings)
            raise _Refusal(
                f"there is no finding {number}; the last code review has {count} (1-{count})"
                if count
                else "the last code review found nothing"
            )
        finding = findings[number - 1]
        annotation_id = str(finding.get("annotation_id") or "")
        found = self.annotations().get(annotation_id) if annotation_id else None
        if found is None:
            discussed = annotation_id.replace("ann-", "dsc-", 1) in self.annotations() if annotation_id else False
            raise _Refusal(
                f"finding {number} is not on a line of this change, so lc review keeps it as a comment on the "
                "Review, and a comment has no decision; answer it in the Reader"
                if discussed
                else f"finding {number} is not open on the Review (a finding reported as already fixed never is)"
            )
        return number, found

    def passing_checks(self, revision_id: str) -> list[str]:
        payload = self.request("GET", f"{self.path}/evidence", None)
        raw = payload.get("evidence")
        rows = [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []
        return [
            str(item["id"])
            for item in rows
            if item.get("id")
            and item.get("revision_id") == revision_id
            and item.get("kind") == "verification"
            and str(item.get("verification_status") or "").upper() == "PASS"
        ]

    def publish(self, revision_number: int) -> None:
        self.request("POST", f"{self.path}/revisions/{revision_number}/publish", {})


def _source_pr(session: Mapping[str, Any]) -> int:
    """The pull request number of a Review opened by ``lc review --pr`` (``source_ref`` ``github:o/r#N``), else 0."""

    source_ref = str(session.get("source_ref") or "")
    number = source_ref.rsplit("#", 1)[-1] if source_ref.startswith("github:") else ""
    return int(number) if number.isdigit() else 0


# --------------------------------------------------------------------------- #
# The pull request on GitHub
# --------------------------------------------------------------------------- #

_STATUS_WORDS: Final[dict[str, str]] = {
    "fixed": "Fixed",
    "dismissed": "Skipped",
    "confirmed_by_reviewer": "Confirmed",
    "superseded_stale": "Outdated",
}
_QUOTED_LINES: Final = 12
_THREADS_QUERY: Final = (
    "query($owner:String!,$name:String!,$number:Int!,$cursor:String){repository(owner:$owner,name:$name){"
    "pullRequest(number:$number){reviewThreads(first:100,after:$cursor){pageInfo{hasNextPage endCursor}"
    "nodes{id isResolved comments(first:1){nodes{fullDatabaseId}}}}}}}"
)
_THREAD_MUTATION: Final = "mutation($id:ID!){%s(input:{threadId:$id}){thread{isResolved}}}"


def pull_request_review_body(
    findings: Sequence[Mapping[str, Any]],
    states: Mapping[int, tuple[str, str]],
    *,
    agent: str,
    on_diff: Collection[int],
) -> str:
    """The body of the review lc review posts on a pull request: every numbered finding and where it stands.

    ``states`` maps a finding's number to its ``(disposition, reason)`` (open when absent); ``on_diff`` are the
    numbers that have their own inline comment. Any other finding's text is quoted here, the only place it shows.
    """

    count = len(findings)
    if not count:
        return f"**{agent}'s code review found no problems.**"
    lines = [
        f"**{agent}'s code review: {count} finding{'' if count == 1 else 's'}.** {agent} fixes each one it agrees "
        "with and answers it here; skipping one needs the author's OK.",
        "",
    ]
    for number, finding in enumerate(findings, 1):
        disposition, reason = states.get(number, ("open", ""))
        status = _STATUS_WORDS.get(disposition, "Open")
        if disposition == "dismissed" and reason in REASON_WORDS:
            status += f" ({REASON_WORDS[reason]})"
        title = str(finding.get("title") or "Finding")
        path, line = str(finding.get("path") or ""), int(finding.get("line") or 0)
        where = f" `{path}:{line}`" if path and line else f" `{path}`" if path else ""
        lines.append(f"{number}. **{status}** · {title}{where}")
        text = str(finding.get("body") or "").strip()
        if number not in on_diff and text and text != title:
            lines.extend(f"   > {row}".rstrip() for row in text.splitlines()[:_QUOTED_LINES])
    return "\n".join(lines)


def _gh_for(repo_root: Path, host: str) -> GitHubCall:
    """``gh`` run in *repo_root*, against *host* when it is not github.com."""

    def call(args: Sequence[str], stdin: str | None = None) -> str:
        import shutil

        gh = shutil.which("gh")
        if gh is None:
            raise GitHubError("the GitHub CLI `gh` is not installed")
        hostname = ["--hostname", host] if host and host != "github.com" else []
        try:
            done = subprocess.run(
                [gh, args[0], *hostname, *args[1:]],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=repo_root,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitHubError(f"`gh {args[0]}` did not finish: {exc}") from exc
        if done.returncode != 0:
            said = (done.stderr or done.stdout or "").strip().splitlines()
            raise GitHubError(said[-1][:200] if said else f"`gh {args[0]}` exited {done.returncode}")
        return done.stdout

    return call


def _gh_json(gh: GitHubCall, args: Sequence[str], body: Mapping[str, Any] | None = None) -> Any:
    output = gh([*args, "--input", "-"] if body is not None else args, json.dumps(body) if body is not None else None)
    try:
        return json.loads(output) if output.strip() else {}
    except ValueError as exc:
        raise GitHubError(f"GitHub answered `gh {' '.join(args[:2])}` with something that is not JSON") from exc


def _review_threads(gh: GitHubCall, slug: str, number: int) -> dict[int, tuple[str, bool]]:
    """``{first comment id: (thread id, resolved)}`` for every review thread of the pull request."""

    owner, _, name = slug.partition("/")
    threads: dict[int, tuple[str, bool]] = {}
    cursor: str | None = None
    while True:
        variables = {"owner": owner, "name": name, "number": number, "cursor": cursor}
        data = _gh_json(gh, ["api", "graphql"], {"query": _THREADS_QUERY, "variables": variables})
        if isinstance(data, dict) and data.get("errors"):
            raise GitHubError(str(data["errors"])[:200])
        page: Any = data
        for key in ("data", "repository", "pullRequest", "reviewThreads"):
            page = page.get(key) if isinstance(page, dict) else None
        if not isinstance(page, dict):
            return threads
        for node in page.get("nodes") or ():
            comments = ((node or {}).get("comments") or {}).get("nodes") or [{}]
            first = str((comments[0] or {}).get("fullDatabaseId") or "")
            if first.isdigit() and isinstance(node.get("id"), str):
                threads[int(first)] = (node["id"], node.get("isResolved") is True)
        info = page.get("pageInfo") or {}
        if info.get("hasNextPage") is not True or not isinstance(info.get("endCursor"), str):
            return threads
        cursor = info["endCursor"]


def _reply(decision: str, reason: str, *, agent: str, head_sha: str) -> str:
    """What the finding's comment is answered with."""

    if decision == "fixed":
        where = f" in {head_sha[:7]}" if head_sha else ""
        return (f"{agent} fixed this{where}" if agent else f"Fixed{where}") + "; tests pass."
    if decision == "dismissed":
        why = REASON_WORDS.get(reason, reason.replace("_", " "))
        return f"Skipped, with the author's OK: {why}." if agent else f"Skipped: {why}."
    if decision == "open":
        return "Open again."
    return ""


def _answer_on_pull_request(
    report: Mapping[str, Any],
    annotations: Mapping[str, Mapping[str, Any]],
    done: Sequence[Mapping[str, Any]],
    decision: str,
    *,
    head_sha: str,
    gh: GitHubCall,
) -> str:
    """Answer each decided finding on the pull request and bring the review's list up to date; one line saying so."""

    raw_pull, raw_posted = report.get("pull_request"), report.get("github")
    pull: Mapping[str, Any] = raw_pull if isinstance(raw_pull, Mapping) else {}
    posted: Mapping[str, Any] = raw_posted if isinstance(raw_posted, Mapping) else {}
    slug, number, review_id = str(pull.get("slug") or ""), _pr_number(report), posted.get("review_id")
    if not slug or not number:
        return ""
    if not isinstance(review_id, int) or review_id <= 0:
        why = str(posted.get("error") or "lc review has not posted this code review on it")
        return f"PR #{number} was not updated: {why}"
    findings = [item for item in report.get("findings") or [] if isinstance(item, Mapping)]
    numbers = {str(item.get("annotation_id") or ""): index for index, item in enumerate(findings, 1)}
    states: dict[int, tuple[str, str]] = {
        index: (
            str(annotations.get(str(item.get("annotation_id") or ""), {}).get("finding_disposition") or "open"),
            str(annotations.get(str(item.get("annotation_id") or ""), {}).get("finding_disposition_reason") or ""),
        )
        for index, item in enumerate(findings, 1)
    }
    agent = agent_name(str(done[0].get("agent_host") or "")) if done and done[0].get("actor") == "agent" else ""
    api = f"repos/{slug}/pulls/{number}"
    try:
        threads: dict[int, tuple[str, bool]] | None = None
        for item in done:
            index = int(item.get("number") or 0) or numbers.get(str(item.get("annotation_id") or ""), 0)
            if not index:
                continue
            states[index] = (str(item["finding_disposition"]), str(item.get("reason") or ""))
            comment_id = findings[index - 1].get("comment_id")
            text = _reply(decision, str(item.get("reason") or ""), agent=agent, head_sha=head_sha)
            if not isinstance(comment_id, int) or comment_id <= 0 or not text:
                continue
            _gh_json(gh, ["api", "--method", "POST", f"{api}/comments/{comment_id}/replies"], {"body": text})
            if decision not in {"fixed", "dismissed", "open"}:
                continue
            threads = threads if threads is not None else _review_threads(gh, slug, number)
            thread_id, resolved = threads.get(comment_id, ("", decision == "open"))
            if thread_id and resolved == (decision == "open"):
                mutation = _THREAD_MUTATION % ("unresolveReviewThread" if decision == "open" else "resolveReviewThread")
                _gh_json(gh, ["api", "graphql"], {"query": mutation, "variables": {"id": thread_id}})
        on_diff = {index for index, item in enumerate(findings, 1) if item.get("comment_id")}
        body = pull_request_review_body(
            findings, states, agent=agent_name(str(report.get("host") or "")), on_diff=on_diff
        )
        marker = str(posted.get("marker") or "")
        _gh_json(
            gh,
            ["api", "--method", "PUT", f"{api}/reviews/{review_id}"],
            {"body": f"{body}\n\n{marker}" if marker else body},
        )
    except GitHubError as exc:
        return f"PR #{number} was not updated: {exc}"
    return f"Answered on PR #{number}."


def _run_command(argv: Sequence[str], cwd: Path) -> tuple[int, str]:
    done = subprocess.run(list(argv), cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    return done.returncode, done.stdout.decode("utf-8", "replace")


def _checkout_state(repo_root: Path) -> str:
    """HEAD and every non-ignored change, as a command run in the checkout sees them; writes nothing."""

    import hashlib

    from .review import _INTERNAL_PATHS

    spec = ["--", ".", *[f":(exclude){path}" for path in _INTERNAL_PATHS]]
    digest = hashlib.sha256()
    untracked = b""
    for args in (
        ["rev-parse", "--verify", "--quiet", "HEAD"],
        ["diff", "HEAD", "--binary", "--no-ext-diff", "--no-color", *spec],
        ["ls-files", "--others", "--exclude-standard", "-z", *spec],
    ):
        done = subprocess.run(["git", "-C", str(repo_root), *args], capture_output=True, check=False)
        digest.update(done.stdout + b"\0\0")
        untracked = done.stdout if args[0] == "ls-files" else untracked
    for name in filter(None, untracked.split(b"\0")):
        path = repo_root / os.fsdecode(name)
        digest.update(name + b"\0")
        try:
            digest.update(os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes())
        except OSError:
            digest.update(b"\0unreadable")
    return digest.hexdigest()


def _capture_snapshot(config: ClientConfig, rng: ReviewRange, *, source_ref: str) -> Mapping[str, Any]:
    """A new Snapshot of *rng*, captured as ``lc review`` captures one."""

    from .review import _capture

    response, _url = _capture(config, rng, with_impact=True, restore_archived=False, source_ref=source_ref)
    return response


def _check_range(report: Mapping[str, Any], session: Mapping[str, Any], repo_root: Path) -> tuple[ReviewRange, str]:
    """What the check captures, and the Review it names (``""``: the range's own).

    A pull request's Review takes ``merge-base(origin/BASE, HEAD)..HEAD``, the range its findings were saved
    from, so HEAD must be the committed fix, pushed when this clone tracks the branch.
    """

    from dataclasses import replace

    from .review import resolve_review_range

    number = _pr_number(report) or _source_pr(session)
    if not number:
        raise _Refusal(
            "this code review is not on a pull request; lc review records a tested fix only for a pull request's "
            "findings"
        )
    raw = report.get("pull_request")
    pull: Mapping[str, Any] = raw if isinstance(raw, Mapping) else {}
    source_ref = str(session.get("source_ref") or "")
    if not source_ref.startswith("github:"):
        slug = str(pull.get("slug") or "")
        source_ref = f"github:{slug}#{number}" if slug else ""
    base_ref = str(pull.get("base_ref") or "") or _git(
        repo_root, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"
    ).removeprefix("origin/")
    merge_base = _git(repo_root, "merge-base", f"refs/remotes/origin/{base_ref}", "HEAD") if base_ref else ""
    if not merge_base:
        raise _Refusal(
            f"cannot tell where PR #{number} leaves {base_ref or 'its base branch'}: run "
            f"`git fetch origin {base_ref or 'main'}`, then this again"
        )
    rng = resolve_review_range(repo_root, base=merge_base, head="HEAD")
    if rng.dirty:
        raise _Refusal(
            f"commit the fix first: the check runs on PR #{number}'s head commit, and this checkout has "
            "uncommitted or untracked changes (`git status`)"
        )
    branch = _git(repo_root, "symbolic-ref", "--quiet", "--short", "HEAD")
    pushed = _git(repo_root, "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}") if branch else ""
    if pushed and pushed != rng.head_sha:
        raise _Refusal(
            f"push the fix first: the check runs on PR #{number}'s head, and origin/{branch} is not HEAD yet"
        )
    title = str(pull.get("title") or "")
    return replace(rng, title=f"PR #{number}: {title}" if title else f"PR #{number}"), source_ref


def _run_check(
    review: _Review,
    repo_root: Path,
    command: str,
    *,
    config: ClientConfig,
    run: CheckRunner,
    capture: SnapshotCapture,
) -> tuple[dict[str, Any], str]:
    """The Review's ``tests`` check: capture its current Snapshot, run COMMAND in the checkout, record the result.

    What ``lc review [--pr N] --check tests --run COMMAND`` records, done here so an installed ``lc`` needs no
    full Review CLI. The checkout is fingerprinted around the capture and the run: if it moved, the result is
    UNKNOWN, never a PASS for source the command did not see. Returns the check's record and the last lines of
    the command's output.
    """

    import shlex

    try:
        argv = shlex.split(command)
    except ValueError as exc:
        raise _Refusal(f"--run could not be parsed: {exc}") from exc
    _, _, session = review.revision()
    rng, source_ref = _check_range(review.report, session, repo_root)
    before = _checkout_state(repo_root)
    response = capture(config, rng, source_ref=source_ref)
    raw_review, raw_revision = response.get("review"), response.get("revision")
    captured = raw_review if isinstance(raw_review, Mapping) else {}
    revision = raw_revision if isinstance(raw_revision, Mapping) else {}
    review_id, revision_id = str(captured.get("id") or ""), str(revision.get("id") or "")
    revision_number = revision.get("revision_number")
    if not review_id or not revision_id or not isinstance(revision_number, int):
        raise _Refusal("lc review captured the Snapshot without naming it, so nothing was checked")
    if review_id != review.review_id:
        raise _Refusal(
            f"the Snapshot went to another Review than this code review's ({review_id} instead of "
            f"{review.review_id}), so nothing was checked or marked fixed"
        )
    # A capture is a running draft until published; the check proves the Snapshot people read.
    review.publish(revision_number)
    if _checkout_state(repo_root) != before:
        raise _Refusal("the checkout changed while its Snapshot was captured; run this again")
    started = time.monotonic()
    exit_code: int | None
    try:
        exit_code, output = run(argv, repo_root)
        failure = ""
    except OSError as exc:
        exit_code, output, failure = None, "", str(exc)
    elapsed = time.monotonic() - started
    if exit_code is None:
        status, detail = "NOT_RUN", f"`{command}` could not start: {failure}"
    elif _checkout_state(repo_root) != before:
        status = "UNKNOWN"
        detail = (
            f"`{command}` exited {exit_code} after {elapsed:.1f}s, but the checkout changed while it ran, "
            "so the result describes no Snapshot."
        )
    else:
        status = "PASS" if exit_code == 0 else "FAIL"
        detail = f"`{command}` exited {exit_code} after {elapsed:.1f}s."
    recorded = review.request(
        "POST",
        f"{review.path}/verification",
        {
            "revision_id": revision_id,
            "name": _CHECK_NAME,
            "status": status,
            "source": "test",
            "source_ref": command[:500],
            "detail": detail,
        },
    )
    evidence = recorded.get("evidence")
    record = {
        "review_id": review_id,
        "revision_id": revision_id,
        "revision_number": revision_number,
        "evidence_id": str(evidence.get("id") or "") if isinstance(evidence, Mapping) else "",
        "name": _CHECK_NAME,
        "status": status,
        "exit_code": exit_code,
        "duration_s": round(elapsed, 3),
        "detail": detail,
        "head_sha": rng.head_sha,
    }
    return record, "\n".join(output.strip().splitlines()[-_TAIL_LINES:])


def _decide(
    review: _Review,
    targets: Sequence[str],
    decision: str,
    options: Mapping[str, Any],
    *,
    checked_revision_id: str = "",
) -> list[dict[str, Any]]:
    resolved = [review.target(value) for value in targets]
    revision_id, revision_number, session = review.revision()
    if not revision_id:
        raise _Refusal("the Review has no Snapshot to record a decision on")
    if checked_revision_id and revision_id != checked_revision_id:
        raise _Refusal(
            "a newer Snapshot arrived while the check ran, so its result does not prove a fix on it; run this again"
        )
    evidence: list[str] = list(options.get("evidence") or [])
    if decision == "fixed" and not evidence:
        evidence = review.passing_checks(revision_id)
        if not evidence:
            pushed = bool(_source_pr(session))
            numbers = " ".join(targets)
            raise _Refusal(
                "a finding is marked fixed only with a passing check on the Review's latest Snapshot. Commit the "
                + ("fix and push it, " if pushed else "fix, ")
                + f'then run `lc review finding {numbers} fixed --run "<test command>"`'
            )
    body: dict[str, Any] = {
        "finding_disposition": DECISIONS[decision],
        "finding_disposition_reason": str(options.get("reason") or "") if decision == "dismissed" else "",
        "finding_disposition_evidence": evidence if decision == "fixed" else [],
        "expected_revision_id": revision_id,
    }
    from .review_terminal import agent_session_host

    # From a coding agent's shell the decision is the agent's, whatever it was told: no flag claims the human's.
    host = agent_session_host()
    if host:
        body.update({"actor": "agent", "agent_host": host})
    done: list[dict[str, Any]] = []
    published = False
    for number, annotation in resolved:
        path = f"/api/annotations/{quote(str(annotation['id']), safe='')}"
        try:
            review.request("PATCH", path, body)
        except ClientError as exc:
            if published or _DRAFT_REFUSAL not in str(exc) or revision_number < 0:
                raise
            # The latest Snapshot is still a running draft: save it, as the Reader's own button would.
            review.publish(revision_number)
            published = True
            review.request("PATCH", path, body)
        note = str(options.get("note") or "").strip()
        if note:
            start = int(annotation.get("start_line") or 0)
            review.request(
                "POST",
                f"{review.path}/annotations",
                {
                    "path": str(annotation.get("path") or ""),
                    "body": note,
                    "kind": "comment",
                    "parent_id": str(annotation["id"]),
                    "file_level": start < 1,
                    "range": {"start_line": start, "end_line": int(annotation.get("end_line") or start)},
                    "expected_revision_id": revision_id,
                },
            )
        done.append(
            {
                "number": number,
                "annotation_id": str(annotation["id"]),
                "title": str(annotation.get("title") or ""),
                "decision": decision,
                "finding_disposition": DECISIONS[decision],
                "reason": body["finding_disposition_reason"],
                "evidence": body["finding_disposition_evidence"],
                "actor": "agent" if host else "human",
                "agent_host": host,
            }
        )
    return done


def finding_main(
    arguments: Sequence[str],
    *,
    sink: TextIO,
    store_root: Path | None = None,
    load_config: Callable[..., ClientConfig] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    run: CheckRunner = _run_command,
    capture: SnapshotCapture = _capture_snapshot,
    github: GitHubFor = _gh_for,
) -> int:
    """``lc review finding ARGUMENTS``. Exit status: 0 done, 1 refused (the line says why), 2 usage error.

    ``fixed --run`` captures its Snapshot with ``capture`` and runs the command with ``run``; a decision is
    answered on the pull request through ``github(repo_root, host)``. A pull request GitHub could not update
    still exits 0: the decision is recorded, and the line says what was not answered.
    """

    if any(argument in {"-h", "--help", "help"} for argument in arguments):
        sink.write(USAGE)
        return 0
    try:
        targets, decision, options = _parse(arguments)
    except _Refusal as exc:
        sink.write(f"{exc}\n")
        return 2
    as_json = bool(options.get("json"))
    from .review_terminal import agent_session_host

    if agent_session_host() and (decision == "confirmed" or options.get("note")):
        # The local server refuses both from an agent's process too; say why before anything runs.
        sink.write(
            "confirming a finding is the reviewer's call, so a coding agent's session cannot record it; fix it "
            '(`fixed --run "<test command>"`) or leave it open\n'
            if decision == "confirmed"
            else "--note adds a reply in the reviewer's name, so a coding agent's session cannot send it; record "
            "the skip with --reason alone (the user's words stay in the chat)\n"
        )
        return 2
    cwd = Path(str(options.get("repo_root") or "") or os.getcwd()).expanduser()
    try:
        top = _git(cwd, "rev-parse", "--show-toplevel")
        if not top:
            raise _Refusal(f"{cwd} is not inside a Git checkout")
        repo_root = Path(top).resolve()
        branch = _git(repo_root, "symbolic-ref", "--quiet", "HEAD")
        root = store_root or _store_root(os.environ)
        report = latest_report(root, repo_root, branch)
        if report is None:
            raise _Refusal(
                "no pull request review has been saved for this branch yet; lc review saves one when the agent "
                "opens a pull request"
            )
        findings = [item for item in report.get("findings") or [] if isinstance(item, dict)]
        if not targets and report.get("status") == "saving":
            # The list needs no server: the numbers are already kept.
            return _write_list(report, findings, {}, sink, as_json=as_json)
        report = _wait_until_saved(report, lambda: latest_report(root, repo_root, branch), sink, sleep)
        if load_config is None:
            from .config import load_config as load
        else:
            load = load_config
        config = load(cwd=repo_root)
        if config.hosted:
            raise _Refusal("code review findings are saved on the local Review server only")
        from .review_terminal import session_request

        review = _Review(session_request(config), report)
        if not targets:
            return _write_list(report, findings, review.annotations(), sink, as_json=as_json)
        if not decision:
            return _write_details(review, targets, sink, as_json=as_json)
        check: dict[str, Any] | None = None
        if options.get("run"):
            for value in targets:
                review.target(value)  # every number resolves before the tests run
            check, tail = _run_check(review, repo_root, str(options["run"]), config=config, run=run, capture=capture)
            if check.get("status") != "PASS":
                return _write_failed_check(check, tail, sink, as_json=as_json)
            evidence_id = str(check.get("evidence_id") or "")
            options = {**options, "evidence": [evidence_id] if evidence_id else []}
        done = _decide(
            review,
            targets,
            decision,
            options,
            checked_revision_id=str(check.get("revision_id") or "") if check else "",
        )
    except _Refusal as exc:
        sink.write(json.dumps({"error": str(exc)}) + "\n" if as_json else f"{exc}\n")
        return 1
    except ClientError as exc:
        message = f"lc review refused: {exc.message}"
        sink.write(json.dumps({"error": message}) + "\n" if as_json else f"{message}\n")
        return 1
    raw_pull = report.get("pull_request")
    host = str(raw_pull.get("github_host") or "") if isinstance(raw_pull, Mapping) else ""
    answered = _answer_on_pull_request(
        report,
        review.annotations(),
        done,
        decision,
        head_sha=str(check.get("head_sha") or "") if check else "",
        gh=github(repo_root, host),
    )
    if as_json:
        result: dict[str, Any] = {"url": str(report.get("url") or ""), "decisions": done, "pull_request": answered}
        if check is not None:
            result["check"] = check
        sink.write(json.dumps(result, sort_keys=True) + "\n")
        return 0
    if check is not None:
        sink.write(f"{_CHECK_WORDS['PASS']}: {check.get('detail') or ''}".rstrip() + "\n")
    for item in done:
        name = f"Finding {item['number']}" if item["number"] else f"Finding {item['annotation_id']}"
        why = f" ({REASON_WORDS.get(item['reason'], item['reason'])})" if item["reason"] else ""
        sink.write(f"{name} {_DONE[decision]}{why}: {item['title']}\n")
    if answered:
        sink.write(f"{answered}\n")
    return 0


def _write_failed_check(check: Mapping[str, Any], tail: str, sink: TextIO, *, as_json: bool) -> int:
    """A check that did not pass: nothing is marked, and the end of its output says why."""

    status = str(check.get("status") or "UNKNOWN")
    if as_json:
        error = f"check {_CHECK_NAME} {status}; nothing was marked fixed"
        sink.write(json.dumps({"error": error, "check": dict(check), "output_tail": tail}, sort_keys=True) + "\n")
        return 1
    sink.write(f"{_CHECK_WORDS.get(status, 'Tests ' + status)}: {check.get('detail') or ''}".rstrip() + "\n")
    if tail:
        sink.write(f"{tail}\n")
    sink.write("Nothing was marked fixed. Fix the failure, then run this again.\n")
    return 1


def _write_list(
    report: Mapping[str, Any],
    findings: Sequence[Mapping[str, Any]],
    annotations: Mapping[str, Mapping[str, Any]],
    sink: TextIO,
    *,
    as_json: bool,
) -> int:
    saving = report.get("status") == "saving"

    def state(finding: Mapping[str, Any]) -> str:
        annotation_id = str(finding.get("annotation_id") or "")
        annotation = annotations.get(annotation_id)
        if saving:
            return "saving"
        if annotation is not None:
            return _state(annotation)
        if annotation_id and annotation_id.replace("ann-", "dsc-", 1) in annotations:
            return "a comment on the Review (not on a changed line)"
        return "not opened"

    rows = [
        {
            "number": number,
            "title": str(finding.get("title") or ""),
            "path": str(finding.get("path") or ""),
            "line": int(finding.get("line") or 0),
            "state": state(finding),
        }
        for number, finding in enumerate(findings, 1)
    ]
    url, pr = str(report.get("url") or ""), _pr_number(report)
    if as_json:
        sink.write(
            json.dumps({"url": url, "pull_request": pr, "status": report.get("status"), "findings": rows}) + "\n"
        )
        return 0
    count = len(rows)
    head = f"lc review: {count} finding{'' if count == 1 else 's'}" if count else "lc review: no findings"
    head += f" on PR #{pr}" if pr else ""
    head += " (still saving)" if saving else ""
    sink.write(" · ".join(part for part in (head, url) if part) + "\n")
    for row in rows:
        sink.write(f"{row['number']}. {_label(row)} · {row['state']}\n")
    settled = {"fixed", "skipped", "confirmed", "outdated"}
    if count and all(str(row["state"]).split(" (")[0] in settled for row in rows):
        sink.write("Every finding is answered.\n")
    elif count:
        push = ", commit and push" if pr else ""
        sink.write(
            f'Fix the ones you agree with{push}, then `lc review finding N fixed --run "<test command>"`. '
            'Ask before skipping one: Skip N? <reason>. Reply "skip N" or "fix N".\n'
        )
    return 0


def _write_details(review: _Review, targets: Sequence[str], sink: TextIO, *, as_json: bool) -> int:
    shown = []
    for value in targets:
        number, annotation = review.target(value)
        shown.append(
            {
                "number": number,
                "annotation_id": str(annotation.get("id") or ""),
                "title": str(annotation.get("title") or ""),
                "path": str(annotation.get("path") or ""),
                "line": int(annotation.get("start_line") or 0),
                "state": _state(annotation),
                "body": str(annotation.get("body") or ""),
            }
        )
    if as_json:
        sink.write(json.dumps({"url": str(review.report.get("url") or ""), "findings": shown}) + "\n")
        return 0
    for item in shown:
        name = f"Finding {item['number']}" if item["number"] else f"Finding {item['annotation_id']}"
        sink.write(f"{name}: {_label(item)} · {item['state']}\n\n{item['body']}\n\n")
    return 0
