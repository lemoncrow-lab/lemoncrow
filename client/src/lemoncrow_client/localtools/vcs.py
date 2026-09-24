"""``blame``: git history is local, so this is.

The one external program this module needs is ``git``, which is already on the
machine of anyone with a checkout -- and if it is not, the answer is a typed
``local_tool_unavailable`` naming it. Nothing here installs anything.

Every invocation is read-only, has a fixed argv (no shell), and carries a
deadline. ``git`` is run with ``-C <repo root>`` rather than by changing this
process's working directory, because a tool call must not move the process the
rest of the session is using.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from typing import Any, Final

from ..errors import AgentAction, ClientError, ErrorCode
from . import LocalContext, LocalResult, text_result
from .arguments import bool_arg

__all__ = ["git_available", "run_blame", "run_git"]

_TIMEOUT_S: Final[float] = 30.0
_MAX_OUTPUT_CHARS: Final[int] = 128 * 1024
_DEFAULT_LOG_LIMIT: Final[int] = 20


def git_available() -> bool:
    return shutil.which("git") is not None


def run_git(repo_root: Any, args: Sequence[str], *, timeout_s: float = _TIMEOUT_S) -> str:
    """Run one bounded read-only git command, or raise a typed refusal."""
    if not git_available():
        raise ClientError(
            ErrorCode.LOCAL_TOOL_UNAVAILABLE,
            "git is not on PATH; the thin client never installs it",
            details={"program": "git"},
            action=AgentAction.ABANDON,
        )
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ClientError(
            ErrorCode.DEADLINE_EXCEEDED,
            f"git exceeded {timeout_s:g}s",
            details={"argv": " ".join(args[:3])},
            action=AgentAction.RETRY_LATER,
        ) from exc
    except OSError as exc:
        raise ClientError(ErrorCode.LOCAL_TOOL_FAILED, f"could not run git: {exc}", action=AgentAction.ABANDON) from exc
    if completed.returncode != 0:
        raise ClientError(
            ErrorCode.LOCAL_TOOL_FAILED,
            (completed.stderr or completed.stdout or "git failed").strip()[:512],
            details={"exit_code": completed.returncode},
            action=AgentAction.FIX_REQUEST,
        )
    return completed.stdout[:_MAX_OUTPUT_CHARS]


def run_blame(context: LocalContext, arguments: Mapping[str, Any]) -> LocalResult:
    """Line-level authorship for one path, or recent history when no path is named.

    ``include_churn`` (default true, as the contract says) heads the lines with
    how many commits touched the path and how many lines each author holds.
    """
    raw_path = arguments.get("path")
    include_churn = bool_arg("blame", arguments, "include_churn", True)

    if not isinstance(raw_path, str) or not raw_path.strip():
        # No path: the honest local answer is the repository's recent history.
        # Symbol-level attribution (``symbol_id``, ``qualified_name``) needs
        # the link index, which lives on the server.
        for unavailable in ("symbol_id", "symbol_name", "qualified_name"):
            if arguments.get(unavailable):
                raise ClientError(
                    ErrorCode.TOOL_NOT_SERVER_SIDE,
                    f"{unavailable} attribution resolves through the server index, not git alone",
                    details={"argument": unavailable},
                    action=AgentAction.RETRY_LATER,
                )
        log = run_git(
            context.repo_root,
            ["log", f"-n{_DEFAULT_LOG_LIMIT}", "--date=short", "--pretty=format:%h %ad %an  %s"],
        )
        return text_result(log or "[no commits]")

    target = context.resolve(raw_path.strip())
    relative = context.relative(target)
    lines = run_git(context.repo_root, ["blame", "-c", "--", relative]) or f"[no blame output for {relative}]"
    if not include_churn:
        return text_result(lines)

    authors: dict[str, int] = {}
    for line in run_git(context.repo_root, ["blame", "--line-porcelain", "--", relative]).splitlines():
        if line.startswith("author "):
            authors[line[7:].strip()] = authors.get(line[7:].strip(), 0) + 1
    ranked = sorted(authors.items(), key=lambda row: (-row[1], row[0]))
    commits = int(run_git(context.repo_root, ["rev-list", "--count", "HEAD", "--", relative]).strip() or 0)
    summary = "\n".join(f"{count:>6}  {name}" for name, count in ranked)
    heading = f"## {relative} — {commits} commit{'' if commits == 1 else 's'}; lines by author"
    return LocalResult(
        content=({"type": "text", "text": f"{heading}\n{summary}\n\n{lines}"},),
        structured={"path": relative, "commits": commits, "authors": dict(ranked)},
    )
