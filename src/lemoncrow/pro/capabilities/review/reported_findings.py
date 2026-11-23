"""Route a coding agent's code review of the pull request it opened into lc review and onto that pull request.

When the agent opens a pull request (``pr_trigger``), Claude's ``/code-review`` ends with one ``ReportFindings``
call, or, forked, with the same findings as a JSON block (see ``forked_review_payload`` and
``forked_skill_payload``); Codex hands its own review to ``lc review report-findings --pr``. A report that answers
no pull request is left alone: lc review saves and numbers only a pull request's code review.

The hook is fire and forget: it names the pull request's Review URL (``pull_request_review_url``, one read) and hands
the report to a detached worker (``spawn_reported_findings``). The worker first posts the findings on the pull
request as one ``COMMENT`` review from the user's ``gh`` login, numbered in report order, each on its diff line
where GitHub can hold it (``post_pull_request_review``); then it captures ``merge-base(base, head)...head`` on
the pull request's own Review, the one ``lc review --pr N`` uses, retains the report in the session ledger bound
to that Snapshot, and has the local server import it. Local server only: a hosted server cannot read this ledger.

The chat and the pull request number the findings alike. ``note_report`` keeps that numbering per checkout and
branch in ``<store>/review/reported-findings.json`` (the newest report per branch; entries older than
:data:`_REPORT_RETENTION` are dropped on every write), and the worker adds each number's annotation id and
inline comment id, so ``lc review finding N ...`` records the answer on the right finding and answers it on the
pull request.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from lemoncrow_client.config import ClientConfig

    from lemoncrow.pro.capabilities.review.gitdiff import RevRange
    from lemoncrow.pro.capabilities.review.pr_trigger import PendingPullRequest
    from lemoncrow.pro.capabilities.review.sources.github import GhRunner

_WORKER = "lemoncrow.pro.capabilities.review.reported_findings"
_REPORTS_KEY = "reports"
_REPORT_RETENTION = timedelta(days=30)
_MAX_REPORTS = 100
_MAX_BODY = 4000


@dataclass(frozen=True, slots=True)
class ReportedReview:
    url: str
    review_id: str
    revision_id: str
    findings: int


def agent_name(host: str) -> str:
    """The coding agent as the chat names it: ``claude`` is Claude."""
    from lemoncrow_client.review_findings import agent_name as name

    return name(host)


def report_message(
    rows: Sequence[Mapping[str, Any]],
    url: str,
    *,
    pr_number: int,
    listed: bool = False,
    agent: str = "Claude",
) -> str:
    """What the chat shows for one report (``rows`` as ``reported_findings_rows`` gives them).

    ``url`` is the pull request's Review (empty while the capture opens it); ``listed`` adds the numbered list,
    for a review whose own output the user never saw (a forked ``/code-review``).
    """
    count = len(rows)
    if not count:
        line = f"lc review: no problems found on PR #{pr_number}."
    else:
        line = (
            f"lc review: {count} problem{'' if count == 1 else 's'} found on PR #{pr_number}. {agent} is fixing "
            f"{'them' if count > 1 else 'it'}; details are on the PR."
        )
    lines = [line, *(f"{number}. {_row_label(row)}" for number, row in enumerate(rows, 1))] if listed else [line]
    if url:
        lines.append(f"Also in lc review: {url}")
    return "\n".join(lines)


def _row_label(row: Mapping[str, Any]) -> str:
    path, line = str(row.get("path") or ""), int(row.get("line") or 0)
    where = f"{path}:{line}" if path and line else path
    return f"{row.get('title') or 'Finding'}" + (f" ({where})" if where else "")


#: How the agent records a fix in one step; shared with ``pr_trigger``'s instructions.
FIXED_STEP = '`lc review finding N fixed --run "<test command>"`'
#: The one line the agent asks before it skips a finding.
SKIP_QUESTION = '`Skip N? <one-line reason>. Reply "skip N" or "fix N".`'
DISMISS_STEP = "`lc review finding N dismissed --reason <incorrect|accepted_risk|duplicate|not_applicable>`"


def report_context(rows: Sequence[Mapping[str, Any]], url: str, *, pull_request: PendingPullRequest) -> str:
    """``additionalContext`` for the agent: what each number means, and to fix the ones it agrees with now."""
    count = len(rows)
    number = pull_request.number
    if not count:
        return f"LemonCrow: lc review saved this code review of pull request #{number}: no findings. Nothing to do."
    lines = [
        f"LemonCrow: lc review saved these {count} findings of pull request #{number} and posts them on it as one "
        "review, numbered as the user sees them:",
        *(f"{index}. {_row_label(row)}" for index, row in enumerate(rows, 1)),
        "Fix them now, without waiting for the user:",
        f"- Each finding you agree with: fix it, commit, push to the pull request, then record it in one step with "
        f"{FIXED_STEP}. It runs the tests on the pull request's new head and marks N fixed only if they pass, then "
        "answers and resolves its comment on the pull request; on a failure it marks nothing and prints the failing "
        "output, so fix that and run it again. Several numbers go in one command: `lc review finding 1 2 fixed --run "
        '"<test command>"`.',
        f"- Each finding you would skip (not a real problem, intended, a duplicate): do not fix it and record "
        f'nothing yet. Ask the user one short line, {SKIP_QUESTION} On "skip N", run {DISMISS_STEP}, '
        'choosing the reason that matches their words; on "fix N", fix it as above.',
        '- "why N": run `lc review finding N`, then explain it in plain words; record nothing.',
        "Do not post on the pull request yourself: lc review answers each finding there when you record it. Never "
        "put an lc review link in anything posted to GitHub: it is a local address only this machine opens.",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# The numbered report, per checkout and branch
# --------------------------------------------------------------------------- #


def reports_path(store_root: Path) -> Path:
    """Read by ``lemoncrow_client.review_findings`` too (the same path, restated there)."""
    return Path(store_root).expanduser() / "review" / "reported-findings.json"


def _report_scope(payload: Mapping[str, Any]) -> tuple[Path, str] | None:
    """The checkout and branch (``refs/heads/<name>``, or ``""`` detached) a report's numbers belong to."""
    from lemoncrow.pro.capabilities.review.gitdiff import current_branch_ref, detect_repo_root

    cwd = str(payload.get("cwd") or "").strip()
    if not cwd:
        return None
    try:
        repo_root = detect_repo_root(Path(cwd)).resolve()
    except (ValueError, OSError):
        return None
    return repo_root, current_branch_ref(repo_root)


