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

import contextlib
import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from lemoncrow.pro.capabilities.review.host_tools import host_executable, host_tool_environment

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.session_models import Annotation
    from lemoncrow.pro.capabilities.review.store import ReviewStore

_BG_ID_RE = re.compile(r"\b[0-9a-fA-F]{8}\b")
_DONE_BACKGROUND_STATES = frozenset({"done", "completed", "stopped", "failed"})
DIRECT_DELIVERY_HOSTS: tuple[str, ...] = ("claude", "codex", "opencode", "lemoncode", "copilot")
# Hosts LemonCrow can start fresh *and* learn the new session's id, so the
# agent's later `review_feedback_addressed` claim matches the delivery. Claude
# takes a caller-chosen id; the others report theirs in their JSON event stream.
# Copilot reports no id for a new session, so it is not offered.
NEW_SESSION_HOSTS: tuple[str, ...] = ("claude", "codex", "opencode", "lemoncode")
PENDING_SESSION_ID = "pending"
_NEW_SESSION_ID_WAIT_S = 10.0
_SESSION_ID_KEYS = ("thread_id", "sessionID", "session_id", "sessionId")
# Delivered sessions run in a checkout whose content (AGENTS.md, PR text quoted
# in feedback) may be untrusted, so every host keeps its normal approval mode:
# never a bypass/skip-permissions/allow-all flag. Codex runs headless, so it gets
# its workspace-write sandbox with network off instead of interactive approval.
# The environment stays the user's own (host_tool_environment): these are the
# user's agent CLIs and need their own auth/config variables; the sandbox and
# approval mode, not a scrubbed environment, bound what the agent can do.
_CODEX_NETWORK_OFF = ("-c", "sandbox_workspace_write.network_access=false")
_CODEX_EXEC_SANDBOX = ("-s", "workspace-write", *_CODEX_NETWORK_OFF)
_CODEX_RESUME_SANDBOX = ("-c", 'sandbox_mode="workspace-write"', *_CODEX_NETWORK_OFF)


