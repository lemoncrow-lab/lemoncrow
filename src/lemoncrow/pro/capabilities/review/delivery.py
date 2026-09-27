"""Feedback delivery adapters for exact review-author sessions.

The review protocol is host-neutral: a prepared feedback operation is bound to
one review revision, one exact feedback payload, and one exact author session.
Host adapters only answer the last-mile question: how does this particular CLI
continue that session without silently creating unrelated work?

Claude gets extra post-dispatch verification because its background resume path
may fork a copy when the target is already active. Codex, OpenCode, and Copilot
expose explicit session-id continuation flags; those adapters launch the exact
resume command and return once the process is known to have started.
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
DIRECT_DELIVERY_HOSTS: tuple[str, ...] = ("claude", "codex", "opencode", "lemoncode", "copilot")


@dataclass(frozen=True)
class AgentDeliveryResult:
    state: str
    target_ref: str
    remote_ref: str = ""
    message: str = ""

    @property
    def sent(self) -> bool:
        """Compatibility alias for a handoff with proven consumption."""

        return self.state in {"sent", "consumed"}

    @property
    def accepted(self) -> bool:
        """The host accepted ownership; consumption may still be pending."""

        return self.state in {"sent", "consumed", "working", "queued", "leased"}


# Compatibility for existing imports/tests while callers migrate to the generic name.
ClaudeDeliveryResult = AgentDeliveryResult


def direct_delivery_supported(host: str) -> bool:
    return host.strip().lower() in DIRECT_DELIVERY_HOSTS


def _feedback_prompt(feedback: str, *, delivery_id: str = "") -> str:
    identity = f"LemonCrow delivery ID: {delivery_id}. " if delivery_id else ""
    replay = (
        "If this same delivery ID is presented again, treat it as an idempotent replay rather than a new request. "
        if delivery_id or feedback.startswith("LemonCrow delivery ID:")
        else ""
    )
    return (
        identity + "Human review feedback from LemonCrow follows. Apply the requested changes to this workspace, "
        "preserve comments as human-owned review state, run the relevant verification, then call the "
        "LemonCrow review_feedback_addressed tool with only the annotation IDs you actually addressed. "
        "That call records an author claim for human re-review; it does not resolve the comments. "
        + replay
        + "Then stop.\n\n"
        + feedback
    )


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


def deliver_to_claude_session(
    session_id: str,
    repo_root: Path,
    feedback: str,
    *,
    wrap_review_feedback: bool = True,
) -> AgentDeliveryResult:
    """Resume *session_id* with human feedback, or fail closed.

    ``claude --resume <id> --bg`` is the current Claude CLI primitive that can
    continue an inactive conversation without taking over the reviewer's
    terminal. Claude may create a copy if the session races active, so this
    adapter probes before dispatch and verifies the exact session afterwards.
    """

    session_id = session_id.strip()
    if not session_id:
        return AgentDeliveryResult("blocked", "", message="no exact Claude session id is available")
    cli = shutil.which("claude")
    if not cli:
        return AgentDeliveryResult("failed", f"claude:{session_id}", message="Claude CLI is not installed")

    target_ref = f"claude:{session_id}"
    try:
        before = _agents(cli, cwd=repo_root)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        return AgentDeliveryResult("failed", target_ref, message=str(exc))

    active = next(
        (item for item in before if str(item.get("sessionId") or "") == session_id and _is_running(item)), None
    )
    prompt = _feedback_prompt(feedback) if wrap_review_feedback else feedback
    if active is not None:
        # Resuming an already-running Claude session may fork a copy. Put the
        # feedback in LemonCrow's exact-session inbox instead; UserPromptSubmit
        # atomically claims it for this same session on the next model turn.
        try:
            from lemoncrow.core.foundation.paths import default_store_root
            from lemoncrow.pro.capabilities.review.session_inbox import enqueue_session_message

            inbox_message = enqueue_session_message(
                default_store_root(),
                host="claude",
                session_id=session_id,
                message=prompt,
            )
        except (OSError, TypeError, ValueError) as exc:
            return AgentDeliveryResult("failed", target_ref, message=f"could not queue feedback: {exc}")
        return AgentDeliveryResult(
            "queued",
            target_ref,
            remote_ref=f"inbox:{inbox_message.id}",
            message="feedback queued to the exact running Claude session; it will be injected on its next model turn",
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
        return AgentDeliveryResult("failed", target_ref, message=str(exc))
    if dispatched.returncode != 0:
        message = (dispatched.stderr or dispatched.stdout or "Claude resume failed").strip()
        return AgentDeliveryResult("failed", target_ref, message=message)

    remote_ref = _dispatch_id("\n".join((dispatched.stdout or "", dispatched.stderr or "")))
    try:
        after = _agents(cli, cwd=repo_root)
    except (OSError, subprocess.SubprocessError, RuntimeError) as exc:
        # Dispatch succeeded. Calling this a clean failure would invite an
        # automatic retry that could apply the same correction twice.
        return AgentDeliveryResult(
            "uncertain",
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
        return AgentDeliveryResult(
            "working",
            target_ref,
            remote_ref=remote_ref,
            message="the exact Claude session is running with the feedback handoff; consumption is not yet separately acknowledged",
        )

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
        return AgentDeliveryResult(
            "blocked",
            target_ref,
            remote_ref=remote_ref,
            message=(
                f"Claude created a copy ({copied_session or 'unknown session'}) instead of resuming the exact session; "
                "the copy was stopped"
            ),
        )

    return AgentDeliveryResult(
        "uncertain",
        target_ref,
        remote_ref=remote_ref,
        message="Claude returned success but LemonCrow could not verify the resumed session identity",
    )


def _workspace_session_activity(repo_root: Path, session_id: str, *, host: str) -> str:
    """Return the exact host/session activity bridge, failing closed."""

    try:
        from lemoncrow.core.foundation.paths import resolve_workspace_store_dir

        state_path = resolve_workspace_store_dir(workspace_root=repo_root) / "session_state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return ""
    if not isinstance(state, dict):
        return ""
    if str(state.get("host") or "").strip().lower() != host.strip().lower():
        return ""
    if str(state.get("session_id") or "").strip() != session_id:
        return ""
    return str(state.get("activity") or "").strip().lower()


def _opencode_session_is_active(repo_root: Path, session_id: str, *, host: str = "opencode") -> bool:
    """OpenCode/LemonCode keep `active` while the exact session is live."""

    return _workspace_session_activity(repo_root, session_id, host=host) == "active"


def _copilot_session_is_working(repo_root: Path, session_id: str) -> bool:
    """Copilot can accept in-process Review feedback at the active turn boundary."""

    return _workspace_session_activity(repo_root, session_id, host="copilot") == "working"


def _spawn_resume(
    host: str,
    session_id: str,
    repo_root: Path,
    feedback: str,
    *,
    wrap_review_feedback: bool = True,
    delivery_id: str = "",
) -> AgentDeliveryResult:
    cli = shutil.which(host)
    target_ref = f"{host}:{session_id}"
    if not cli:
        return AgentDeliveryResult("failed", target_ref, message=f"{host} CLI is not installed")
    prompt = _feedback_prompt(feedback, delivery_id=delivery_id) if wrap_review_feedback else feedback
    if host in {"opencode", "lemoncode"} and _opencode_session_is_active(repo_root, session_id, host=host):
        try:
            from lemoncrow.core.foundation.paths import default_store_root
            from lemoncrow.pro.capabilities.review.session_inbox import enqueue_session_message

            queued = enqueue_session_message(
                default_store_root(),
                host=host,
                session_id=session_id,
                message=prompt,
            )
        except (OSError, TypeError, ValueError) as exc:
            return AgentDeliveryResult("failed", target_ref, message=f"could not queue feedback: {exc}")
        return AgentDeliveryResult(
            "queued",
            target_ref,
            remote_ref=f"inbox:{queued.id}",
            message=f"feedback queued to the exact running {host} session; it will be injected on its next model turn",
        )
    if host == "copilot" and _copilot_session_is_working(repo_root, session_id):
        try:
            from lemoncrow.core.foundation.paths import default_store_root
            from lemoncrow.pro.capabilities.review.session_inbox import enqueue_session_message

            queued = enqueue_session_message(
                default_store_root(),
                host="copilot",
                session_id=session_id,
                message=prompt,
            )
        except (OSError, TypeError, ValueError) as exc:
            return AgentDeliveryResult("failed", target_ref, message=f"could not queue feedback: {exc}")
        return AgentDeliveryResult(
            "queued",
            target_ref,
            remote_ref=f"inbox:{queued.id}",
            message=(
                "feedback queued to the exact working Copilot session; "
                "agentStop will inject it as an immediate continuation"
            ),
        )
    if host == "codex":
        # Not `codex queue`: it accepts a message for an idle thread and exits 0,
        # but nothing consumes it (verified live against codex-cli 0.155.1 --
        # even `exec resume` of that thread ignores it), so the Review would show
        # feedback as queued while it silently stalls. `exec resume` runs it.
        # lc-debt: no probe says whether a Codex thread is attached right now;
        # route live threads through `queue` once one exists.
        command = [
            cli,
            "exec",
            "resume",
            "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            session_id,
            prompt,
        ]
    elif host in {"opencode", "lemoncode"}:
        command = [
            cli,
            "run",
            "--session",
            session_id,
            "--format",
            "json",
            "--dir",
            str(repo_root),
            "--dangerously-skip-permissions",
            prompt,
        ]
    elif host == "copilot":
        command = [
            cli,
            f"--resume={session_id}",
            "-p",
            prompt,
            "-C",
            str(repo_root),
            "--allow-all",
            "--output-format",
            "json",
            "--stream",
            "off",
            "--no-ask-user",
        ]
    else:  # pragma: no cover - guarded by deliver_to_agent_session
        return AgentDeliveryResult("blocked", target_ref, message=f"direct feedback delivery is unsupported for {host}")

    try:
        process = subprocess.Popen(
            command,
            cwd=str(repo_root),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        return AgentDeliveryResult("failed", target_ref, message=str(exc))

    # Catch immediate argument/session errors without turning a long agent run
    # into a synchronous HTTP request. Once the process survives this window the
    # explicit session-id flag is the continuation contract for these CLIs.
    try:
        returncode = process.wait(timeout=0.35)
    except subprocess.TimeoutExpired:
        return AgentDeliveryResult(
            "working",
            target_ref,
            remote_ref=f"pid:{process.pid}",
            message=f"exact {host} resume process started; feedback consumption is still in flight",
        )
    if returncode == 0:
        return AgentDeliveryResult(
            "consumed",
            target_ref,
            remote_ref=f"pid:{process.pid}",
            message=f"the exact {host} resume completed after consuming the feedback",
        )
    return AgentDeliveryResult(
        "failed",
        target_ref,
        remote_ref=f"pid:{process.pid}",
        message=f"{host} exact-session resume exited with status {returncode}",
    )


def deliver_to_agent_session(
    host: str,
    session_id: str,
    repo_root: Path,
    feedback: str,
    *,
    delivery_id: str = "",
) -> AgentDeliveryResult:
    """Continue one exact authoring session using that host's verified CLI primitive."""

    host = host.strip().lower()
    session_id = session_id.strip()
    target_ref = f"{host}:{session_id}" if host and session_id else ""
    if not host or not session_id:
        return AgentDeliveryResult("blocked", target_ref, message="no exact author session is available")
    if host == "claude":
        claude_feedback = f"LemonCrow delivery ID: {delivery_id}\n\n{feedback}" if delivery_id else feedback
        return deliver_to_claude_session(session_id, repo_root, claude_feedback)
    if host in {"codex", "opencode", "lemoncode", "copilot"}:
        return _spawn_resume(host, session_id, repo_root, feedback, delivery_id=delivery_id)
    return AgentDeliveryResult(
        "blocked",
        target_ref,
        message=f"direct feedback delivery is not configured for {host}; copy/export remains available",
    )


