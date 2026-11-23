"""The agent opens a pull request: review it now, in the chat, on the pull request's own range.

A coding host's ``PostToolUse`` hook hands this module each tool call. ``opened_pull_request`` recognises a
successful pull request creation: ``gh pr create`` that printed the pull request's URL, or a GitHub MCP
``create_pull_request`` call. ``record_pr_trigger`` notes it once per head commit, and the hook tells the agent
to review that pull request now (``review_instruction``). When the review reports, ``claim_pull_request`` hands
the pull request to the report, so the worker posts its findings on the pull request and binds them to a Snapshot
of ``merge-base(base, head)...head``, the Review ``lc review --pr N`` uses. A report no pull request claims is
not saved.

State: ``<store>/review/pr-triggers.json``. Every write drops entries older than :data:`_RETENTION` and keeps
at most :data:`_MAX_ENTRIES`, so the file never grows past a few kilobytes. Nothing here touches the network.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

#: A head commit already reviewed is never reviewed again while its entry lives.
_RETENTION = timedelta(days=7)
_MAX_ENTRIES = 200
#: A report this long after the pull request opened is an ordinary review, not the pull request's.
CLAIM_WINDOW = timedelta(hours=1)
_GIT_TIMEOUT_S = 5.0

_PR_URL = re.compile(r"https://(?P<host>[A-Za-z0-9.-]+)/(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)/pull/(?P<number>\d+)\b")
_GH_PR_CREATE = re.compile(r"(?:^|[\s;&|(`'\"])gh\s+pr\s+create\b")
_EXIT_LINE = re.compile(r"(?:Process exited with code|Exit code:?)\s*(-?\d+)", re.IGNORECASE)
_MCP_EXIT = re.compile(r"^\[exit (-?\d+)\]", re.MULTILINE)
_SHELL_TOOLS = frozenset({"bash", "shell", "exec_command", "run_command", "unified_exec", "local_shell"})
_SHELL_OPERATORS = frozenset({"&&", "||", ";", "|", "&"})
_GH_VALUE_FLAGS = {"--base": "base", "-B": "base", "--head": "head", "-H": "head", "--title": "title", "-t": "title"}


@dataclass(frozen=True, slots=True)
class OpenedPullRequest:
    """What one tool call says about the pull request it opened."""

    url: str
    github_host: str
    slug: str
    number: int
    base_ref: str = ""
    head_ref: str = ""
    title: str = ""


@dataclass(frozen=True, slots=True)
class PendingPullRequest:
    """One opened pull request waiting for its review report, as the state file keeps it."""

    host: str
    session_id: str
    url: str
    slug: str
    number: int
    github_host: str
    repo_root: str
    base_ref: str
    head_sha: str
    title: str
    created_at: str
    claimed_at: str = ""

    @property
    def source_ref(self) -> str:
        """The durable Review identity ``lc review --pr`` gives this pull request (``PullRequestRef.source_ref``)."""

        return f"github:{self.slug}#{self.number}"

    def to_wire(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_wire(cls, raw: object) -> PendingPullRequest | None:
        if not isinstance(raw, dict):
            return None
        try:
            values = {item.name: raw[item.name] for item in fields(cls) if item.name in raw}
            pending = cls(**values)
        except (KeyError, TypeError):
            return None
        if not pending.url or not isinstance(pending.number, int) or pending.number <= 0:
            return None
        return pending


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #


def _response_text(response: object) -> str:
    """What the command printed on standard output (a host or MCP tool that gives one text: all of it)."""

    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        for key in ("stdout", "output", "aggregated_output", "formatted_output"):
            value = response.get(key)
            if isinstance(value, str) and value:
                return value
        content = response.get("content")
        return _response_text(content) if isinstance(content, list) else ""
    if isinstance(response, list):
        # MCP content blocks: [{"type": "text", "text": ...}, ...].
        return "\n".join(
            str(block.get("text") or "")
            for block in response
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _failed(response: object) -> bool:
    """A tool call that says it failed: non-zero exit, interrupted, or an error result."""

    # An MCP shell (lc bash) ends each command's output with `[exit N]`; any non-zero one fails the call.
    if any(int(code) != 0 for code in _MCP_EXIT.findall(_response_text(response))):
        return True
    if isinstance(response, dict):
        if any(response.get(key) is True for key in ("interrupted", "is_error", "isError")):
            return True
        metadata = response.get("metadata")
        for source in (response, metadata if isinstance(metadata, dict) else {}):
            for key in ("exit_code", "exitCode", "returnCode", "return_code", "code"):
                value = source.get(key)
                if value is None or (key == "code" and not isinstance(value, int)):
                    continue
                try:
                    return int(value) != 0
                except (TypeError, ValueError):
                    return True
    text = response if isinstance(response, str) else ""
    exit_line = _EXIT_LINE.search(text)
    return exit_line is not None and int(exit_line.group(1)) != 0


def _gh_create_args(rest: str) -> dict[str, str]:
    """``--base``/``--head``/``--title`` of one ``gh pr create``; nothing when the command line does not split."""

    try:
        tokens = shlex.split(rest, posix=True)
    except ValueError:
        return {}
    found: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in _SHELL_OPERATORS:
            break
        name, has_inline, inline = token.partition("=")
        if name in _GH_VALUE_FLAGS:
            if has_inline:
                found[_GH_VALUE_FLAGS[name]] = inline
            elif index + 1 < len(tokens):
                index += 1
                found[_GH_VALUE_FLAGS[name]] = tokens[index]
        index += 1
    return found


def _opened(match: re.Match[str], *, base_ref: str = "", head_ref: str = "", title: str = "") -> OpenedPullRequest:
    return OpenedPullRequest(
        url=match.group(0),
        github_host=match["host"].lower(),
        slug=f"{match['owner']}/{match['repo']}",
        number=int(match["number"]),
        base_ref=base_ref.strip(),
        # `gh pr create --head owner:branch` names a fork's branch; the branch is what this clone has.
        head_ref=head_ref.strip().rsplit(":", 1)[-1],
        title=title.strip()[:200],
    )


def opened_pull_request(payload: dict[str, Any]) -> OpenedPullRequest | None:
    """The pull request this ``PostToolUse`` call opened, or ``None``.

    Only a call that succeeded and named the new pull request counts: ``gh pr create`` whose standard output
    carries the ``https://<host>/<owner>/<repo>/pull/<n>`` line it prints on success (a failed create, such as
    "a pull request already exists", exits non-zero and prints the URL on standard error), or a GitHub MCP
    ``create_pull_request`` call whose result carries the URL.
    """

    tool = str(payload.get("tool_name") or "").strip().lower()
    raw_input = payload.get("tool_input")
    tool_input = raw_input if isinstance(raw_input, dict) else {}
    response = payload.get("tool_response")
    name = tool.rsplit("__", 1)[-1]
    if tool.startswith("mcp__") and name == "create_pull_request":
        if _failed(response):
            return None
        found = list(_PR_URL.finditer(json.dumps(response, default=str)))
        if not found:
            return None
        return _opened(
            found[0],
            base_ref=str(tool_input.get("base") or ""),
            head_ref=str(tool_input.get("head") or ""),
            title=str(tool_input.get("title") or ""),
        )
    if name not in _SHELL_TOOLS:
        return None
    raw_command = tool_input.get("command")
    # A batch (`command: [...]`) is several commands: the one that creates counts.
    commands = [str(part) for part in raw_command] if isinstance(raw_command, list) else [str(raw_command or "")]
    command = next((item for item in commands if _GH_PR_CREATE.search(item)), "")
    create = _GH_PR_CREATE.search(command)
    if create is None or _failed(response):
        return None
    printed = list(_PR_URL.finditer(_response_text(response)))
    if not printed:
        return None
    args = _gh_create_args(command[create.end() :])
    # gh prints the new pull request's URL last.
    return _opened(
        printed[-1], base_ref=args.get("base", ""), head_ref=args.get("head", ""), title=args.get("title", "")
    )


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


def state_path(store_root: Path) -> Path:
    return Path(store_root).expanduser() / "review" / "pr-triggers.json"


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_time(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def update_json_state(
    path: Path,
    key: str,
    mutate: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    *,
    retention: timedelta,
    cap: int,
    stamp: str = "created_at",
) -> list[dict[str, Any]]:
    """Locked read, *mutate*, prune and atomic write of one small JSON list ``{key: [...]}``; returns what was kept.

    Entries whose *stamp* is older than *retention* (or unreadable) are dropped and the newest *cap* are kept:
    this is the only closer the file needs.
    """

    from lemoncrow.core.foundation.run_file_io import RunFileLock, atomic_write_json

    with RunFileLock(path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        raw = data.get(key) if isinstance(data, dict) else None
        entries = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
        floor = _now() - retention
        entries = [item for item in entries if (_parse_time(item.get(stamp)) or floor) > floor]
        entries = mutate(entries)
        entries.sort(key=lambda item: str(item.get(stamp) or ""))
        entries = entries[-cap:]
        atomic_write_json(path, {"version": 1, key: entries})
    return entries


def _git(cwd: Path, *args: str) -> str:
    try:
        done = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip() if done.returncode == 0 else ""


def _head_sha(repo_root: Path, head_ref: str) -> str:
    candidates = [f"refs/heads/{head_ref}", f"refs/remotes/origin/{head_ref}"] if head_ref else ["HEAD"]
    for ref in candidates:
        sha = _git(repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
        if sha:
            return sha
    return ""


def default_base_ref(repo_root: Path) -> str:
    """The branch ``gh pr create`` targets without ``--base``: origin's default branch, from local Git state."""

    remote_head = _git(repo_root, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    return remote_head.removeprefix("origin/") if remote_head.startswith("origin/") else ""


def opened_from_url(url: str) -> OpenedPullRequest | None:
    """A pull request named by its ``https://<host>/<owner>/<repo>/pull/<n>`` URL (``--pr`` of report-findings)."""

    match = _PR_URL.fullmatch(url.strip().rstrip("/"))
    return _opened(match) if match else None


def pending_pull_request(
    opened: OpenedPullRequest, *, host: str, session_id: str, cwd: Path
) -> PendingPullRequest | None:
    """*opened* resolved against the checkout at *cwd* (head commit, base branch); ``None`` outside a checkout."""

    repo_root = _git(cwd, "rev-parse", "--show-toplevel")
    if not repo_root:
        return None
    root = Path(repo_root)
    return PendingPullRequest(
        host=host,
        session_id=session_id,
        url=opened.url,
        slug=opened.slug,
        number=opened.number,
        github_host=opened.github_host,
        repo_root=str(root),
        base_ref=opened.base_ref or default_base_ref(root),
        head_sha=_head_sha(root, opened.head_ref),
        title=opened.title,
        created_at=_now().isoformat(),
    )


def record_pr_trigger(
    store_root: Path, opened: OpenedPullRequest, *, host: str, session_id: str, cwd: Path
) -> PendingPullRequest | None:
    """Note *opened* as waiting for its review; ``None`` when its head commit was already noted or *cwd* is no checkout."""

    pending = pending_pull_request(opened, host=host, session_id=session_id, cwd=cwd)
    if pending is None:
        return None
    fresh: list[bool] = []

    def add(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen = any(item.get("url") == pending.url and item.get("head_sha") == pending.head_sha for item in entries)
        fresh.append(not seen)
        return entries if seen else [*entries, pending.to_wire()]

    update_json_state(state_path(store_root), "pull_requests", add, retention=_RETENTION, cap=_MAX_ENTRIES)
    return pending if fresh and fresh[0] else None


def local_review_configured(cwd: Path) -> bool:
    """Whether reported findings can land here: a local (not hosted) Review configuration for this checkout."""

    from lemoncrow_client.config import load_config

    try:
        return not load_config(cwd=cwd).hosted
    except Exception:
        return False


def report_command() -> str:
    """How an agent's shell runs ``lc review report-findings``: it needs the full CLI, not the installed client."""

    import shutil
    import sys

    if shutil.which("lemoncrow"):
        return "lemoncrow review report-findings"
    return f"{shlex.quote(sys.executable)} -m lemoncrow.gateway.cli review report-findings"


def claim_pull_request(store_root: Path, *, host: str, session_id: str, cwd: Path) -> PendingPullRequest | None:
    """The pull request this session opened and has not reported on yet, claimed by the report now arriving.

    The newest unclaimed one opened within :data:`CLAIM_WINDOW` in the report's checkout wins, and only while
    that checkout still sits on the pull request's head commit: a review after new commits reviews them, not
    the pull request. Claiming it means a later ordinary review in the same session captures the working
    change again.
    """

    if not session_id or not state_path(store_root).is_file():
        return None
    repo_root = _git(cwd, "rev-parse", "--show-toplevel")
    if not repo_root:
        return None
    head = _git(Path(repo_root), "rev-parse", "HEAD")
    claimed: list[PendingPullRequest] = []
    now = _now()

    def claim(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for index in range(len(entries) - 1, -1, -1):
            pending = PendingPullRequest.from_wire(entries[index])
            opened_at = _parse_time(pending.created_at) if pending else None
            if (
                pending is None
                or pending.claimed_at
                or pending.host != host
                or pending.session_id != session_id
                or pending.repo_root != repo_root
                or (pending.head_sha and pending.head_sha != head)
                or opened_at is None
                or now - opened_at > CLAIM_WINDOW
            ):
                continue
            taken = replace(pending, claimed_at=now.isoformat())
            claimed.append(taken)
            entries[index] = taken.to_wire()
            break
        return entries

    update_json_state(state_path(store_root), "pull_requests", claim, retention=_RETENTION, cap=_MAX_ENTRIES)
    return claimed[0] if claimed else None


# --------------------------------------------------------------------------- #
# What the agent is told
# --------------------------------------------------------------------------- #

_NO_LOCAL_LINK = (
    "Never put an lc review link in anything posted to GitHub: it is a local address only this machine opens."
)


def _fix_all(number: int) -> str:
    """What to do with the findings: fix every one the agent agrees with now, ask only before skipping one."""

    from lemoncrow.pro.capabilities.review.reported_findings import DISMISS_STEP, FIXED_STEP, SKIP_QUESTION

    return (
        "Then fix them without waiting for the user. Each finding you agree with: fix it, commit, push to "
        f"pull request #{number}, then record it in one step with {FIXED_STEP} (it runs the tests on the "
        "pull request's new head and marks N fixed only if they pass, then answers and resolves its comment on "
        "the pull request; on a failure it marks nothing and prints the failing output). Each finding you would "
        f'skip: record nothing yet and ask the user one short line, {SKIP_QUESTION} On "skip N", run '
        f'{DISMISS_STEP}; on "fix N", fix it as above. "why N" is `lc review finding N`, then a plain '
        "explanation. Do not post on the pull request yourself: lc review posts the findings and every answer."
    )


def review_instruction(pending: PendingPullRequest) -> str:
    """``additionalContext`` for Claude Code: run its own ``code-review`` skill on the pull request.

    Without ``--comment``: lc review posts the findings on the pull request itself, as one numbered review it
    answers as each one is decided. It also says what to do with the findings (fix the ones it agrees with, ask
    before skipping one): a background review's findings reach no hook that can add context to this
    conversation, so this is the only place the agent learns the commands.
    """

    number = pending.number
    return (
        f"LemonCrow: you just opened pull request #{number} ({pending.url}). Review it now, before you "
        f'reply: invoke the Skill tool with skill "code-review" and args "{number}". lc review saves the '
        "findings on this pull request's Review, numbers them in report order (1, 2, ...) and posts them on the "
        f"pull request as one review. {_fix_all(number)} {_NO_LOCAL_LINK}"
    )


def codex_review_instruction(pending: PendingPullRequest) -> str:
    """``additionalContext`` for Codex, which has no code-review skill: review, post with ``gh``, hand to lc review."""

    head = pending.head_sha or "HEAD"
    diff = f"git diff origin/{pending.base_ref}...{head}" if pending.base_ref else f"gh pr diff {pending.number}"
    session = f" --session-id {pending.session_id}" if pending.session_id else ""
    return (
        f"LemonCrow: you just opened pull request #{pending.number} ({pending.url}). Review it now, before you "
        "reply:\n"
        f"1. Read the change: `{diff}`.\n"
        "2. Find real bugs only (wrong behaviour, security holes, broken AGENTS.md rules); no style notes.\n"
        "3. Hand them to lc review, which posts them on the pull request as one numbered review: write a JSON "
        'array [{"file":"<path>","line":<new-side line>,"summary":"<what is wrong>","short_summary":"<a few '
        'words>","failure_scenario":"<input -> wrong result>","severity":"high|medium|low"}] ([] when clean) to a '
        f"file and run `{report_command()} --host codex{session} --pr {pending.url} <file>`. Show the user the "
        "numbered list it prints.\n"
        f"4. {_fix_all(pending.number)}\n"
        f"{_NO_LOCAL_LINK}"
    )


__all__ = [
    "CLAIM_WINDOW",
    "OpenedPullRequest",
    "PendingPullRequest",
    "claim_pull_request",
    "codex_review_instruction",
    "default_base_ref",
    "local_review_configured",
    "opened_from_url",
    "opened_pull_request",
    "pending_pull_request",
    "record_pr_trigger",
    "report_command",
    "review_instruction",
    "state_path",
    "update_json_state",
]