@dataclass(frozen=True)
class AgentDeliveryResult:
    state: str
    target_ref: str
    remote_ref: str = ""
    message: str = ""
    session_id: str = ""
    """The exact session that now owns the feedback, when a launch created it."""

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
        env=host_tool_environment(),
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
    cli = host_executable("claude")
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
            env=host_tool_environment(),
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
            env=host_tool_environment(),
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
    cli = host_executable(host)
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
        # `exec resume` has no -s flag; the same sandbox goes in as config.
        command = [
            cli,
            "exec",
            "resume",
            "--json",
            *_CODEX_RESUME_SANDBOX,
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
            env=host_tool_environment(),
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


def new_session_supported(host: str) -> tuple[bool, str]:
    """Whether LemonCrow can start *host* fresh here, and why not."""

    host = host.strip().lower()
    if host not in NEW_SESSION_HOSTS:
        return False, f"LemonCrow cannot start a new {host or 'agent'} session and learn its session id"
    if host_executable(host) is None:
        return False, f"{host} CLI is not installed"
    return True, ""


def new_session_id(host: str, seed: str) -> str:
    """The id a new session will get: chosen here when the host allows it.

    Deterministic in *seed*, so a feedback preview and its send name the same
    target and a provably-unsent retry reuses it instead of forking a second id.
    """

    if host.strip().lower() == "claude":
        return str(uuid.uuid5(uuid.NAMESPACE_URL, "lemoncrow-review-handoff:" + seed))
    return PENDING_SESSION_ID


def _find_session_id(value: Any, *, depth: int = 0) -> str:
    if depth > 3:
        return ""
    if isinstance(value, dict):
        for key in _SESSION_ID_KEYS:
            found = value.get(key)
            if isinstance(found, str) and found.strip():
                return found.strip()
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                found_nested = _find_session_id(nested, depth=depth + 1)
                if found_nested:
                    return found_nested
    elif isinstance(value, list):
        for item in value:
            found_item = _find_session_id(item, depth=depth + 1)
            if found_item:
                return found_item
    return ""


def _session_id_from_log(log: Path) -> str:
    try:
        text = log.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        found = _find_session_id(event)
        if found:
            return found
    return ""


def launch_agent_session(
    host: str,
    repo_root: Path,
    feedback: str,
    *,
    session_id: str = "",
    delivery_id: str = "",
    log_dir: Path,
    wait_s: float = _NEW_SESSION_ID_WAIT_S,
    wrap_review_feedback: bool = True,
) -> AgentDeliveryResult:
    """Start a fresh *host* session in *repo_root* with the feedback, or fail closed.

    The result carries the new session's exact id so later sends resume it and
    its addressed claim matches the delivery. When a started process has not
    reported its id in time the result is ``uncertain``: it is running with the
    feedback, so it must not be launched again. ``wrap_review_feedback=False``
    sends an already-complete prompt (a Review action handoff) as is, through
    the same command line and the same tool policy.
    """

    host = host.strip().lower()
    pending_ref = f"{host}:{session_id or PENDING_SESSION_ID}"
    supported, reason = new_session_supported(host)
    if not supported:
        return AgentDeliveryResult("blocked", pending_ref, message=reason)
    cli = host_executable(host) or host
    prompt = _feedback_prompt(feedback, delivery_id=delivery_id) if wrap_review_feedback else feedback
    if host == "claude":
        if not session_id or session_id == PENDING_SESSION_ID:
            return AgentDeliveryResult("blocked", pending_ref, message="a new Claude session needs a chosen session id")
        try:
            started = subprocess.run(
                [cli, "--bg", "--session-id", session_id, prompt],
                cwd=str(repo_root),
                env=host_tool_environment(),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return AgentDeliveryResult("failed", pending_ref, message=str(exc))
        if started.returncode != 0:
            message = (started.stderr or started.stdout or "Claude could not start a new session").strip()
            return AgentDeliveryResult("failed", pending_ref, message=message)
        return AgentDeliveryResult(
            "working",
            pending_ref,
            remote_ref=_dispatch_id("\n".join((started.stdout or "", started.stderr or ""))),
            message="a new Claude session started with the feedback; consumption is not yet separately acknowledged",
            session_id=session_id,
        )

    if host == "codex":
        command = [cli, "exec", "--json", *_CODEX_EXEC_SANDBOX, prompt]
    else:  # opencode / lemoncode: the same flags as their verified resume path
        command = [
            cli,
            "run",
            "--format",
            "json",
            "--dir",
            str(repo_root),
            prompt,
        ]
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / f"{host}-{uuid.uuid4().hex}.jsonl"
    try:
        with log.open("wb") as sink:
            process = subprocess.Popen(
                command,
                cwd=str(repo_root),
                env=host_tool_environment(),
                stdin=subprocess.DEVNULL,
                stdout=sink,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError as exc:
        with contextlib.suppress(OSError):
            log.unlink()
        return AgentDeliveryResult("failed", pending_ref, message=str(exc))
    remote_ref = f"pid:{process.pid}"
    found = ""
    deadline = time.monotonic() + max(0.0, wait_s)
    try:
        while True:
            found = _session_id_from_log(log)
            if found:
                break
            returncode = process.poll()
            if returncode is not None:
                found = _session_id_from_log(log)
                if found:
                    break
                if returncode != 0:
                    return AgentDeliveryResult(
                        "failed",
                        pending_ref,
                        remote_ref=remote_ref,
                        message=f"new {host} session exited with status {returncode} before reporting its id",
                    )
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.1)
    finally:
        # The process keeps its open descriptor; unlinking only drops the name.
        with contextlib.suppress(OSError):
            log.unlink()
    if not found:
        return AgentDeliveryResult(
            "uncertain",
            pending_ref,
            remote_ref=remote_ref,
            message=(
                f"a new {host} session started with the feedback but has not reported its session id; "
                "LemonCrow will not start another one"
            ),
        )
    return AgentDeliveryResult(
        "working",
        f"{host}:{found}",
        remote_ref=remote_ref,
        message=f"a new {host} session started with the feedback; consumption is not yet separately acknowledged",
        session_id=found,
    )


def running_claude_sessions(repo_root: Path) -> tuple[tuple[str, str], ...]:
    """``(session_id, state)`` of Claude agents running in *repo_root* right now.

    Best effort: an absent CLI or a failed listing is simply no sessions.
    """

    cli = host_executable("claude")
    if not cli:
        return ()
    try:
        items = _agents(cli, cwd=repo_root)
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return ()
    root = repo_root.resolve()
    found: list[tuple[str, str]] = []
    for item in items:
        session_id = str(item.get("sessionId") or "").strip()
        cwd = str(item.get("cwd") or "").strip()
        if not session_id or not cwd or not _is_running(item):
            continue
        try:
            resolved_cwd = Path(cwd).resolve()
        except OSError:
            resolved_cwd = None
        if resolved_cwd != root:
            continue
        found.append((session_id, str(item.get("state") or item.get("kind") or "")))
    return tuple(found)


def workspace_agent_session(repo_root: Path) -> tuple[str, str, str] | None:
    """``(host, session_id, activity)`` of the session last active in *repo_root*."""

    try:
        from lemoncrow.core.foundation.paths import resolve_workspace_store_dir

        state = json.loads(
            (resolve_workspace_store_dir(workspace_root=repo_root) / "session_state.json").read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    host = str(state.get("host") or "").strip().lower()
    session_id = str(state.get("session_id") or "").strip()
    if not host or not session_id:
        return None
    return host, session_id, str(state.get("activity") or "").strip().lower()


def workspace_session_activity_at(repo_root: Path, host: str, session_id: str) -> str:
    """Last recorded activity, only when the bridge still names this exact session."""

    try:
        from lemoncrow.core.foundation.paths import resolve_workspace_store_dir

        state = json.loads(
            (resolve_workspace_store_dir(workspace_root=repo_root) / "session_state.json").read_text(encoding="utf-8")
        )
    except (OSError, TypeError, ValueError):
        return ""
    if not isinstance(state, dict):
        return ""
    if str(state.get("host") or "").strip().lower() != host or str(state.get("session_id") or "").strip() != session_id:
        return ""
    return str(state.get("activity_at") or "").strip()


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
    # Stage consumption until every annotation has been validated. A rejected
    # batch must not consume an earlier delivery without an author response.
    consume_by_annotation: dict[str, tuple[str, str]] = {}
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
        if operation_id:
            consume_by_annotation[annotation.id] = (
                operation_id,
                str(getattr(delivery, "remote_ref", "") or ""),
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
            if annotation.id in consume_by_annotation and hasattr(store, "update_delivery_target"):
                operation_id, remote_ref = consume_by_annotation[annotation.id]
                store.update_delivery_target(
                    operation_id, target_ref, state="consumed", remote_ref=remote_ref, last_error=""
                )
            updated.append(row)
    return tuple(updated)


__all__ = [
    "DIRECT_DELIVERY_HOSTS",
    "NEW_SESSION_HOSTS",
    "PENDING_SESSION_ID",
    "AgentDeliveryResult",
    "ClaudeDeliveryResult",
    "deliver_prompt_to_agent_session",
    "deliver_to_agent_session",
    "deliver_to_claude_session",
    "direct_delivery_supported",
    "launch_agent_session",
    "mark_feedback_addressed",
    "new_session_id",
    "new_session_supported",
    "running_claude_sessions",
    "workspace_agent_session",
]