def deliver_prompt_to_agent_session(
    host: str,
    session_id: str,
    repo_root: Path,
    prompt: str,
) -> AgentDeliveryResult:
    """Continue one exact authoring session with a generic contextual prompt."""

    def as_prompt_result(result: AgentDeliveryResult) -> AgentDeliveryResult:
        return AgentDeliveryResult(
            result.state,
            result.target_ref,
            remote_ref=result.remote_ref,
            message=result.message.replace("Feedback", "Prompt").replace("feedback", "prompt"),
        )

    host = host.strip().lower()
    session_id = session_id.strip()
    target_ref = f"{host}:{session_id}" if host and session_id else ""
    if not host or not session_id:
        return AgentDeliveryResult("blocked", target_ref, message="no exact author session is available")
    if host == "claude":
        return as_prompt_result(
            deliver_to_claude_session(
                session_id,
                repo_root,
                prompt,
                wrap_review_feedback=False,
            )
        )
    if host in {"codex", "opencode", "lemoncode", "copilot"}:
        return as_prompt_result(
            _spawn_resume(
                host,
                session_id,
                repo_root,
                prompt,
                wrap_review_feedback=False,
            )
        )
    return AgentDeliveryResult(
        "blocked",
        target_ref,
        message=f"direct prompt delivery is not configured for {host}",
    )


