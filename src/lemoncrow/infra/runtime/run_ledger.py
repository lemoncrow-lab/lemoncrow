"""Append-only ledger of observable events during an agent run."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.models import (
    LedgerEvent,
    to_jsonable,
)
from lemoncrow.core.foundation.runtime_decisions import RuntimeDecisionEvent
from lemoncrow.infra.runtime.cost_tracker import CostTracker


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _parse_dt(value: Any) -> datetime | None:
    """Parse a recorded ISO timestamp, or ``None`` when it carries nothing.

    Naive stamps are read as UTC: every writer in-tree emits UTC, and mixing an
    aware default with a naive restore leaves the two uncomparable.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# Hard cap on the bytes of any single string value inside an event payload.
# Per-event payloads carry arbitrary tool/command output, and ``snapshot``
# re-serializes the entire events list on every ``persist`` -- so one oversized
# payload string can bloat run.json without bound. We truncate individual
# payload string fields at record time (with a clear marker) rather than
# dropping events or changing the events-list contract. Generous, env-overridable.
try:
    _MAX_PAYLOAD_STR_BYTES = max(0, int(os.environ.get("LEMONCROW_MAX_PAYLOAD_STR_BYTES", "65536")))
except ValueError:
    _MAX_PAYLOAD_STR_BYTES = 65536


# Depth cap on recursive payload bounding. Real payloads nest a few levels
# (args -> content, lessons_used -> entries); this guards against pathological
# or cyclic-looking structures without rejecting legitimate ones.
_MAX_PAYLOAD_DEPTH = 8


# Hard cap on the NUMBER of raw events retained in memory. ``_handle`` appends
# ~2 events per tool call to one module-global ledger that lives for the whole
# long-lived stdio session, so an unbounded ``self.events`` list grows linearly
# for the life of the process (a confirmed RSS leak on the MCP hot path).
# We retain only the most recent events; older raw events are evicted. This is
# safe because the durable cumulative truth (routing/compaction savings, per-
# call cost) is flushed independently to ``live_savings_events.jsonl`` and the
# per-session savings sidecars on every call, and the live consumers of
# ``self.events`` (compaction, loop detection, routing recommendation) only read
# the recent tail. The cap is generous and env-overridable.
try:
    _MAX_RETAINED_EVENTS = max(0, int(os.environ.get("LEMONCROW_MAX_LEDGER_EVENTS", "8000")))
except ValueError:
    _MAX_RETAINED_EVENTS = 8000

# Evict in chunks so trimming is amortized O(1) on the hot path rather than an
# O(n) shift on every append once the cap is reached.
_EVENT_EVICTION_CHUNK = 512


