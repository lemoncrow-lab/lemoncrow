"""One tool call in, one result out -- routed by the table and nothing else.

This is the only place in the client where a tool name becomes a decision, and
the decision is a table lookup:

``Site.CLIENT``
    Run the named local executor. If the route says ``pushes_blobs``, the paths
    it wrote are pushed and an overlay revision is committed *before* the
    result is returned. That ordering is the read-after-edit guarantee.

``Site.SERVER`` / ``Site.SERVER_ADMIN``
    Call the server, naming the revision this client has observed. If the
    server was never reachable and the route has an ``offline`` executor, run
    that instead and flag the answer degraded. If it *was* reachable and has
    gone away, return the typed error -- the degradation table says
    mid-session loss is an error the agent can act on, not a silent fallback to
    a different answer.

``Site.CLIENT_UPLOAD_SERVER_BUILD``
    The sync protocol, via the ``index`` executor.

There is no fifth branch and no default. An unrouted tool raises ``tool_unknown``
in :func:`lemoncrow_client.routing.route_for`.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from .config import ClientConfig
from .errors import AgentAction, ClientError, ErrorCode, error_block
from .localtools import LocalContext, LocalResult, executor_for
from .localtools.shell import background_running, close_background
from .routing import Route, Site, route_for
from .session import RemoteSession
from .trace import ClientTraceWriter, new_trace_id, trace_scope
from .worktrees import LinkedWorktree, WorktreeCatalog

__all__ = ["Dispatcher", "ToolOutcome"]

#: Worktree sessions kept open at once. Each owns an independent checkout
#: view; content-addressed blobs let unchanged file content be reused. The bound
#: keeps a long session's live-view cost flat.
_MAX_WORKTREE_SESSIONS: Final[int] = 3
#: A worktree session that failed to open is not retried on every call.
_WORKTREE_RETRY_S: Final[float] = 30.0


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """What the MCP layer hands back to the host."""

    content: tuple[Mapping[str, Any], ...]
    is_error: bool = False
    degraded: bool = False
    degraded_reason: str = ""
    #: Where it actually ran, so a test (and a transcript) can tell.
    site: str = ""
    view_revision: int | None = None
    structured: Mapping[str, Any] | None = None
    #: The call's runtime trace id (``lt_...``). Not sent to the model; look
    #: it up with ``lemoncrow trace show``.
    trace_id: str = ""
    #: The session/view that answered, set only when the call ran in a worktree's
    #: own session, so a result cache never mixes two checkouts.
    session_id: str = ""
    view_id: str = ""

    def to_mcp(self, *, include_structured: bool = False) -> dict[str, Any]:
        """Render an MCP tool result for a consumer boundary.

        The default is intentionally model-facing and text-only. ``content`` is
        already the compact/rendered representation chosen for an agent; sending
        the same result again as ``structuredContent`` duplicates context and can
        cause hosts such as Claude Code to prefer raw machine metadata over the
        curated Markdown/text. Programmatic consumers that explicitly need the
        structured payload must opt in with ``include_structured=True``.
        """
        payload: dict[str, Any] = {
            "content": [dict(block) for block in self.content],
            "isError": self.is_error,
        }
        if include_structured and self.structured is not None:
            payload["structuredContent"] = dict(self.structured)
        if self.degraded:
            payload["_meta"] = {
                "lemoncrow/degraded": True,
                "lemoncrow/degraded_reason": self.degraded_reason,
            }
        return payload


class Dispatcher:
    """Routes one tool call and owns session-scoped checkout routing state."""

    __slots__ = (
        "_active_worktree",
        "_catalog",
        "_child_attempts",
        "_child_lock",
        "_children",
        "_config",
        "_context",
        "_routing_lock",
        "_session",
        "_traces",
    )

    def __init__(self, config: ClientConfig, session: RemoteSession) -> None:
        self._config = config
        self._session = session
        self._catalog = WorktreeCatalog(config.repo_root)
        self._active_worktree: LinkedWorktree | None = None
        self._routing_lock = threading.Lock()
        self._children: OrderedDict[Path, Dispatcher] = OrderedDict()
        self._child_attempts: dict[Path, float] = {}
        self._child_lock = threading.Lock()
        self._traces = ClientTraceWriter(config.state_dir, hosted=bool(getattr(config, "hosted", False)))
        self._context = LocalContext(
            config=config,
            repo_root=config.repo_root,
            sync=_SyncBridge(session),
            environment=dict(os.environ),
        )

    @property
    def session(self) -> RemoteSession:
        return self._session

    def background_running(self) -> bool:
        """Whether a ``bash bg=true`` run of this session is still alive."""
        with self._child_lock:
            children = list(self._children.values())
        return background_running(self._context.memory) or any(child.background_running() for child in children)

    def close_background(self) -> int:
        """Kill this session's ``bash bg=true`` runs; nothing outlives the session."""
        with self._child_lock:
            children = list(self._children.values())
        return close_background(self._context.memory) + sum(child.close_background() for child in children)

    def close_worktrees(self) -> None:
        """Close every worktree session this one opened."""
        with self._child_lock:
            children = list(self._children.values())
            self._children.clear()
        for child in children:
            child.close_background()
            child.session.close()

    def call(self, tool: str, arguments: Mapping[str, Any]) -> ToolOutcome:
        """Dispatch, turning every typed refusal into an agent-readable result.

        Mints the call's trace id; server-routed calls carry it as their
        request id so every layer's trace record shares it.
        """
        trace_id = new_trace_id()
        started_at = datetime.now(UTC)
        started = time.perf_counter()
        recoveries_before = len(getattr(getattr(self._session, "state", None), "recovery_events", ()) or ())
        routed = None
        with trace_scope(trace_id):
            try:
                # project_id is a persistent-gateway concern. Refuse it before
                # worktree routing so it can never acquire a second meaning.
                self._refuse_project_routing(arguments)
                with self._routing_lock:
                    active = self._active_worktree
                routed = self._catalog.route(arguments, active=active)
                route = route_for(tool)
                if routed.worktree is None:
                    outcome = self._route(route, routed.arguments)
                else:
                    child = self._worktree_session(routed.worktree)
                    outcome = replace(
                        child._route(route, routed.arguments),
                        session_id=child.session.state.session_id,
                        view_id=child.session.state.view_id,
                    )
                if not outcome.is_error and routed.explicit:
                    with self._routing_lock:
                        self._active_worktree = routed.worktree
                if routed.sticky and routed.worktree is not None:
                    outcome = _with_worktree_header(outcome, routed.worktree)
            except ClientError as exc:
                outcome = ToolOutcome(content=(error_block(exc),), is_error=True, site="refused")
        self._trace(trace_id, tool, arguments, started_at, started, outcome, recoveries_before)
        return replace(outcome, trace_id=trace_id)

    def _worktree_session(self, worktree: LinkedWorktree) -> Dispatcher:
        """The open session for a linked worktree, created on first use.

        Sessions are opened lazily and kept least-recently-used, so an unused
        worktree costs nothing and a long-lived client has a fixed upper bound.
        """
        with self._child_lock:
            child = self._children.pop(worktree.root, None)
            if child is None:
                config = replace(self._config, repo_root=worktree.root)
                child = Dispatcher(config, RemoteSession(config))
            self._children[worktree.root] = child
            for root, idle in list(self._children.items()):
                if len(self._children) <= _MAX_WORKTREE_SESSIONS:
                    break
                if root != worktree.root and not idle.background_running():
                    del self._children[root]
                    idle.session.close()
            session = child.session
            if not session.bootstrapped:
                now = time.monotonic()
                if now - self._child_attempts.get(worktree.root, float("-inf")) >= _WORKTREE_RETRY_S:
                    self._child_attempts[worktree.root] = now
                    try:
                        report = session.bootstrap(deadline_s=self._config.startup_budget_s)
                    except ClientError as exc:
                        session.state.go_offline(f"{exc.server_code}: {exc.message}")
                    else:
                        if not report.ok:
                            session.state.go_offline(report.reason)
            return child

    def _refuse_project_routing(self, arguments: Mapping[str, Any]) -> None:
        """``project_id`` needs the persistent gateway; never ignore it silently.

        This client is bound to one workspace and has no project registry, so
        honouring the argument is impossible -- and dropping it would run the
        call (an ``edit``, a ``bash``) against the wrong tree without a word.
        """
        project = arguments.get("project_id")
        if isinstance(project, str) and project.strip():
            raise ClientError(
                ErrorCode.LOCAL_TOOL_UNAVAILABLE,
                "project_id is not supported by this client",
                details={
                    "detail": "project_id is resolved by the persistent MCP gateway; this client serves one "
                    f"workspace ({self._config.repo_root}). Omit project_id to use that workspace."
                },
                action=AgentAction.FIX_REQUEST,
            )

    def _trace(
        self,
        trace_id: str,
        tool: str,
        arguments: Mapping[str, Any],
        started_at: datetime,
        started: float,
        outcome: ToolOutcome,
        recoveries_before: int,
    ) -> None:
        if not self._traces.enabled:
            return
        try:
            state = self._session.state
            # The event list is bounded; if it rotated mid-call, keep the tail.
            recent = state.recovery_events[min(recoveries_before, len(state.recovery_events)) :]
            self._traces.record(
                trace_id=trace_id,
                tool=tool,
                arguments=arguments,
                started_at=started_at,
                duration_ms=round((time.perf_counter() - started) * 1000),
                site=outcome.site,
                is_error=outcome.is_error,
                degraded=outcome.degraded,
                degraded_reason=outcome.degraded_reason,
                output_chars=sum(len(str(block.get("text") or "")) for block in outcome.content),
                session_id=getattr(state, "session_id", "") or None,
                view_revision=outcome.view_revision,
                state_token=state.state_token(),
                recoveries=[f"{event.kind}:{event.outcome}" for event in recent],
            )
        except Exception:  # a trace must never fail a tool call
            return

    def _route(self, route: Route, arguments: Mapping[str, Any]) -> ToolOutcome:
        if route.tool == "read":
            split = _split_absolute_read_files(arguments)
            if split is not None:
                return self._read_with_absolute_files(route, *split)
        if route.site is Site.CLIENT or route.site is Site.CLIENT_UPLOAD_SERVER_BUILD:
            return self._local(route, arguments)
        return self._remote(route, arguments)

    def _read_with_absolute_files(
        self,
        route: Route,
        local_arguments: Mapping[str, Any],
        remote_arguments: Mapping[str, Any] | None,
    ) -> ToolOutcome:
        """Read explicit absolute files locally; never put them in the View."""
        local = executor_for("read_from_disk")(self._context, local_arguments)
        if remote_arguments is None:
            return ToolOutcome(
                content=local.content,
                is_error=local.is_error,
                site=Site.CLIENT.value,
                structured=local.structured,
            )
        remote = self._route(route, remote_arguments)
        return ToolOutcome(
            content=remote.content + local.content,
            is_error=remote.is_error or local.is_error,
            degraded=remote.degraded,
            degraded_reason=remote.degraded_reason,
            site="hybrid",
            view_revision=remote.view_revision,
            structured=remote.structured,
        )

    # -- client side ---------------------------------------------------- #

    def _local(self, route: Route, arguments: Mapping[str, Any]) -> ToolOutcome:
        result = executor_for(route.executor)(self._context, arguments)
        revision: int | None = None
        if route.pushes_blobs and result.changed_paths:
            revision = self._push(result.changed_paths)
        return ToolOutcome(
            content=result.content,
            is_error=result.is_error,
            degraded=result.degraded,
            degraded_reason=result.degraded_reason,
            site=route.site.value,
            view_revision=revision,
            structured=result.structured,
        )

    def _push(self, paths: tuple[str, ...]) -> int | None:
        """Bring the server's view forward, or say why it could not.

        A push failure is not allowed to lose the edit -- the file is already
        written -- so it degrades the *result* rather than raising. The agent
        is told the server's copy is behind, which is exactly the fact a
        subsequent server-side read would otherwise get wrong.
        """
        if not self._session.online or not self._session.state.view_bound:
            return None
        try:
            return self._session.push_paths(paths)
        except ClientError as exc:
            if exc.code is ErrorCode.SERVER_UNREACHABLE:
                self._session.state.go_offline(exc.message)
            raise ClientError(
                exc.code,
                f"the edit was written locally but the server's view is behind: {exc.message}",
                details=exc.details,
                retryable=exc.retryable,
                action=exc.action or AgentAction.RETRY_LATER,
                server_code=exc.server_code,
            ) from exc

    # -- server side ---------------------------------------------------- #

    def _remote(self, route: Route, arguments: Mapping[str, Any]) -> ToolOutcome:
        if not self._session.bootstrapped:
            return self._offline(route, arguments)
        if route.offline and not self._session.indexed:
            # No view outside a git repository: read the file from disk.
            local = executor_for(route.offline)(self._context, arguments)
            return ToolOutcome(
                content=local.content,
                is_error=local.is_error,
                site=Site.CLIENT.value,
                structured=local.structured,
            )
        answer = self._session.call_tool(route.tool, arguments)
        return ToolOutcome(
            content=answer.content,
            is_error=answer.is_error,
            degraded=answer.degraded,
            degraded_reason=answer.degraded_reason,
            site=route.site.value,
            view_revision=answer.view_revision,
            structured=answer.structured,
        )

    def _offline(self, route: Route, arguments: Mapping[str, Any]) -> ToolOutcome:
        """The session never opened. One bounded local answer, or a refusal."""
        reason = self._session.state.reason or "no LemonCrow session"
        if not route.offline:
            raise ClientError(
                ErrorCode.SERVER_SESSION_UNAVAILABLE,
                f"{route.tool} runs on the LemonCrow server, which is unavailable: {reason}",
                details={"tool": route.tool, "site": route.site.value},
                retryable=True,
                action=AgentAction.RETRY_LATER,
            )
        result: LocalResult = executor_for(route.offline)(self._context, arguments)
        return ToolOutcome(
            content=result.content,
            is_error=result.is_error,
            degraded=True,
            degraded_reason=result.degraded_reason or f"server_unreachable:{route.offline}",
            site="client_offline_fallback",
            structured=result.structured,
        )