def mark_feedback_addressed(
    store: ReviewStore,
    repo_root: Path,
    annotation_ids: list[str] | tuple[str, ...],
    *,
    host: str,
    session_id: str,
) -> tuple[Annotation, ...]:
    """Record an exact author session's claim that delivered feedback was addressed.

    This never resolves a comment. The same host/session must have received the
    current semantic annotation version through a successful LemonCrow delivery.
    Editing the request after delivery therefore invalidates the old claim.
    """

    host = host.strip().lower()
    session_id = session_id.strip()
    if not host or not session_id:
        raise ValueError("addressed feedback requires an exact agent session")
    requested = tuple(dict.fromkeys(item.strip() for item in annotation_ids if item.strip()))
    if not requested:
        raise ValueError("at least one annotation id is required")

    target_ref = f"{host}:{session_id}"
    root = repo_root.resolve()
    annotations: list[Annotation] = []
    delivery_revision_by_annotation: dict[str, str] = {}
    delivered_version_by_annotation: dict[str, int] = {}
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
        if annotation.author_response == "addressed" and annotation.author_response_source_id == session_id:
            annotations.append(annotation)
            continue
        delivered = [
            item
            for item in store.list_deliveries(annotation.id)
            if item.target_type == "agent_session"
            and item.target_ref == target_ref
            and item.state in {"working", "queued", "sent", "consumed"}
        ]
        if not delivered:
            raise ValueError(f"annotation {annotation_id!r} was not delivered to this exact agent session")
        delivery = delivered[-1]
        versions = store.list_annotation_versions(annotation.id)
        current_version = versions[-1].version_number if versions else 0
        if delivery.annotation_version <= 0:
            raise ValueError(
                f"annotation {annotation_id!r} delivery predates version binding; resend the feedback before claiming it addressed"
            )
        if delivery.annotation_version != current_version:
            raise ValueError(
                f"annotation {annotation_id!r} changed after delivery "
                f"(sent version {delivery.annotation_version}, current version {current_version})"
            )
        operation_id = str(getattr(delivery, "operation_id", "") or "")
        if operation_id and hasattr(store, "update_delivery_target"):
            store.update_delivery_target(
                operation_id,
                target_ref,
                state="consumed",
                remote_ref=str(getattr(delivery, "remote_ref", "") or ""),
                last_error="",
            )
        delivery_revision_by_annotation[annotation.id] = delivery.revision_id
        delivered_version_by_annotation[annotation.id] = current_version
        annotations.append(annotation)

    from lemoncrow.pro.capabilities.review.store import AnnotationHistoryContext, AnnotationVersionConflict

    now = datetime.now(UTC).isoformat()
    updated: list[Annotation] = []
    with store.atomic_review_transition():
        for annotation in annotations:
            if annotation.author_response == "addressed" and annotation.author_response_source_id == session_id:
                updated.append(annotation)
                continue
            try:
                row = store.update_annotation(
                    annotation.id,
                    AnnotationHistoryContext(
                        revision_id=delivery_revision_by_annotation.get(annotation.id, annotation.revision_id),
                        changed_by=session_id,
                        changed_by_actor="agent",
                        change_kind="author_response",
                        expected_version=delivered_version_by_annotation.get(annotation.id),
                    ),
                    author_response="addressed",
                    author_response_source_id=session_id,
                    author_response_at=now,
                )
            except AnnotationVersionConflict as exc:
                raise ValueError(f"annotation {annotation.id!r} changed after delivery; resend the feedback") from exc
            if row is None:  # pragma: no cover - validated immediately above
                raise RuntimeError(f"annotation {annotation.id!r} disappeared while recording author response")
            updated.append(row)
    return tuple(updated)


__all__ = [
    "DIRECT_DELIVERY_HOSTS",
    "AgentDeliveryResult",
    "ClaudeDeliveryResult",
    "deliver_prompt_to_agent_session",
    "deliver_to_agent_session",
    "deliver_to_claude_session",
    "direct_delivery_supported",
    "mark_feedback_addressed",
]