def _bound_value(value: Any, depth: int) -> Any:
    """Return a bounded copy of ``value``, truncating oversized string leaves.

    Recurses into nested dicts and lists (up to ``_MAX_PAYLOAD_DEPTH``) so the
    big bloat carriers -- a tool's ``args`` dict or a ``lessons_used`` list with
    giant strings nested inside -- are bounded too. Never mutates the input: a
    new structure is built and returned.
    """
    if isinstance(value, str):
        if len(value) > _MAX_PAYLOAD_STR_BYTES:
            dropped = len(value) - _MAX_PAYLOAD_STR_BYTES
            return value[:_MAX_PAYLOAD_STR_BYTES] + f"\n...[truncated {dropped} chars]"
        return value
    if depth >= _MAX_PAYLOAD_DEPTH:
        return value
    if isinstance(value, dict):
        return {k: _bound_value(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_bound_value(v, depth + 1) for v in value]
    return value


def _bound_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded copy of an event payload, truncating oversized strings.

    Bounds per-event payload bytes so a single event's arbitrary output can't
    bloat run.json. String leaves at any depth (inside nested dicts and lists)
    are truncated with a clear marker; other structure is preserved. The input
    is never mutated -- a fresh structure is returned -- so a public ``record``
    caller that retains its dict won't see its strings silently truncated.
    """
    if _MAX_PAYLOAD_STR_BYTES <= 0:
        return payload
    return {key: _bound_value(value, 1) for key, value in payload.items()}


# --------------------------------------------------------------------------- #
# Per-session paths — the run ledger and its sidecars live in sessions/<id>/  #
# alongside the trace files, so a session is one self-contained folder.       #
# --------------------------------------------------------------------------- #


# session_run_dir/run_file_path were removed: every per-session path (this
# ledger's own run.json included) now goes through the single canonical
# lemoncrow.core.foundation.paths.session_dir(root, host, session_id), which is
# host-segregated (see paths.detect_host) and resolves a session's date
# instead of recomputing "today" on every call.


def outcomes_path(root: str | Path, host: str, session_id: str) -> Path:
    """Captured route/compact outcomes: ``session_dir(...)/outcomes.json``."""
    from lemoncrow.core.foundation.paths import session_dir

    return session_dir(root, host, session_id) / "outcomes.json"


def iter_run_files(root: str | Path) -> list[Path]:
    """All run-ledger snapshots under the canonical sessions/YYYY/MM/DD/<host>/<id>/ tree."""
    base = Path(root)
    sessions_root = base / "sessions"
    if sessions_root.is_dir():
        return sorted(sessions_root.glob("*/*/*/*/*/run.json"))
    return []


_EMPTY_GIT_ANCHOR: dict[str, str] = {"head": "", "branch": "", "repo_root": ""}


def _read_ref(git_dir: Path, ref: str) -> str:
    """Resolve a symbolic ref to a sha by reading files -- loose, then packed.

    Also follows ``commondir`` so a linked worktree (whose refs live in the main
    checkout's .git) resolves instead of silently reporting nothing.
    """
    roots = [git_dir]
    common = git_dir / "commondir"
    if common.is_file():
        pointer = Path(common.read_text(encoding="utf-8").strip())
        roots.append(pointer if pointer.is_absolute() else (git_dir / pointer))
    for base in roots:
        loose = base / ref
        if loose.is_file():
            return loose.read_text(encoding="utf-8").strip()
    for base in roots:
        packed = base / "packed-refs"
        if not packed.is_file():
            continue
        for line in packed.read_text(encoding="utf-8").splitlines():
            if line.startswith(("#", "^")):
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[1].strip() == ref:
                return parts[0].strip()
    return ""


def _resolve_git_anchor(workspace_path: str) -> dict[str, str]:
    """Return ``{"head", "branch", "repo_root"}`` for *workspace_path*.

    Why this exists: nothing else in the session substrate records which commit
    a run was working against, which makes after-the-fact "who wrote this diff"
    correlation a heuristic. Stamping HEAD into the ledger turns that into an
    exact join for every future run.

    Read straight off ``.git`` -- no subprocess. Forking ``git`` from the
    multi-threaded MCP process costs seconds per call, and this runs on every
    snapshot. Every failure degrades to empty strings: a run ledger must never
    fail to persist because a workspace is not a checkout.
    """
    if not workspace_path:
        return dict(_EMPTY_GIT_ANCHOR)
    try:
        start = Path(workspace_path)
        git_dir: Path | None = None
        repo_root = ""
        for parent in [start, *start.parents]:
            marker = parent / ".git"
            if marker.is_dir():
                git_dir, repo_root = marker, str(parent)
                break
            if marker.is_file():
                # Linked worktree or submodule: ".git" holds "gitdir: <path>".
                pointer = marker.read_text(encoding="utf-8").strip()
                if pointer.startswith("gitdir:"):
                    target = Path(pointer.split(":", 1)[1].strip())
                    git_dir = target if target.is_absolute() else (parent / target).resolve()
                    repo_root = str(parent)
                break
        if git_dir is None or not git_dir.is_dir():
            return dict(_EMPTY_GIT_ANCHOR)
        head_text = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head_text.startswith("ref:"):
            ref = head_text.split(":", 1)[1].strip()
            return {"head": _read_ref(git_dir, ref), "branch": ref.rsplit("/", 1)[-1], "repo_root": repo_root}
        return {"head": head_text, "branch": "", "repo_root": repo_root}
    except (OSError, ValueError, UnicodeDecodeError):
        return dict(_EMPTY_GIT_ANCHOR)


# --------------------------------------------------------------------------- #
# Merge-on-persist                                                             #
# --------------------------------------------------------------------------- #
#
# ``persist`` is not the only writer of a session's run.json. The Claude
# PostToolUse hooks append ``file_edit`` and ``command_result`` events to the
# very same file from a separate process, and those carry the at_head/model/
# host provenance ``lc review`` joins a diff to its author on. Writing a
# snapshot of in-memory state straight over the file deleted every one of them
# -- an unloaded process-global ledger persisting once wiped the whole run --
# so the snapshot is folded into what is already on disk instead of replacing
# it. Every writer also holds the shared per-run RunFileLock across its whole
# read/merge/replace transaction; atomic rename alone prevents torn JSON but
# cannot prevent a concurrent writer from being overwritten.


# Sort position for an event whose ``at`` is missing or unparseable, so the
# merged order is still total. Every writer in-tree stamps one.
_UNDATED_EVENT_AT = datetime.min.replace(tzinfo=UTC)


def _event_identity(event: dict[str, Any]) -> tuple[str, str, str, str]:
    """Identity of one serialized event, comparable across writers.

    The instant is normalized because the two writers spell the same moment
    differently -- pydantic emits ``...Z``, the hook emits ``...+00:00`` -- and
    comparing the raw strings would read one event as two and duplicate it on
    every persist.
    """
    at = _parse_dt(event.get("at"))
    stamp = at.astimezone(UTC).isoformat() if at is not None else str(event.get("at") or "")
    try:
        payload = json.dumps(event.get("payload") or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        payload = repr(event.get("payload"))
    return (str(event.get("kind") or ""), stamp, str(event.get("summary") or ""), payload)


# The host hook names an out-of-process writer stamps into ``payload.event``
# when it appends straight to run.json: the Claude plugin hooks under
# ``integrations/claude/plugin/hooks/`` and the codex/opencode writer in
# ``core.capabilities.plugin_runtime._codex_append_ledger_events``.
#
# Only these are folded back in on persist. Everything else in run.json came
# from some process's own ``record*`` calls, and those stamp ``payload.event``
# with their own vocabulary (``edit``, ``revert``, a workflow step name). A
# ledger rebuilt from scratch re-stamps ``at`` with the current instant, so its
# events never match the identity of the ones already on disk -- folding them
# in would union rather than replace and double the file on every rebuild.
# Three real callers rebuild: LedgerReconstructor.reconstruct, the service
# worker's session re-ingest (core/service/worker.py) and ``lc swarm
# run-child``, which reuses one ledger id across attempts.
_OUT_OF_PROCESS_EVENTS = frozenset(
    {
        "PostToolUse",
        "PostToolUseFailure",
        "SessionStart",
        "SessionEnrichment",
        "Stop",
        "UserPromptSubmit",
        "PreCompact",
        "PostCompact",
        "PostToolUseBash",
        "codex_exec",
    }
)


def _appended_out_of_process(event: dict[str, Any]) -> bool:
    """True when *event* was written straight to run.json by a host hook."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return False
    return str(payload.get("event") or "") in _OUT_OF_PROCESS_EVENTS


def _output_chars(event: dict[str, Any]) -> int:
    """``payload.output_chars`` as an int, or 0 for anything else."""
    payload = event.get("payload")
    if not isinstance(payload, dict):
        return 0
    try:
        return int(payload.get("output_chars", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _merge_persisted_events(memory_events: list[dict[str, Any]], disk_events: Any) -> list[dict[str, Any]]:
    """The in-memory events plus every hook-appended on-disk event they lack.

    Only events a host hook appended out of process are recovered (see
    :func:`_appended_out_of_process`); an in-process event on disk that this
    ledger does not hold belongs to a run this object is replacing, not
    extending, and folding it in would double a rebuilt ledger on every persist.

    Identity is counted as a *multiset*, so the same event shape appended twice
    out of process survives as two events while one that merely round-tripped
    through ``load``/``persist`` is not duplicated. The union is ordered by
    instant -- the order both writers appended in -- and trimmed to the same
    ceiling ``record`` enforces on ``self.events``, so merging can never make
    run.json grow past the bound the eviction cap already set.
    """
    if not isinstance(disk_events, list) or not disk_events:
        return memory_events
    credits: dict[tuple[str, str, str, str], int] = {}
    for event in memory_events:
        key = _event_identity(event)
        credits[key] = credits.get(key, 0) + 1
    recovered: list[dict[str, Any]] = []
    for candidate in disk_events:
        if not isinstance(candidate, dict) or not _appended_out_of_process(candidate):
            continue
        key = _event_identity(candidate)
        held = credits.get(key, 0)
        if held:
            credits[key] = held - 1  # already represented in memory
            continue
        recovered.append(candidate)
    if not recovered:
        return memory_events
    merged = [*memory_events, *recovered]
    merged.sort(key=lambda event: _parse_dt(event.get("at")) or _UNDATED_EVENT_AT)
    ceiling = _MAX_RETAINED_EVENTS + _EVENT_EVICTION_CHUNK
    if _MAX_RETAINED_EVENTS and len(merged) > ceiling:
        del merged[:-ceiling]
    return merged


def _merge_persisted_paths(memory_paths: list[str], disk_paths: Any) -> list[str]:
    """Union of the touched paths held in memory and those a hook appended."""
    if not isinstance(disk_paths, list) or not disk_paths:
        return memory_paths
    merged = list(memory_paths)
    known = set(merged)
    for candidate in disk_paths:
        text = str(candidate)
        if text and text not in known:
            known.add(text)
            merged.append(text)
    return merged


def _merge_with_persisted(snapshot: dict[str, Any], path: Path) -> dict[str, Any]:
    """Fold whatever another process appended to *path* back into *snapshot*.

    Degrades to the snapshot unchanged on anything unreadable: persisting a run
    must never fail because the file already on disk is missing or damaged.
    """
    try:
        recorded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return snapshot
    if not isinstance(recorded, dict):
        return snapshot
    snapshot["files_touched"] = _merge_persisted_paths(
        list(snapshot.get("files_touched") or []), recorded.get("files_touched")
    )
    # A loaded ledger restores aggregate counters but intentionally does not
    # replay CostTracker history, because replaying would append duplicate calls
    # to cost_history.json. Preserve the already-recorded per-run cost snapshot
    # until this process has fresh tracker state of its own.
    if not snapshot.get("cost") and isinstance(recorded.get("cost"), dict) and recorded["cost"]:
        snapshot["cost"] = recorded["cost"]
    memory_events: list[dict[str, Any]] = list(snapshot.get("events") or [])
    merged = _merge_persisted_events(memory_events, recorded.get("events"))
    if merged is memory_events:
        return snapshot
    snapshot["events"] = merged
    # These three describe the events actually written, so they are recomputed
    # over the union rather than left describing this process's memory alone.
    tool_calls = [event for event in merged if event.get("kind") == "tool_call"]
    snapshot["tool_call_count"] = len(tool_calls)
    snapshot["total_tool_output_chars"] = sum(_output_chars(event) for event in tool_calls)
    snapshot["alert_count"] = sum(1 for event in merged if event.get("kind") == "watchdog_alert")
    return snapshot


class RunLedger:
    """Append-only ledger for a single agent run."""

    def __init__(
        self,
        session_id: str | None = None,
        agent: str | None = None,
        root: Path | None = None,
        task: str = "",
        domain: str | None = None,
        workspace_path: str | Path | None = None,
    ) -> None:
        self.session_id = session_id or uuid.uuid4().hex
        self.agent = agent
        self.task = task
        self.domain = domain
        configured_workspace = (
            workspace_path or os.environ.get("LEMONCROW_WORKSPACE_ROOT") or os.environ.get("CLAUDE_WORKSPACE_ROOT")
        )
        self.workspace_path = str(Path(configured_workspace).expanduser().resolve()) if configured_workspace else ""
        self.events: list[LedgerEvent] = []
        # Monotonic checkpoint step counter. Authoritative even after old
        # checkpoint events are evicted from the bounded ``self.events`` list.
        self._checkpoint_seq = 0
        # Guards mutation of events/counters and snapshot reads: the MCP
        # dispatcher shares one ledger across a thread pool, so concurrent
        # record()/record_call() and snapshot() must not race (torn snapshot,
        # lost token_count increment). RLock: record_call() re-enters via record().
        self._lock = threading.RLock()
        self.created_at = _utcnow()
        self.updated_at = self.created_at
        self.status: str = "running"
        self._root = root

        # V2 reasoning/procedural state
        self.current_plan: list[str] = []
        self.files_touched: list[str] = []
        self.tools_called: list[str] = []
        self.commands_run: list[str] = []
        self.tests_run: list[str] = []
        self.errors_seen: list[str] = []
        self.repeated_failures: list[str] = []
        self.hypotheses_tried: list[str] = []
        self.hypotheses_rejected: list[str] = []
        self.verified_facts: list[str] = []
        self.open_questions: list[str] = []
        self.active_playbooks: list[str] = []
        self.active_rubrics: list[str] = []
        self.current_blockers: list[str] = []
        self.next_required_validation: str | None = None
        self.workflow_state: dict[str, Any] = {}
        self.plan_review: dict[str, Any] = {}
        self.task_progress: dict[str, Any] = {}
        self.workflow_step_events: list[dict[str, Any]] = []
        self.agent_settings: dict[str, Any] = {}
        self.skills: list[str] = []
        self.token_count: int = 0
        self.tool_count: int = 0
        self.budget: dict[str, int] = {}
        # Per-call cost tracking (lazy: tracker only persists if a root is set).
        self._cost_root: Path | None = root
        self.cost_tracker: CostTracker | None = CostTracker(root) if root is not None else None
        # Git HEAD/branch/root of the workspace, resolved once on first
        # snapshot rather than in __init__: a ledger is constructed on paths
        # that never persist (and by load(), which restores the recorded value
        # instead), so the filesystem walk should not be paid up front.
        self._git: dict[str, str] | None = None

    # ----- setters -------------------------------------------------------- #

    def set_plan(self, plan: list[str]) -> None:
        with self._lock:
            self.current_plan = list(plan)
            self.updated_at = _utcnow()

    def add_hypothesis(self, hypothesis: str, *, rejected: bool = False) -> None:
        with self._lock:
            if rejected:
                if hypothesis not in self.hypotheses_rejected:
                    self.hypotheses_rejected.append(hypothesis)
            else:
                if hypothesis not in self.hypotheses_tried:
                    self.hypotheses_tried.append(hypothesis)
            self.updated_at = _utcnow()

    def add_verified_fact(self, fact: str) -> None:
        with self._lock:
            if fact not in self.verified_facts:
                self.verified_facts.append(fact)
            self.updated_at = _utcnow()

    def add_open_question(self, question: str) -> None:
        with self._lock:
            if question not in self.open_questions:
                self.open_questions.append(question)
            self.updated_at = _utcnow()

    def set_blocker(self, blocker: str) -> None:
        with self._lock:
            self.current_blockers = [blocker]
            self.updated_at = _utcnow()

    def set_next_validation(self, validation: str | None) -> None:
        with self._lock:
            self.next_required_validation = validation
            self.updated_at = _utcnow()

    def _normalize_workflow_event(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if event_type == "workflow_state":
            normalized: dict[str, Any] = {}
            workflow_step = str(payload.get("workflow_step") or payload.get("current_step") or "").strip()
            session_phase = str(payload.get("session_phase") or "").strip()
            if workflow_step:
                normalized["workflow_step"] = workflow_step
            if session_phase:
                normalized["session_phase"] = session_phase
            return normalized
        if event_type == "plan_review":
            normalized = {}
            review_decision = str(payload.get("review_decision") or payload.get("decision") or "").strip()
            plan_id = str(payload.get("plan_id") or "").strip()
            workflow_step = str(payload.get("workflow_step") or "").strip()
            if review_decision:
                normalized["review_decision"] = review_decision
            if plan_id:
                normalized["plan_id"] = plan_id
            if workflow_step:
                normalized["workflow_step"] = workflow_step
            return normalized
        if event_type == "task_progress":
            normalized = {}
            task_id = str(payload.get("task_id") or "").strip()
            workflow_step = str(payload.get("workflow_step") or "").strip()
            if task_id:
                normalized["task_id"] = task_id
            if workflow_step:
                normalized["workflow_step"] = workflow_step
            for key in ("completed_tasks", "remaining_tasks"):
                value = payload.get(key)
                if isinstance(value, bool):
                    continue
                try:
                    normalized[key] = max(0, int(value or 0))
                except (TypeError, ValueError):
                    continue
            return normalized
        return {}

    def record_workflow_event(self, event_type: str, payload: dict[str, Any]) -> LedgerEvent:
        normalized = self._normalize_workflow_event(event_type, payload)
        with self._lock:
            if event_type == "workflow_state":
                self.workflow_state = normalized
                summary = f"workflow:{normalized.get('workflow_step') or 'recorded'}"
            elif event_type == "plan_review":
                self.plan_review = normalized
                summary = f"plan_review:{normalized.get('review_decision') or 'recorded'}"
            elif event_type == "task_progress":
                self.task_progress = normalized
                summary = f"task_progress:{normalized.get('task_id') or 'recorded'}"
            else:
                summary = f"event:{event_type}"
        return self.record("note", summary, normalized)

    def record_workflow_step_event(
        self,
        *,
        step_id: str,
        event: str,
        kind: str,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> LedgerEvent:
        normalized = {
            "step_id": step_id,
            "event": event,
            "kind": kind,
            "status": status,
        }
        if payload:
            normalized.update(payload)
        with self._lock:
            self.workflow_step_events.append(dict(normalized))
        return self.record("note", f"workflow_step:{step_id}:{event}", normalized)

    # ----- recording ------------------------------------------------------ #

    def record_runtime_decision(self, event: RuntimeDecisionEvent) -> LedgerEvent:
        """Persist a shared runtime decision inside the existing run ledger.

        The ledger already carries the raw session identifier at the run level,
        so omit the nested copy. All mapping fields were bounded/redacted by
        RuntimeDecisionEvent before they reach this sink.
        """

        payload = event.to_payload()
        payload.pop("session_id", None)
        return self.record(
            "note",
            f"runtime_decision:{event.kind}",
            {"runtime_decision": payload},
        )

    def record(
        self,
        kind: str,
        summary: str,
        payload: dict[str, Any] | None = None,
    ) -> LedgerEvent:
        event = LedgerEvent(
            kind=kind,  # type: ignore[arg-type]
            summary=summary,
            payload=_bound_payload(payload or {}),
        )
        with self._lock:
            self.events.append(event)
            # Bound the retained raw events so a long-lived session can't grow
            # ``self.events`` without limit. Evict the oldest chunk in one slice
            # assignment (kept O(1) amortized; ``self.events`` stays a list so
            # external slicing/indexing callers are unaffected).
            if _MAX_RETAINED_EVENTS and len(self.events) > _MAX_RETAINED_EVENTS + _EVENT_EVICTION_CHUNK:
                del self.events[:_EVENT_EVICTION_CHUNK]
            self.updated_at = _utcnow()
        return event

    def record_tool_call(
        self,
        tool: str,
        args: dict[str, Any] | None = None,
        output: str | None = None,
        args_signature: str | None = None,
    ) -> LedgerEvent:
        from lemoncrow.pro.foundation.watchdogs import args_signature as _sig

        with self._lock:
            self.tool_count += 1
            if tool not in self.tools_called:
                self.tools_called.append(tool)

        signature = args_signature or _sig(args)

        return self.record(
            "tool_call",
            f"{tool}({signature})",
            {
                "tool": tool,
                "args": args or {},
                "output": output,
                "args_signature": signature,
                "output_chars": len(output) if output else 0,
            },
        )

    def record_command(
        self,
        command: str,
        ok: bool,
        error_signature: str = "",
        stdout: str | None = None,
        stderr: str | None = None,
    ) -> LedgerEvent:
        with self._lock:
            self.commands_run.append(command)
            if not ok:
                sig = error_signature.strip()
                if sig and sig not in self.errors_seen:
                    self.errors_seen.append(sig)
        return self.record(
            "command_result",
            command,
            {"ok": ok, "error_signature": error_signature, "stdout": stdout, "stderr": stderr},
        )

    def record_file_event(self, path: str, event: str, diff: str | None = None, *, model: str = "") -> LedgerEvent:
        """Record an edit together with who made it, against which HEAD.

        ``session_id`` / ``agent`` / ``model`` / ``at_head`` are the same four
        keys the claude and codex PostToolUse hooks write, so the MCP ``lc edit``
        path is a peer authoring surface rather than a second-class one. They go
        in ``payload`` because ``LedgerEvent`` is ``extra="forbid"`` with a fixed
        field set -- the payload dict is the extension point. ``model`` is
        supplied by the caller (only the MCP dispatcher knows it) and stays
        ``""`` when unknown: an empty string is a fact, a guess is not.
        """
        with self._lock:
            if path and path not in self.files_touched:
                self.files_touched.append(path)
        kind = "file_revert" if event == "revert" else "file_edit"
        payload = {
            "path": path,
            "event": event,
            "session_id": self.session_id,
            "host": self.agent or "",
            "model": model,
            "at_head": self.git_anchor().get("head", ""),
        }
        if diff:
            payload["diff"] = diff
        return self.record(kind, f"{event}:{path}", payload)

    def record_alert(self, monitor: str, severity: str, message: str) -> LedgerEvent:
        if severity == "high":
            with self._lock:
                self.current_blockers = [f"[{monitor}] {message}"]
        return self.record(
            "watchdog_alert",
            f"[{severity}] {monitor}: {message}",
            {"monitor": monitor, "severity": severity, "message": message},
        )

    def record_test(self, test_id: str, passed: bool, detail: str = "") -> LedgerEvent:
        with self._lock:
            if test_id not in self.tests_run:
                self.tests_run.append(test_id)
        return self.record(
            "test_result",
            f"{test_id}={'pass' if passed else 'fail'}",
            {"test_id": test_id, "passed": passed, "detail": detail},
        )

    def record_call(
        self,
        *,
        operation: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cost_usd: float | None = None,
        lessons_used: list[str] | None = None,
        prompt: str | None = None,
        response: str | None = None,
        stable_prefix_hash: str | None = None,
        prefix_invalidated_reason: str = "",
        cache_write_tokens: int = 0,
        modeled_cache_read_tokens: int = 0,
        cache_evidence: str = "",
        phase: str | None = None,
    ) -> LedgerEvent:
        """Record a single LLM call with cost + lessons attribution.

        ``cache_write_tokens`` and ``phase`` are additive Phase 13 fields
        (LINEAR-02 / T-13-04): keyword-only with defaults so existing
        callers and on-disk JSONL records remain compatible. Loaders that
        read these keys must use ``.get(..., default)``.
        """
        if self.cost_tracker is None:
            # No explicit root: persist cost history under the LemonCrow store
            # root rather than polluting the process CWD (SDK middleware users
            # construct RunLedger without a root).
            from lemoncrow.core.foundation.paths import default_store_root

            self.cost_tracker = CostTracker(default_store_root())
        rec = self.cost_tracker.record_call(
            operation=operation,
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            domain=self.domain,
            task=self.task,
            cost_usd=cost_usd,
            lessons_used=lessons_used,
        )
        with self._lock:
            self.token_count += rec.input_tokens + rec.output_tokens
        return self.record(
            "tool_call",
            f"llm:{operation}({model})",
            {
                "kind": "llm_call",
                "operation": rec.operation,
                "model": rec.model,
                "input_tokens": rec.input_tokens,
                "output_tokens": rec.output_tokens,
                "cache_read_tokens": rec.cache_read_tokens,
                "cache_write_tokens": cache_write_tokens,
                "modeled_cache_read_tokens": modeled_cache_read_tokens,
                "cache_evidence": cache_evidence,
                "cost_usd": rec.cost_usd,
                "lessons_used": list(rec.lessons_used),
                "op_key": rec.op_key,
                "prompt": prompt,
                "response": response,
                "stable_prefix_hash": stable_prefix_hash or "",
                "prefix_invalidated_reason": prefix_invalidated_reason,
                "phase": phase,
            },
        )

    def close(self, status: str = "complete") -> None:
        with self._lock:
            self.status = status
            self.updated_at = _utcnow()

    def record_checkpoint(
        self,
        tool_name: str,
        model_route: str,
        input_data: str,
        output_data: str,
        compact_state: str = "",
        cost_so_far_usd: float = 0.0,
        root: Path | None = None,
    ) -> Any:
        """Create and persist an idempotent Checkpoint at the current step.

        Returns the Checkpoint object. Uses run_ledger root if root is None.
        """
        from lemoncrow.infra.runtime.checkpoint import Checkpoint, CheckpointStore

        # Monotonic per-ledger step id: derived from a counter rather than the
        # (now bounded) raw events list, so eviction of old checkpoint events
        # can never make step ids repeat and collide in the CheckpointStore.
        with self._lock:
            step_id = self._checkpoint_seq
            self._checkpoint_seq += 1
        ckpt = Checkpoint.create(
            session_id=self.session_id,
            step_id=step_id,
            tool_name=tool_name,
            model_route=model_route,
            input_data=input_data,
            output_data=output_data,
            compact_state=compact_state,
            cost_so_far_usd=cost_so_far_usd,
        )
        store = CheckpointStore(root or self._root)
        store.save(ckpt)
        self.record(
            "checkpoint",
            f"step={step_id} tool={tool_name} route={model_route}",
            ckpt.to_dict(),
        )
        return ckpt

    # ----- timing helpers ------------------------------------------------- #

    @property
    def first_event_at(self) -> datetime:
        """Timestamp of the first ledger event, falling back to ``created_at``."""
        return self.events[0].at if self.events else self.created_at

    @property
    def last_event_at(self) -> datetime:
        """Timestamp of the most recent ledger event, falling back to ``updated_at``."""
        return self.events[-1].at if self.events else self.updated_at

    @property
    def duration_seconds(self) -> float:
        """Wall-clock seconds between first and last event (0 for single-event runs)."""
        delta = self.last_event_at - self.first_event_at
        return max(0.0, delta.total_seconds())

    # ----- snapshot / persistence ----------------------------------------- #

    def git_anchor(self) -> dict[str, str]:
        """Commit this run is working against; empty strings outside a checkout.

        Resolved once and cached: HEAD can move mid-session, and the value that
        makes a run correlatable is the one it started from.
        """
        if self._git is None:
            self._git = _resolve_git_anchor(self.workspace_path)
        return dict(self._git)

    def snapshot(self) -> dict[str, Any]:
        # Hold the lock across the whole snapshot so a concurrent record_*/setter
        # cannot mutate any list/dict mid-read (torn snapshot) and every field is
        # captured at one consistent instant.
        with self._lock:
            events = list(self.events)
            workflow_step_events = list(self.workflow_step_events)
            # Single pass over the (locked-copy) events instead of three.
            tool_calls: list[LedgerEvent] = []
            alerts: list[LedgerEvent] = []
            total_output = 0
            for e in events:
                if e.kind == "tool_call":
                    tool_calls.append(e)
                    total_output += int(e.payload.get("output_chars", 0))
                elif e.kind == "watchdog_alert":
                    alerts.append(e)
            return {
                "session_id": self.session_id,
                "agent": self.agent,
                "task": self.task,
                "domain": self.domain,
                "workspace_path": self.workspace_path,
                "git": self.git_anchor(),
                "status": self.status,
                "tool_call_count": len(tool_calls),
                "total_tool_output_chars": total_output,
                "alert_count": len(alerts),
                "created_at": self.created_at.isoformat(),
                "updated_at": self.updated_at.isoformat(),
                "current_plan": list(self.current_plan),
                "files_touched": list(self.files_touched),
                "tools_called": list(self.tools_called),
                "commands_run": list(self.commands_run),
                "tests_run": list(self.tests_run),
                "errors_seen": list(self.errors_seen),
                "repeated_failures": list(self.repeated_failures),
                "hypotheses_tried": list(self.hypotheses_tried),
                "hypotheses_rejected": list(self.hypotheses_rejected),
                "verified_facts": list(self.verified_facts),
                "open_questions": list(self.open_questions),
                "active_playbooks": list(self.active_playbooks),
                "active_rubrics": list(self.active_rubrics),
                "current_blockers": list(self.current_blockers),
                "next_required_validation": self.next_required_validation,
                "workflow_state": dict(self.workflow_state),
                "plan_review": dict(self.plan_review),
                "task_progress": dict(self.task_progress),
                "workflow_step_events": [dict(event) for event in workflow_step_events],
                "agent_settings": dict(self.agent_settings),
                "skills": list(self.skills),
                "token_count": self.token_count,
                "tool_count": self.tool_count,
                "budget": dict(self.budget),
                "cost": (self.cost_tracker.snapshot() if self.cost_tracker else {}),
                "events": [to_jsonable(e) for e in events],
            }

    def persist(self, root: Path | None = None) -> Path:
        target_root = root or self._root
        if target_root is None:
            raise ValueError("RunLedger.persist requires a root directory.")
        from lemoncrow.core.foundation.paths import session_dir
        from lemoncrow.core.foundation.run_file_io import RunFileLock, atomic_write_json

        path = session_dir(target_root, self.agent or "claude", self.session_id) / "run.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic rename prevents torn JSON, but only the per-run lock prevents a
        # hook that appends between our read and rename from being overwritten.
        # Every in-tree run.json writer takes this same lock.
        with RunFileLock(path):
            payload = _merge_with_persisted(self.snapshot(), path)
            atomic_write_json(path, payload)
        return path
        return path

    @classmethod
    def load(cls, path: Path) -> RunLedger:
        path = Path(path)
        try:
            snap: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"corrupt run ledger at {path}: {exc}") from exc
        led = cls(
            session_id=snap.get("session_id"),
            agent=snap.get("agent"),
            task=snap.get("task", "") or "",
            domain=snap.get("domain"),
            workspace_path=snap.get("workspace_path"),
        )
        # Old ledgers intentionally remain unscoped instead of inheriting the
        # workspace environment of whichever process happens to load them.
        led.workspace_path = str(snap.get("workspace_path") or "")
        # Absent on every ledger written before the anchor existed. Left unset
        # (not {}) in that case so a session that resumes still resolves one;
        # re-resolving is only wrong when the file already recorded the answer.
        recorded_git = snap.get("git")
        if isinstance(recorded_git, dict) and recorded_git:
            led._git = {str(key): str(value) for key, value in recorded_git.items()}
        led.status = snap.get("status", "running")
        for ev in snap.get("events", []):
            # ``at`` is the whole point of an append-only ledger: dropping it
            # reset every event's timestamp to load time, which silently
            # rewrote the one field that says *when* an edit happened.
            # ``LedgerEvent.at`` defaults to now, so only pass it when the file
            # actually recorded a parseable stamp.
            fields: dict[str, Any] = {
                "kind": ev.get("kind"),
                "summary": ev.get("summary", ""),
                "payload": ev.get("payload", {}),
            }
            recorded_at = _parse_dt(ev.get("at"))
            if recorded_at is not None:
                fields["at"] = recorded_at
            led.events.append(LedgerEvent(**fields))
            if ev.get("kind") == "checkpoint":
                led._checkpoint_seq += 1
        # Wall-clock bounds of the run, restored for the same reason: a loaded
        # ledger that claims it was created just now is not the run on disk.
        created_at = _parse_dt(snap.get("created_at"))
        if created_at is not None:
            led.created_at = created_at
        updated_at = _parse_dt(snap.get("updated_at"))
        led.updated_at = updated_at if updated_at is not None else led.created_at
        led.current_plan = list(snap.get("current_plan") or [])
        led.files_touched = list(snap.get("files_touched") or [])
        led.tools_called = list(snap.get("tools_called") or [])
        led.commands_run = list(snap.get("commands_run") or [])
        led.tests_run = list(snap.get("tests_run") or [])
        led.errors_seen = list(snap.get("errors_seen") or [])
        led.repeated_failures = list(snap.get("repeated_failures") or [])
        led.hypotheses_tried = list(snap.get("hypotheses_tried") or [])
        led.hypotheses_rejected = list(snap.get("hypotheses_rejected") or [])
        led.verified_facts = list(snap.get("verified_facts") or [])
        led.open_questions = list(snap.get("open_questions") or [])
        led.active_playbooks = list(snap.get("active_playbooks") or [])
        led.active_rubrics = list(snap.get("active_rubrics") or [])
        led.current_blockers = list(snap.get("current_blockers") or [])
        led.next_required_validation = snap.get("next_required_validation")
        led.workflow_state = dict(snap.get("workflow_state") or {})
        led.plan_review = dict(snap.get("plan_review") or {})
        led.task_progress = dict(snap.get("task_progress") or {})
        led.workflow_step_events = [
            dict(event) for event in snap.get("workflow_step_events", []) if isinstance(event, dict)
        ]
        led.agent_settings = dict(snap.get("agent_settings") or {})
        led.skills = list(snap.get("skills") or [])
        led.token_count = int(snap.get("token_count") or 0)
        led.tool_count = int(snap.get("tool_count") or 0)
        led.budget = dict(snap.get("budget") or {})
        return led