def _with_worktree_header(outcome: ToolOutcome, worktree: LinkedWorktree) -> ToolOutcome:
    """Mark a result whose checkout came only from sticky session context."""
    prefix = f"[worktree {worktree.name}]"
    blocks = [dict(block) for block in outcome.content]
    for index, block in enumerate(blocks):
        value = block.get("text")
        if block.get("type") == "text" and isinstance(value, str):
            block["text"] = f"{prefix}\n{value}" if value else prefix
            blocks[index] = block
            return replace(outcome, content=tuple(blocks))
    return replace(outcome, content=({"type": "text", "text": prefix}, *outcome.content))


def _read_entry_path(entry: object) -> str:
    """The path one ``read`` entry names, in the read tool's own grammar."""
    if isinstance(entry, Mapping):
        return str(entry.get("path") or entry.get("file_path") or entry.get("filePath") or "")
    from .kit.read_path import split_file_opts

    return split_file_opts(str(entry))[0]


def _split_absolute_read_files(
    arguments: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Mapping[str, Any] | None] | None:
    raw_files = arguments.get("files")
    if isinstance(raw_files, (str, Mapping)):
        entries: list[object] = [raw_files]
    elif isinstance(raw_files, Sequence) and not isinstance(raw_files, (bytes, bytearray)):
        entries = list(raw_files)
    else:
        return None
    absolute: list[object] = []
    relative: list[object] = []
    for entry in entries:
        target = _read_entry_path(entry)
        (absolute if target and Path(target).is_absolute() else relative).append(entry)
    if not absolute:
        return None
    local_arguments: Mapping[str, Any] = {"files": absolute}
    if not relative and not arguments.get("symbol"):
        return local_arguments, None
    remote_arguments = dict(arguments)
    if relative:
        remote_arguments["files"] = relative
    else:
        remote_arguments.pop("files", None)
    return local_arguments, remote_arguments


class _SyncBridge:
    """The narrow view of the session that local tools are allowed to hold."""

    __slots__ = ("_session",)

    def __init__(self, session: RemoteSession) -> None:
        self._session = session

    @property
    def online(self) -> bool:
        return self._session.online

    @property
    def view_revision(self) -> int:
        return self._session.view_revision

    def push_paths(self, paths: tuple[str, ...]) -> int:
        return self._session.push_paths(paths)

    def resync(self) -> Mapping[str, Any]:
        return self._session.resync()
