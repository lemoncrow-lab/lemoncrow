"""Narrow feedback delivery adapters for review.

Only one adapter lives here today: exact Claude-session continuation. It is
purposefully not a generic agent runner. Human review feedback may be handed to
the exact authoring session when LemonCrow can prove which session that is; an
active session, an ambiguous provenance record, or a Claude CLI that forked a
copy all fail closed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.session_models import Annotation
    from lemoncrow.pro.capabilities.review.store import ReviewStore

_BG_ID_RE = re.compile(r"\b[0-9a-fA-F]{8}\b")
_DONE_BACKGROUND_STATES = frozenset({"done", "completed", "stopped", "failed"})


@dataclass(frozen=True)
class ClaudeDeliveryResult:
    state: str
    target_ref: str
    remote_ref: str = ""
    message: str = ""

    @property
    def sent(self) -> bool:
        return self.state == "sent"


def _agents(cli: str, *, cwd: Path) -> tuple[dict[str, Any], ...]:
    result = subprocess.run(
        [cli, "agents", "--json"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "claude agents failed").strip())
    try:
        payload = json.loads(result.stdout or "[]")
    except ValueError as exc:
        raise RuntimeError("claude agents returned invalid JSON") from exc
    if not isinstance(payload, list):
        raise RuntimeError("claude agents returned a non-list payload")
    return tuple(item for item in payload if isinstance(item, dict))


def _is_running(item: dict[str, Any]) -> bool:
    if str(item.get("kind") or "") == "interactive":
        return True
    state = str(item.get("state") or "").strip().lower()
    return state not in _DONE_BACKGROUND_STATES


def _dispatch_id(text: str) -> str:
    matches = _BG_ID_RE.findall(text or "")
    return matches[-1].lower() if matches else ""


def deliver_to_claude_session(session_id: str, repo_root: Path, feedback: str) -> ClaudeDeliveryResult:
    """Resume *session_id* with human feedback, or fail closed.

    ``claude --resume <id> --bg`` is the one current Claude CLI primitive that
    can continue an inactive conversation without taking over the reviewer's
    terminal. The CLI explicitly says it may create a copy when the target is
    already running. We therefore check before dispatch, verify the background
    entry afterwards, and stop a mismatched copy if a race still occurred.
    """

    session_id = session_id.strip()
    if not session_id:
        return ClaudeDeliveryResult("blocked", "", message="no exact Claude session id is available")
    cli = shutil.which("claude")
    if not cli:
        return ClaudeDeliveryResult("failed", f"claude:{session_id}", message="Claude CLI is not installed")

    target_ref = f"claude:{session_id}"
    try:
        before = _agents(cli, cwd=repo_root)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return ClaudeDeliveryResult("failed", target_ref, message=str(exc))

    active = next(
        (item for item in before if str(item.get("sessionId") or "") == session_id and _is_running(item)), None
    )
    if active is not None:
        return ClaudeDeliveryResult(
            "blocked",
            target_ref,
            message="the exact Claude session is already running; LemonCrow will not fork a copy to deliver feedback",
        )

    prompt = (
        "Human review feedback from LemonCrow follows. Apply the requested changes to this workspace, "
        "preserve comments as human-owned review state, run the relevant verification, then call the "
        "LemonCrow review_feedback_addressed tool with only the annotation IDs you actually addressed. "
        "That call records an author claim for human re-review; it does not resolve the comments. Then stop.\n\n"
        + feedback
    )
    try:
        dispatched = subprocess.run(
            [cli, "--resume", session_id, "--bg", prompt],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ClaudeDeliveryResult("failed", target_ref, message=str(exc))
    if dispatched.returncode != 0:
        message = (dispatched.stderr or dispatched.stdout or "Claude resume failed").strip()
        return ClaudeDeliveryResult("failed", target_ref, message=message)

    remote_ref = _dispatch_id("\n".join((dispatched.stdout or "", dispatched.stderr or "")))
    try:
        after = _agents(cli, cwd=repo_root)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return ClaudeDeliveryResult(
            "failed",
            target_ref,
            remote_ref=remote_ref,
            message=f"Claude started but exact-session verification failed: {exc}",
        )

    exact = next(
        (
            item
            for item in after
            if str(item.get("sessionId") or "") == session_id
            and (not remote_ref or str(item.get("id") or "").lower() == remote_ref)
        ),
        None,
    )
    if exact is not None:
        return ClaudeDeliveryResult(
            "sent", target_ref, remote_ref=remote_ref, message="feedback sent to the exact Claude session"
        )

    # If Claude raced with another process it may have created a copy. The short
    # background id identifies that copy; stop it rather than leaving a wrong
    # author running after we have already refused to call the delivery exact.
    copied = next((item for item in after if remote_ref and str(item.get("id") or "").lower() == remote_ref), None)
    if copied is not None:
        with subprocess.Popen(
            [cli, "stop", remote_ref],
            cwd=str(repo_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ):
            pass
        copied_session = str(copied.get("sessionId") or "")
        return ClaudeDeliveryResult(
            "blocked",
            target_ref,
            remote_ref=remote_ref,
            message=(
                f"Claude created a copy ({copied_session or 'unknown session'}) instead of resuming the exact session; "
                "the copy was stopped"
            ),
        )

    return ClaudeDeliveryResult(
        "failed",
        target_ref,
        remote_ref=remote_ref,
        message="Claude returned success but LemonCrow could not verify the resumed session identity",
    )


def mark_feedback_addressed(
    store: ReviewStore,
    repo_root: Path,
    annotation_ids: list[str] | tuple[str, ...],
    *,
    host: str,
    session_id: str,
) -> tuple[Annotation, ...]:
    """Record an author's *claim* that delivered human feedback was addressed.

    This never resolves a comment. The exact agent session must have previously
    received that annotation through a successful LemonCrow delivery, and every
    annotation must belong to this repository. All inputs are validated before
    the first write so a mixed valid/invalid request cannot partially mark a
    review as addressed.
    """

    host = host.strip().lower()
    session_id = session_id.strip()
    if host != "claude" or not session_id:
        raise ValueError("addressed feedback currently requires an exact Claude Code session")
    requested = tuple(dict.fromkeys(item.strip() for item in annotation_ids if item.strip()))
    if not requested:
        raise ValueError("at least one annotation id is required")

    target_ref = f"claude:{session_id}"
    root = repo_root.resolve()
    annotations: list[Annotation] = []
    for annotation_id in requested:
        annotation = store.get_annotation(annotation_id)
        if annotation is None:
            raise ValueError(f"no such review annotation {annotation_id!r}")
        review = store.get_session(annotation.review_id)
        if review is None or Path(review.repo_root).resolve() != root:
            raise ValueError(f"annotation {annotation_id!r} does not belong to this workspace")
        if annotation.source != "human" or annotation.parent_id:
            raise ValueError(f"annotation {annotation_id!r} is not a root human review comment")
        if annotation.state == "resolved":
            raise ValueError(f"annotation {annotation_id!r} is already resolved by the reviewer")
        delivered = any(
            item.target_type == "agent_session" and item.target_ref == target_ref and item.state == "sent"
            for item in store.list_deliveries(annotation.id)
        )
        if not delivered:
            raise ValueError(f"annotation {annotation_id!r} was not delivered to this exact Claude session")
        annotations.append(annotation)

    now = datetime.now(UTC).isoformat()
    updated: list[Annotation] = []
    for annotation in annotations:
        row = store.update_annotation(
            annotation.id,
            author_response="addressed",
            author_response_source_id=session_id,
            author_response_at=now,
        )
        if row is None:  # pragma: no cover - validated immediately above
            raise RuntimeError(f"annotation {annotation.id!r} disappeared while recording author response")
        updated.append(row)
    return tuple(updated)


__all__ = ["ClaudeDeliveryResult", "deliver_to_claude_session", "mark_feedback_addressed"]