def note_report(payload: dict[str, Any], *, host: str, store_root: Path, url: str) -> dict[str, Any] | None:
    """Keep this report's numbering as the newest for its branch, before the worker saves it."""
    from lemoncrow.core.capabilities.session_comment_capture import reported_findings_rows, reported_source_id
    from lemoncrow.pro.capabilities.review.pr_trigger import update_json_state

    scope = _report_scope(payload)
    tool_input = payload.get("tool_input")
    raw = tool_input.get("findings") if isinstance(tool_input, dict) else None
    if scope is None or not isinstance(raw, list):
        return None
    repo_root, branch = scope
    pull = payload.get("pull_request")
    entry: dict[str, Any] = {
        "source_id": reported_source_id(payload),
        "host": host,
        "session_id": str(payload.get("session_id") or ""),
        "repo_root": str(repo_root),
        "branch": branch,
        "url": url,
        "pull_request": pull if isinstance(pull, dict) else None,
        "status": "saving",
        "review_id": "",
        "revision_id": "",
        "error": "",
        "created_at": datetime.now(UTC).isoformat(),
        "findings": [
            {
                "number": number,
                "key": row["key"],
                "title": row["title"],
                "path": row["path"],
                "line": row["line"],
                "body": row["body"][:_MAX_BODY],
                "annotation_id": "",
            }
            for number, row in enumerate(reported_findings_rows(raw), 1)
        ],
    }

    def put(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = [
            item
            for item in entries
            if (item.get("repo_root"), item.get("branch")) != (entry["repo_root"], entry["branch"])
        ]
        return [*kept, entry]

    update_json_state(reports_path(store_root), _REPORTS_KEY, put, retention=_REPORT_RETENTION, cap=_MAX_REPORTS)
    return entry


def _settle_report(store_root: Path, payload: Mapping[str, Any], *, host: str, **fields: Any) -> None:
    """The worker's outcome on the report ``note_report`` kept; a report it never kept is left alone."""
    from lemoncrow.core.capabilities.session_comment_capture import reported_source_id
    from lemoncrow.pro.capabilities.review.pr_trigger import update_json_state

    path = reports_path(store_root)
    if not path.is_file():
        return
    source_id = reported_source_id(dict(payload))
    session_id = str(payload.get("session_id") or "")
    # Per finding, by its key: each map is written only by the step that passes it.
    per_finding = {
        name: (fields.pop(keyword), empty)
        for keyword, name, empty in (("annotation_ids", "annotation_id", ""), ("comment_ids", "comment_id", 0))
        if keyword in fields
    }

    def settle(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for item in entries:
            if (item.get("source_id"), item.get("host"), item.get("session_id")) != (source_id, host, session_id):
                continue
            item.update(fields)
            for finding in item.get("findings") or []:
                if isinstance(finding, dict):
                    for name, (values, empty) in per_finding.items():
                        finding[name] = values.get(str(finding.get("key") or ""), empty)
        return entries

    update_json_state(path, _REPORTS_KEY, settle, retention=_REPORT_RETENTION, cap=_MAX_REPORTS)


_JSON_FENCE = re.compile(r"```json[ \t]*\n")


def _findings_block(text: str) -> list[dict[str, Any]] | None:
    """The last findings array in ``text``, fenced as ```json or bare; ``None`` when there is none."""
    decoder = json.JSONDecoder()
    starts = [match.end() for match in _JSON_FENCE.finditer(text)] or [0]
    found: list[dict[str, Any]] | None = None
    for start in starts:
        try:
            # raw_decode stops at the array's end: backticks or prose after it never matter.
            value, _ = decoder.raw_decode(text[start:].lstrip())
        except ValueError:
            continue
        if isinstance(value, list) and all(isinstance(item, dict) and "summary" in item for item in value):
            found = value
    return found


def forked_review_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """A forked ``/code-review``'s SubagentStop as the ``ReportFindings`` call it was told not to make.

    In the background, ``/code-review`` runs as a forked subagent whose output contract
    is a JSON findings block. Claude Code records the forked skill beside the subagent's
    transcript; only a ``code-review`` fork whose final message carries findings counts.
    """

    transcript = str(payload.get("agent_transcript_path") or "")
    if not transcript.endswith(".jsonl"):
        return None
    sidecar = Path(transcript.removesuffix(".jsonl") + ".forked-skill.json").expanduser()
    try:
        skill = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(skill, dict) or skill.get("skillName") != "code-review":
        return None
    findings = _findings_block(str(payload.get("last_assistant_message") or ""))
    if findings is None:
        return None
    agent = str(payload.get("agent_id") or sidecar.name.removesuffix(".forked-skill.json"))
    return {
        **payload,
        "tool_name": "ReportFindings",
        "tool_use_id": f"subagent:{agent}",
        "tool_input": {"findings": findings},
    }


def is_code_review_skill(name: object) -> bool:
    """Claude Code's ``code-review`` skill, by its own name or a plugin's ``<plugin>:code-review``."""
    text = str(name or "")
    return text == "code-review" or text.endswith(":code-review")


def forked_skill_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """A ``/code-review`` the Skill tool ran as a fork and waited for, as the ``ReportFindings`` call it was told not to make.

    Claude Code returns such a fork's final message as the Skill tool's own result
    (``{"status": "forked", "agentId": ..., "result": ...}``) and writes no ``.forked-skill.json`` sidecar,
    so its SubagentStop cannot be told apart from any other subagent's: PostToolUse(Skill) can. The id is the
    one :func:`forked_review_payload` gives the same fork, so a host that fires both saves it once.
    """

    tool_input = payload.get("tool_input")
    response = payload.get("tool_response")
    if not isinstance(tool_input, dict) or not isinstance(response, dict):
        return None
    if not is_code_review_skill(tool_input.get("skill")) or response.get("status") != "forked":
        return None
    findings = _findings_block(str(response.get("result") or ""))
    if findings is None:
        return None
    agent = str(response.get("agentId") or "")
    return {
        **payload,
        "tool_name": "ReportFindings",
        "tool_use_id": f"subagent:{agent}" if agent else str(payload.get("tool_use_id") or ""),
        "tool_input": {"findings": findings},
    }


def already_reported(payload: Mapping[str, Any], *, host: str, store_root: Path) -> bool:
    """Whether this very report (same id, host and session) was already handed to the worker."""
    from lemoncrow.core.capabilities.session_comment_capture import reported_source_id

    try:
        data = json.loads(reports_path(store_root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    entries = data.get(_REPORTS_KEY) if isinstance(data, dict) else None
    key = (reported_source_id(dict(payload)), host, str(payload.get("session_id") or ""))
    return isinstance(entries, list) and any(
        isinstance(item, dict) and (item.get("source_id"), item.get("host"), item.get("session_id")) == key
        for item in entries
    )


def pull_request_review_url(
    payload: dict[str, Any], *, store_root: Path, config: ClientConfig | None = None
) -> str | None:
    """The pull request's Review a report will land on, without capturing anything.

    Its URL when the pull request has an open Review, ``""`` when the capture will open a new one, ``None`` when
    the report is not saved at all: it answers no pull request, there is no checkout, or the configuration is
    hosted. One read of the Review list.
    """

    cwd = str(payload.get("cwd") or "").strip()
    if not cwd:
        return None
    from lemoncrow_client.config import load_config
    from lemoncrow_client.errors import ClientError

    from lemoncrow.pro.capabilities.review.gitdiff import detect_repo_root
    from lemoncrow.pro.capabilities.review.hosted import server_review_rows
    from lemoncrow.pro.capabilities.review.pr_trigger import PendingPullRequest

    pull = PendingPullRequest.from_wire(payload.get("pull_request"))
    if pull is None:
        return None
    try:
        repo_root = detect_repo_root(Path(cwd))
    except ValueError:
        return None
    config = config or load_config(cwd=repo_root)
    if config.hosted:
        return None
    try:
        rows = server_review_rows(config)
    except (ClientError, AttributeError, TypeError):
        return ""
    for row in rows:
        ref = str(row.get("ref") or "")
        if row.get("source_ref") == pull.source_ref and row.get("status") == "open" and ref.startswith("r/"):
            return config.endpoint(f"/{ref}")
    return ""


def post_pull_request_review(
    repo_root: Path,
    pull: PendingPullRequest,
    rows: Sequence[Mapping[str, Any]],
    *,
    host: str,
    runner: GhRunner | None = None,
) -> tuple[dict[str, Any], dict[str, int]]:
    """Post one report on its pull request: one ``COMMENT`` review from the user's ``gh`` login, numbered.

    Each finding GitHub can hold on a diff line gets its own inline comment; the review's body lists every
    finding with where it stands (``pull_request_review_body``) and quotes the ones with no line. A retry adopts
    the review an earlier attempt made (its hidden marker). Returns the ledger's ``github`` fields (``review_id``,
    ``url``, ``marker``, ``error``) and each inline comment's id by finding key. Never raises: a pull request that
    moved on, a ``gh`` that is missing or signed out, or a refused post is the ``error``.
    """

    import hashlib
    from dataclasses import replace

    from lemoncrow_client.review_findings import pull_request_review_body

    from lemoncrow.pro.capabilities.review.github_publish import (
        PublishCandidate,
        build_plan,
        load_pull_request_state,
        publish_plan,
        review_marker,
    )
    from lemoncrow.pro.capabilities.review.sources.github import PullRequestRef, run_gh

    owner, _, repo = pull.slug.partition("/")
    pr = PullRequestRef(owner=owner, repo=repo, number=pull.number, host=pull.github_host or "github.com")
    run: GhRunner = runner or (lambda args, stdin: run_gh(args, stdin, cwd=repo_root))
    digest = hashlib.sha256("\0".join(str(row.get("key") or "") for row in rows).encode()).hexdigest()[:10]
    operation_id = f"report-{pull.head_sha[:12]}-{digest}"
    candidates = []
    for number, row in enumerate(rows, 1):
        path, line = str(row.get("path") or ""), int(row.get("line") or 0)
        if not path or line <= 0:
            continue
        title, body = str(row.get("title") or "Finding"), str(row.get("body") or "").strip()
        rest = body[len(title) :].lstrip() if body.startswith(title) else body
        candidates.append(
            PublishCandidate(
                annotation_id=str(number),
                path=path,
                side="new",
                start_line=line,
                end_line=line,
                kind="comment",
                body=f"**{number}. {title}**" + (f"\n\n{rest}" if rest else ""),
            )
        )
    try:
        state = load_pull_request_state(pr, run)
        plan = build_plan(
            pr,
            candidates,
            commit_id=pull.head_sha or state.head_sha,
            outcome="",
            outcome_summary="",
            outcome_current=True,
            state=state,
        )
        on_diff = {int(item.annotation_id) for item in plan.inline}
        summary = pull_request_review_body(rows, {}, agent=agent_name(host), on_diff=on_diff)
        posted = publish_plan(replace(plan, summary=summary, folded=()), operation_id, state, run)
    except Exception as exc:  # detached: the ledger says what went wrong
        return {"review_id": 0, "url": "", "marker": "", "error": str(exc)[:300]}, {}
    keys = [str(row.get("key") or "") for row in rows]
    comments = {keys[int(number) - 1]: comment_id for number, comment_id in posted.comment_ids.items()}
    fields = {"review_id": posted.review_id, "url": posted.html_url, "marker": review_marker(operation_id), "error": ""}
    return fields, comments


def spawn_reported_findings(payload: dict[str, Any], *, host: str, store_root: Path) -> None:
    """Hand one report to a detached worker and return at once; the hook never waits for a capture."""
    import subprocess
    import sys

    worker = subprocess.Popen(
        [sys.executable, "-m", _WORKER, "--host", host, "--store-root", str(store_root)],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
    )
    assert worker.stdin is not None
    # The worker reads stdin before importing anything heavy, so this write never waits long.
    with worker.stdin:
        worker.stdin.write(json.dumps(payload).encode())


def main(argv: Sequence[str] | None = None) -> int:
    """The detached worker: one report on stdin, captured, recorded and imported."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog=_WORKER)
    parser.add_argument("--host", required=True)
    parser.add_argument("--store-root", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 1
    if not isinstance(payload, dict):
        return 1
    try:
        recorded = record_reported_findings(payload, host=args.host, store_root=args.store_root)
    except Exception as exc:  # detached: the ledger says what went wrong
        # Detached: nobody reads this process's output, so `lc review finding` says what went wrong.
        _settle_report(args.store_root, payload, host=args.host, status="failed", error=str(exc)[:300])
        return 1
    if recorded is None:
        _settle_report(
            args.store_root,
            payload,
            host=args.host,
            status="failed",
            error="lc review could not take a Snapshot to save this report on",
        )
        return 1
    return 0


def _git_line(repo_root: Path, *args: str) -> str:
    import subprocess

    try:
        done = subprocess.run(
            ["git", "-C", str(repo_root), *args], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def pull_request_range(repo_root: Path, pull: PendingPullRequest) -> tuple[RevRange, str]:
    """``merge-base(base, head)...head`` of *pull* and its Snapshot title, the range ``lc review --pr N`` takes.

    Local Git first (the base's remote-tracking ref and the head the hook saw); only when this clone cannot
    answer does it ask ``gh``, which also fetches the pull request's head.
    """
    from dataclasses import replace

    from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range

    head, title = pull.head_sha, f"PR #{pull.number}: {pull.title}" if pull.title else f"PR #{pull.number}"
    merge_base = (
        _git_line(repo_root, "merge-base", f"refs/remotes/origin/{pull.base_ref}", head)
        if head and pull.base_ref
        else ""
    )
    if not merge_base:
        from lemoncrow.pro.capabilities.review.sources.github import resolve_pull_request

        resolved = resolve_pull_request(repo_root, pull.url)
        head, merge_base, title = resolved.head_sha, resolved.merge_base_sha, resolved.review_title
    return replace(resolve_rev_range(repo_root, base=merge_base, head=head), title=title), title


def record_reported_findings(
    payload: dict[str, Any],
    *,
    host: str,
    store_root: Path,
    config: ClientConfig | None = None,
    runner: GhRunner | None = None,
) -> ReportedReview | None:
    """Post, capture, publish and record one pull request's reported review; ``None`` when it answers none.

    ``config`` defaults to the checkout's own client configuration, ``runner`` to the checkout's ``gh``.
    """

    tool_input = payload.get("tool_input")
    findings = tool_input.get("findings") if isinstance(tool_input, dict) else None
    cwd = str(payload.get("cwd") or "").strip()
    if not isinstance(findings, list) or not cwd:
        return None

    from lemoncrow_client.config import load_config

    from lemoncrow.core.capabilities.session_comment_capture import (
        capture_reported_findings,
        reported_findings_rows,
    )
    from lemoncrow.pro.capabilities.review.gitdiff import detect_repo_root
    from lemoncrow.pro.capabilities.review.hosted import capture_server_review, server_review_request
    from lemoncrow.pro.capabilities.review.pr_trigger import PendingPullRequest
    from lemoncrow.pro.capabilities.review.session_comments import reported_annotation_id

    pull = PendingPullRequest.from_wire(payload.get("pull_request"))
    if pull is None:
        return None
    repo_root = detect_repo_root(Path(cwd))
    config = config or load_config(cwd=repo_root)
    if config.hosted:
        return None
    rows = reported_findings_rows(findings)
    # On the pull request first, seconds after the review: the agent pushes its first fix soon, and a review
    # posted on a head the pull request has left would land on outdated lines.
    github, comment_ids = post_pull_request_review(repo_root, pull, rows, host=host, runner=runner)
    _settle_report(store_root, payload, host=host, github=github, comment_ids=comment_ids)
    # The pull request's own range and Review, never the working tree it was opened from.
    rng, title = pull_request_range(repo_root, pull)
    source_ref = pull.source_ref
    session_id = str(payload.get("session_id") or "").strip()
    # lc-debt: a branch far ahead of its remote copy captures slowly before its findings show (956 files:
    # 48 s, changed symbols 20 s); upgrade path: reuse the draft the edit hooks prewarm for this range.
    captured = capture_server_review(
        config,
        repo_root,
        rng,
        store_root=store_root,
        session_id=session_id or None,
        session_id_is_hint=True,
        limit=40,
        with_impact=True,
        with_provenance=True,
        # Impact and provenance finish in the background; the URL never waits for them.
        defer_post_ready=rng.mode in {"working_tree", "commit_range"},
        source_ref_override=source_ref,
        title_override=title,
    )
    raw_revision = captured.response.get("revision")
    revision = raw_revision if isinstance(raw_revision, dict) else {}
    revision_id = str(revision.get("id") or "")
    number = revision.get("revision_number")
    if not revision_id or not isinstance(number, int):
        return None
    review_path = f"/api/reviews/{quote(captured.review_id, safe='')}"
    server_review_request(config, "POST", f"{review_path}/revisions/{number}/publish", body={})
    capture_reported_findings(store_root, payload, host=host, review_id=captured.review_id, revision_id=revision_id)
    # Reading the annotations imports the ledger now, not on the Reader's first poll.
    server_review_request(config, "GET", f"{review_path}/annotations")
    _settle_report(
        store_root,
        payload,
        host=host,
        status="saved",
        review_id=captured.review_id,
        revision_id=revision_id,
        url=captured.canonical_url,
        annotation_ids={
            row["key"]: reported_annotation_id(captured.review_id, host, session_id, row["key"]) for row in rows
        },
    )
    return ReportedReview(
        url=captured.canonical_url,
        review_id=captured.review_id,
        revision_id=revision_id,
        findings=len(findings),
    )


if __name__ == "__main__":
    raise SystemExit(main())
