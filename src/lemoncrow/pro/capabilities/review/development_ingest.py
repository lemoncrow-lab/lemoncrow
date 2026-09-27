"""Incremental ingestion of agent run ledgers into a Change's Development stream.

The ledger (``sessions/YYYY/MM/DD/<host>/<id>/run.json``) stays the detailed
execution record.  This module is the ``DevelopmentEventSink`` adapter for it:

    run.json --(mtime/size + tail cursor)--> sanitized drafts --> DevelopmentStore

Cost model, because this runs from the Reader's live-state loop:

* A known session costs one ``stat`` per sync while its ledger is unchanged.
* A changed ledger is parsed once and only events after the stored tail
  cursor are mapped (identity-guarded; a rewritten/evicted ledger falls back
  to a full pass that the store's UNIQUE ids keep idempotent).
* Discovery of *new* sessions runs at most every ``discovery_interval_s`` over
  a bounded window of session date directories.  Files that fail the cheap
  repository-root needle are remembered by ``(mtime, size)`` and not re-read.

Authorship follows ``provenance.py``: workspace path and timing are presence,
not contribution.  A session enters Development only on exact evidence (an
in-scope edit), explicit binding, or a parent/child link to an author.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .development import (
    ADMITTED_KINDS,
    REVIEW_PURPOSE_KINDS,
    VISIBLE_ROLES,
    CursorUpdate,
    DevelopmentDraft,
    DevelopmentSession,
    DevelopmentStore,
    DevelopmentUnavailable,
    classify_check,
    normalize_at,
    parse_at,
    stable_id,
)

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.session_models import ReviewSession
    from lemoncrow.pro.capabilities.review.store import ReviewStore

_CURSOR_VERSION = 1
_IGNORED_ROOTS = frozenset({".git", ".lemoncrow", ".lc-worktrees", ".venv", "node_modules", "__pycache__"})
_SYSTEM_HOSTS = frozenset(
    {
        "lemoncrow",
        "lemoncrow-client",
        "lemoncrow-server",
        "lemoncrow-enterprise-server",
        "lemoncrow-enterprise-server-test",
    }
)
_PURPOSE_BY_PERSONA = {
    "review": "review",
    "reviewer": "review",
    "explore": "investigate",
    "research": "investigate",
    "plan": "investigate",
    "benchmark": "benchmark",
    "code": "implement",
    "execute": "implement",
    "solve": "implement",
    "auto": "implement",
    "general": "implement",
    "bare": "implement",
    "verify": "verify",
}
_DROPPED_KINDS = frozenset({"reasoning", "checkpoint", "rubric_run", "watchdog_alert", "tool_call", "tool_result"})
_LINE_SUFFIX = re.compile(r":L?\d+(?:-L?\d+)?$")
_WORKFLOW_STEP = re.compile(r"^workflow_step:([^:]+):(\w+)")
_MAX_STATE_ITEMS = 200
_MAX_SESSION_DAYS = 14
_IRRELEVANT_CACHE = 4096


@dataclass(frozen=True)
class LedgerRef:
    host: str
    session_id: str
    path: Path


@dataclass(frozen=True)
class _SessionPlan:
    """What one changed ledger contributes, computed before anything is written."""

    key: str
    drafts: tuple[DevelopmentDraft, ...]
    session: DevelopmentSession
    expected_cursor: str


# --------------------------------------------------------------------------- #
# Pure mapping helpers                                                        #
# --------------------------------------------------------------------------- #


def infer_purpose(host: str, run: Mapping[str, Any]) -> str:
    """Best-effort purpose from explicit metadata, host and persona.  Never authorship."""

    raw_settings = run.get("agent_settings")
    settings: Mapping[str, Any] = raw_settings if isinstance(raw_settings, Mapping) else {}
    for value in (run.get("purpose"), settings.get("purpose"), settings.get("development_purpose")):
        text = str(value or "").strip().lower()
        if text in {"implement", "verify", "investigate", "review", "ask", "benchmark", "system"}:
            return text
    if host in _SYSTEM_HOSTS:
        return "system"
    agent = str(run.get("agent") or "").strip().lower()
    domain = str(run.get("domain") or "").strip().lower()
    if domain in {"benchmark", "bench", "eval"} or "bench" in agent:
        return "benchmark"
    for candidate in (agent.rsplit(":", 1)[-1], host.rsplit(":", 1)[-1]):
        if candidate in _PURPOSE_BY_PERSONA:
            return _PURPOSE_BY_PERSONA[candidate]
    return ""


def scoped_path(raw: object, *, repo_root: str, workspace_is_repo: bool) -> str:
    """Repository-relative path of an edit that belongs to this Change, else ``""``.

    Absolute recordings name their tree and must sit under ``repo_root``;
    relative recordings count only when the session's workspace *is* the
    repository (``provenance._under_repo``).  Nested worktrees, the store and
    dependency trees are never Change source.
    """

    text = _LINE_SUFFIX.sub("", str(raw or "").strip().replace("\\", "/"))
    if not text:
        return ""
    if text.startswith("~"):
        text = os.path.expanduser(text)
    if text.startswith("/"):
        norm = os.path.normpath(text)
        if not norm.startswith(repo_root + "/"):
            with contextlib.suppress(OSError, ValueError):
                norm = str(Path(norm).resolve())
        if not norm.startswith(repo_root + "/"):
            return ""
        rel = norm[len(repo_root) + 1 :]
    else:
        if not workspace_is_repo:
            return ""
        rel = os.path.normpath(text)
        if rel == ".." or rel.startswith("../") or rel == ".":
            return ""
    while rel.startswith("./"):
        rel = rel[2:]
    if not rel or rel.split("/", 1)[0] in _IGNORED_ROOTS:
        return ""
    return rel


def _payload(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = raw.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _identity(raw: Mapping[str, Any]) -> str:
    try:
        body = json.dumps(raw.get("payload") or {}, sort_keys=True, default=str)
    except (TypeError, ValueError):
        body = repr(raw.get("payload"))
    return stable_id(raw.get("kind") or "", normalize_at(raw.get("at")), raw.get("summary") or "", body)


def _command_status(payload: Mapping[str, Any]) -> str:
    ok = payload.get("ok")
    if isinstance(ok, bool):
        return "pass" if ok else "fail"
    for field in ("exit_code", "return_code", "returncode"):
        code = payload.get(field)
        if isinstance(code, int) and not isinstance(code, bool):
            return "pass" if code == 0 else "fail"
    return ""


def _diff_stat(diff: object) -> str:
    text = str(diff or "")
    if not text:
        return ""
    added = removed = 0
    for line in text.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return f"+{added} -{removed}" if added or removed else ""


def map_ledger_event(
    raw: Mapping[str, Any],
    *,
    host: str,
    session_id: str,
    repo_root: str,
    workspace_is_repo: bool,
    order: int,
) -> DevelopmentDraft | None:
    """Map one raw RunLedger event to at most one sanitized draft.

    Reasoning, checkpoints, tool chatter (including LLM-call rows carrying raw
    prompts/responses), routing decisions and non-verification shell commands
    never become Development.  Diffs, stdout and stderr are never copied.
    """

    kind = str(raw.get("kind") or "").strip()
    if not kind or kind in _DROPPED_KINDS:
        return None
    payload = _payload(raw)
    at = normalize_at(raw.get("at"))
    summary = str(raw.get("summary") or "").strip()
    source_event_id = f"ledger:{host}:{session_id}:{_identity(raw)}"

    def make(
        draft_kind: str,
        text: str,
        *,
        detail: str = "",
        path: str = "",
        status: str = "",
        task_id: str = "",
    ) -> DevelopmentDraft:
        return DevelopmentDraft(
            kind=draft_kind,
            at=at,
            source_event_id=source_event_id,
            host=host,
            session_id=session_id,
            path=path,
            summary=text,
            detail=detail,
            status=status,
            task_id=task_id,
            source="run_ledger",
            order=order,
        )

    if kind in {"file_edit", "file_revert"}:
        path = scoped_path(payload.get("path"), repo_root=repo_root, workspace_is_repo=workspace_is_repo)
        if not path:
            return None
        edit = kind == "file_edit"
        return make(
            "edit" if edit else "revert",
            f"{'Edited' if edit else 'Reverted'} {path}",
            detail=_diff_stat(payload.get("diff")),
            path=path,
            status="changed" if edit else "reverted",
        )

    if kind in {"command", "command_result"}:
        command = str(payload.get("command") or summary).strip()
        check = classify_check(command)
        if not check:
            return None
        status = _command_status(payload)
        failure = str(payload.get("error_signature") or ("command failed" if status == "fail" else ""))
        return make(check, command, detail=failure, status=status or "pending")

    if kind in {"test_result", "validation"}:
        passed = payload.get("passed")
        status = "pass" if passed is True else "fail" if passed is False else _command_status(payload)
        name = str(payload.get("test_id") or "").strip()
        detail = str(payload.get("detail") or payload.get("error") or payload.get("message") or "")
        return make(
            "test" if kind == "test_result" else "validation",
            name or summary,
            detail=detail,
            status=status or "pending",
            task_id=name,
        )

    if kind == "agent_message":
        role = str(payload.get("role") or "").strip().lower()
        if role == "user":
            text = str(payload.get("prompt") or payload.get("message") or summary.removeprefix("user:").strip())
            return make("intent", text, detail=text) if text.strip() else None
        if role in {"assistant", "agent"}:
            text = str(
                payload.get("message")
                or payload.get("text")
                or payload.get("response")
                or summary.removeprefix("assistant:").strip()
            )
            return make("message", text, detail=text) if len(text.strip()) >= 12 else None
        return None

    if kind == "note":
        step = _WORKFLOW_STEP.match(summary)
        if step is not None and step.group(2) in {"started", "start", "completed", "complete", "done", "failed"}:
            started = step.group(2) in {"started", "start"}
            return make(
                "task_started" if started else "task_completed",
                f"{'Started' if started else 'Completed'} {step.group(1)}",
                status="" if started else ("fail" if step.group(2) == "failed" else "pass"),
                task_id=step.group(1),
            )
        if summary.startswith("plan_review:"):
            decision = summary.removeprefix("plan_review:").strip() or "recorded"
            return make("decision", f"Plan review: {decision}")
        return None
    return None


def _hash_items(values: object) -> list[str]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return []
    return [stable_id(str(item).strip())[:16] for item in list(values)[:_MAX_STATE_ITEMS] if str(item).strip()]


def state_drafts(
    run: Mapping[str, Any],
    previous: Mapping[str, Any],
    *,
    host: str,
    session_id: str,
    emitted: dict[str, int],
) -> tuple[list[DevelopmentDraft], dict[str, Any]]:
    """Delta drafts for the ledger's run-level state (plan, facts, questions…).

    The ledger stores these as current lists, not events.  Diffing them against
    the cursor's hashes turns additions into events and removals of open items
    into ``resolved`` events.  ``emitted`` counts prior identical transitions
    so re-adding a resolved question is a new event rather than a duplicate.
    """

    at = normalize_at(run.get("updated_at") or run.get("created_at"))
    drafts: list[DevelopmentDraft] = []
    state: dict[str, Any] = {}

    def emit(kind: str, summary: str, *, status: str = "", detail: str = "", field: str, value: str) -> None:
        op = f"{field}:{status or 'add'}:{stable_id(value)[:16]}"
        count = emitted.get(op, 0)
        emitted[op] = count + 1
        drafts.append(
            DevelopmentDraft(
                kind=kind,
                at=at,
                source_event_id=f"ledger-state:{host}:{session_id}:{op}:{count}",
                host=host,
                session_id=session_id,
                summary=summary,
                detail=detail,
                status=status,
                source="run_ledger",
                order=100_000 + len(drafts),
            )
        )

    plan = [str(item).strip() for item in (run.get("current_plan") or ()) if str(item).strip()][:12]
    plan_hash = stable_id(*plan)[:16] if plan else ""
    state["plan"] = plan_hash
    if plan and plan_hash != previous.get("plan"):
        emit(
            "plan",
            "Plan updated" if previous.get("plan") else "Plan",
            detail="\n".join(f"{index}. {step}" for index, step in enumerate(plan, start=1)),
            field="plan",
            value=plan_hash,
        )

    fields: tuple[tuple[str, str, str, bool], ...] = (
        ("verified_facts", "fact", "", False),
        ("hypotheses_rejected", "decision", "Rejected: ", False),
        ("open_questions", "question", "", True),
        ("current_blockers", "blocker", "", True),
    )
    for field, kind, prefix, resolvable in fields:
        raw_values = run.get(field) or ()
        values = (
            [str(item).strip() for item in list(raw_values)[:_MAX_STATE_ITEMS] if str(item).strip()]
            if isinstance(raw_values, Sequence) and not isinstance(raw_values, (str, bytes))
            else []
        )
        hashes = [stable_id(value)[:16] for value in values]
        before = list(previous.get(field) or [])
        known = set(before)
        for value, digest in zip(values, hashes, strict=True):
            if digest not in known:
                emit(kind, prefix + value, field=field, value=value, status="open" if resolvable else "")
        if resolvable:
            current = set(hashes)
            texts = dict(previous.get(field + "_text") or {})
            for digest in before:
                if digest not in current and digest in texts:
                    emit(kind, str(texts[digest]), field=field, value=str(texts[digest]), status="resolved")
            state[field + "_text"] = {digest: value[:240] for value, digest in zip(values, hashes, strict=True)}
        state[field] = hashes

    next_validation = str(run.get("next_required_validation") or "").strip()
    state["next"] = next_validation
    previous_next = str(previous.get("next") or "")
    if next_validation and next_validation != previous_next:
        emit("validation", f"Next validation: {next_validation}", status="pending", field="next", value=next_validation)
    if previous_next and previous_next != next_validation:
        emit(
            "validation",
            f"Next validation: {previous_next}",
            status="cleared",
            field="next",
            value=previous_next,
        )
    return drafts, state


# --------------------------------------------------------------------------- #
# Ingestor                                                                    #
# --------------------------------------------------------------------------- #


def _load_cursor(raw: str) -> dict[str, Any]:
    with contextlib.suppress(TypeError, ValueError):
        loaded = json.loads(raw or "{}")
        if isinstance(loaded, dict) and loaded.get("v") == _CURSOR_VERSION:
            return loaded
    return {}


def _dump_cursor(cursor: Mapping[str, Any]) -> str:
    return json.dumps({**cursor, "v": _CURSOR_VERSION}, sort_keys=True, separators=(",", ":"))


def _read_run(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _stat(path: Path) -> tuple[int, int] | None:
    try:
        info = path.stat()
    except OSError:
        return None
    return info.st_mtime_ns, info.st_size


class DevelopmentIngestor:
    """Throttled, incremental RunLedger → Development sink for one repository."""

    def __init__(
        self,
        store: ReviewStore,
        *,
        source_store_root: Path,
        repo_root: Path,
        min_interval_s: float = 2.0,
        discovery_interval_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        today: Callable[[], date] | None = None,
    ) -> None:
        self.development = DevelopmentStore(store)
        self._review_store = store
        self.source_store_root = source_store_root.expanduser().resolve()
        self.repo_root = repo_root.expanduser().resolve()
        self._repo = str(self.repo_root)
        self._needles = (self._repo.encode("utf-8"), json.dumps(self._repo)[1:-1].encode("utf-8"))
        self.min_interval_s = min_interval_s
        self.discovery_interval_s = discovery_interval_s
        self._clock = clock
        # Session directories are dated in local time (``paths.session_dir``).
        self._today = today or date.today
        self._last_sync: dict[str, float] = {}
        self._last_discovery: dict[str, float] = {}
        self._irrelevant: OrderedDict[str, tuple[int, int]] = OrderedDict()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    # ----- public ---------------------------------------------------------- #

    def sync(self, session: ReviewSession, *, force: bool = False) -> int:
        """Ingest new observable evidence for one Change; returns events appended.

        Fail-open: an unreadable ledger or an unavailable backend never breaks
        the Reader, it only leaves Development as it was.
        """

        change_id = session.id
        now = self._clock()
        if not force and now - self._last_sync.get(change_id, -1e9) < self.min_interval_s:
            return 0
        with self._locks_guard:
            lock = self._locks.setdefault(change_id, threading.Lock())
        if not lock.acquire(blocking=force):
            return 0
        try:
            self._last_sync[change_id] = self._clock()
            try:
                return self._sync_locked(session, force=force)
            except DevelopmentUnavailable:
                return 0
        finally:
            lock.release()

    # ----- internals ------------------------------------------------------- #

    def _sync_locked(self, session: ReviewSession, *, force: bool) -> int:
        change_id = session.id
        known = {item.key: item for item in self.development.list_sessions(change_id)}
        refs: dict[str, LedgerRef] = {}
        for item in known.values():
            ref = self._ref_for(item.host, item.session_id, item.cursor_json)
            if ref is not None:
                refs[item.key] = ref
        # Exact edit-time contributors of the current Snapshot are always
        # candidates, even outside the discovery window.
        for host, session_id in self._attributed(change_id):
            key = f"{host}:{session_id}"
            if key not in refs:
                ref = self._ref_for(host, session_id, "{}")
                if ref is not None:
                    refs[key] = ref
        live_scope = session.range_mode in {"working_tree", "staged"}
        now = self._clock()
        if live_scope and (force or now - self._last_discovery.get(change_id, -1e9) >= self.discovery_interval_s):
            self._last_discovery[change_id] = now
            for ref in self._discover(session, skip=set(refs)):
                refs.setdefault(f"{ref.host}:{ref.session_id}", ref)
        # Plan every changed ledger first, then append once: one transaction,
        # one globally time-ordered batch, independent of discovery order.
        plans: dict[str, _SessionPlan] = {}
        for key, ref in sorted(refs.items()):
            plan = self._plan_session(session, ref, known, live_scope=live_scope)
            if plan is not None:
                plans[key] = plan
        # A subagent judged before its parent could not see the parent's new
        # role; re-plan children once against the planned parents.
        planned = {**known, **{key: plan.session for key, plan in plans.items()}}
        for key, ref in sorted(refs.items()):
            parent = planned[key].parent_session_id if key in planned else ""
            if parent and any(item.session_id == parent and item.role == "author" for item in planned.values()):
                plan = self._plan_session(session, ref, known, live_scope=live_scope, force=True, peers=planned)
                if plan is not None:
                    plans[key] = plan
        if not plans:
            return 0
        result = self.development.append(
            change_id,
            [draft for plan in plans.values() for draft in plan.drafts],
            sessions=tuple(plan.session for plan in plans.values()),
            cursor_checks=tuple(
                CursorUpdate(plan.session.host, plan.session.session_id, plan.expected_cursor)
                for plan in plans.values()
            ),
        )
        return len(result.inserted)

    def _attributed(self, change_id: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        with contextlib.suppress(Exception):
            revision = self._review_store.latest_revision(change_id)
            if revision is None:
                return out
            if revision.provenance_certainty == "exact" and revision.provenance_host and revision.provenance_session_id:
                out.append((revision.provenance_host.strip().lower(), revision.provenance_session_id.strip()))
            for item in self._review_store.list_revision_attributions(revision.id):
                if item.certainty == "exact" and item.host and item.session_id:
                    out.append((item.host.strip().lower(), item.session_id.strip()))
        return list(dict.fromkeys(out))

    def _ref_for(self, host: str, session_id: str, cursor_json: str) -> LedgerRef | None:
        cursor = _load_cursor(cursor_json)
        stored = str(cursor.get("ledger") or "")
        if stored:
            path = self.source_store_root / stored
            if path.is_file():
                return LedgerRef(host, session_id, path)
        from lemoncrow.core.foundation.paths import find_session_dir, session_dir

        exact = session_dir(self.source_store_root, host, session_id) / "run.json"
        if exact.is_file():
            return LedgerRef(host, session_id, exact)
        found = find_session_dir(self.source_store_root, session_id)
        if found is not None and found.parent.name == host and (found / "run.json").is_file():
            return LedgerRef(host, session_id, found / "run.json")
        return None

    def _date_dirs(self, session: ReviewSession) -> Iterable[Path]:
        root = self.source_store_root / "sessions"
        today = self._today()
        created = parse_at(normalize_at(session.created_at))
        start = today - timedelta(days=2)
        if created is not None:
            start = min(start, created.date() - timedelta(days=1))
        start = max(start, today - timedelta(days=_MAX_SESSION_DAYS))
        day = start
        while day <= today + timedelta(days=1):
            path = root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
            if path.is_dir():
                yield path
            day += timedelta(days=1)

    def _discover(self, session: ReviewSession, *, skip: set[str]) -> list[LedgerRef]:
        """New candidate ledgers that mention this repository.

        lc-debt: the needle check reads a changed ledger in full once per
        (mtime, size); a busy unrelated session in a path that merely contains
        the repository root is re-read on each change. Upgrade path: a hook-side
        workspace index keyed by repository root.
        """

        found: list[LedgerRef] = []
        for day in self._date_dirs(session):
            try:
                hosts = sorted(os.scandir(day), key=lambda entry: entry.name)
            except OSError:
                continue
            for host_entry in hosts:
                if not host_entry.is_dir():
                    continue
                host = host_entry.name.strip().lower()
                try:
                    entries = sorted(os.scandir(host_entry.path), key=lambda entry: entry.name)
                except OSError:
                    continue
                for entry in entries:
                    key = f"{host}:{entry.name}"
                    if key in skip or not entry.is_dir():
                        continue
                    run_path = Path(entry.path) / "run.json"
                    signature = _stat(run_path)
                    if signature is None:
                        continue
                    cache_key = str(run_path)
                    if self._irrelevant.get(cache_key) == signature:
                        continue
                    try:
                        blob = run_path.read_bytes()
                    except OSError:
                        continue
                    if not any(needle in blob for needle in self._needles):
                        self._remember_irrelevant(cache_key, signature)
                        continue
                    found.append(LedgerRef(host, entry.name, run_path))
        return found

    def _remember_irrelevant(self, key: str, signature: tuple[int, int]) -> None:
        self._irrelevant[key] = signature
        self._irrelevant.move_to_end(key)
        while len(self._irrelevant) > _IRRELEVANT_CACHE:
            self._irrelevant.popitem(last=False)

    def _workspace_is_repo(self, run: Mapping[str, Any]) -> bool:
        workspace = str(run.get("workspace_path") or "").strip()
        if not workspace:
            return False
        try:
            return Path(workspace).expanduser().resolve() == self.repo_root
        except OSError:
            return False

    def _map_events(
        self,
        events: Sequence[Mapping[str, Any]],
        ref: LedgerRef,
        *,
        offset: int,
        workspace_is_repo: bool,
    ) -> list[tuple[DevelopmentDraft, Mapping[str, Any]]]:
        out: list[tuple[DevelopmentDraft, Mapping[str, Any]]] = []
        for index, raw in enumerate(events, start=offset):
            draft = map_ledger_event(
                raw,
                host=ref.host,
                session_id=ref.session_id,
                repo_root=self._repo,
                workspace_is_repo=workspace_is_repo,
                order=index,
            )
            if draft is not None:
                out.append((draft, raw))
        return out

    def _plan_session(
        self,
        session: ReviewSession,
        ref: LedgerRef,
        known: Mapping[str, DevelopmentSession],
        *,
        live_scope: bool,
        force: bool = False,
        peers: Mapping[str, DevelopmentSession] | None = None,
    ) -> _SessionPlan | None:
        change_id = session.id
        key = f"{ref.host}:{ref.session_id}"
        record = known.get(key)
        stored_cursor = record.cursor_json if record is not None else "{}"
        cursor = _load_cursor(stored_cursor)
        signature = _stat(ref.path)
        if signature is None:
            return None
        if not force and cursor.get("mtime_ns") == signature[0] and cursor.get("size") == signature[1]:
            return None
        run = _read_run(ref.path)
        if run is None:
            return None
        raw_events = run.get("events")
        events: list[Mapping[str, Any]] = (
            [item for item in raw_events if isinstance(item, Mapping)] if isinstance(raw_events, list) else []
        )
        workspace_is_repo = self._workspace_is_repo(run)
        explicit = record is not None and record.binding == "explicit"
        purpose = record.purpose if explicit and record is not None else infer_purpose(ref.host, run)
        parent = str(run.get("parent_session_id") or run.get("parent_id") or run.get("parentID") or "").strip()
        if not parent and record is not None:
            parent = record.parent_session_id
        if record is None and not workspace_is_repo:
            relevant = any(
                str(item.get("kind") or "") in {"file_edit", "file_revert"}
                and scoped_path(_payload(item).get("path"), repo_root=self._repo, workspace_is_repo=False)
                for item in events
            )
            if not relevant:
                self._remember_irrelevant(str(ref.path), signature)
                return None

        previous_role = str(cursor.get("role") or "")
        count = int(cursor.get("count") or 0)
        tail_ok = 0 < count <= len(events) and cursor.get("tail") == _identity(events[count - 1])
        # Evidence for a not-yet-admitted session must consider the whole
        # ledger; an admitted one only needs what is new since its cursor.
        incremental = previous_role in VISIBLE_ROLES and tail_ok
        offset = count if incremental else 0
        mapped = self._map_events(events[offset:], ref, offset=offset, workspace_is_repo=workspace_is_repo)
        edits = [item for item in mapped if item[0].kind in {"edit", "revert"}]
        if not live_scope and record is None:
            edits = []
        has_edit = bool(edits)
        has_verify = any(item[0].kind in {"test", "validation", "command"} for item in mapped)
        model = record.model if record is not None else ""
        for _draft, raw in edits:
            if not model:
                model = str(_payload(raw).get("model") or "").strip()

        role = self._role(
            record,
            purpose=purpose,
            parent=parent,
            known=peers if peers is not None else known,
            has_edit=has_edit,
            has_verify=has_verify,
        )
        new_cursor: dict[str, Any] = {
            "ledger": _relative(ref.path, self.source_store_root),
            "mtime_ns": signature[0],
            "size": signature[1],
            "count": len(events),
            "tail": _identity(events[-1]) if events else "",
            "role": role,
            "state": cursor.get("state") or {},
            "emitted": cursor.get("emitted") or {},
            "completed": cursor.get("completed") or "",
        }
        drafts: list[DevelopmentDraft] = []
        allowed = frozenset() if role == "investigator" else ADMITTED_KINDS.get(role, frozenset())
        if purpose in {"review", "ask"}:
            allowed = allowed & REVIEW_PURPOSE_KINDS
        if allowed:
            upgraded = role != previous_role
            if upgraded and incremental:
                # A verifier that starts editing becomes an author: its whole
                # observable history is now Development. UNIQUE ids dedupe.
                mapped = self._map_events(events, ref, offset=0, workspace_is_repo=workspace_is_repo)
            if upgraded and role == "author":
                new_cursor["state"] = {}
                new_cursor["emitted"] = {}
            if previous_role not in VISIBLE_ROLES and "task_started" in allowed:
                first_at = mapped[0][0].at if mapped else ""
                drafts.append(
                    DevelopmentDraft(
                        kind="task_started",
                        at=normalize_at(run.get("created_at")) or first_at,
                        source_event_id=f"lifecycle:start:{ref.host}:{ref.session_id}",
                        host=ref.host,
                        session_id=ref.session_id,
                        summary=str(run.get("task") or "") or "Session joined this Change",
                        order=-1,
                    )
                )
            drafts.extend(draft for draft, _raw in mapped if draft.kind in allowed)
            if role == "author" and purpose not in {"review", "ask"}:
                emitted = dict(new_cursor["emitted"])
                state_rows, state_cursor = state_drafts(
                    run,
                    dict(new_cursor["state"]),
                    host=ref.host,
                    session_id=ref.session_id,
                    emitted=emitted,
                )
                drafts.extend(state_rows)
                new_cursor["state"] = state_cursor
                new_cursor["emitted"] = emitted
            status = str(run.get("status") or "").strip().lower()
            if (
                "task_completed" in allowed
                and status in {"done", "completed", "failed", "error"}
                and new_cursor["completed"] != status
            ):
                ended = normalize_at(run.get("updated_at"))
                drafts.append(
                    DevelopmentDraft(
                        kind="task_completed",
                        at=ended,
                        source_event_id=f"lifecycle:end:{ref.host}:{ref.session_id}:{status}:{ended}",
                        host=ref.host,
                        session_id=ref.session_id,
                        summary="Session completed" if status in {"done", "completed"} else "Session failed",
                        status="pass" if status in {"done", "completed"} else "fail",
                        order=1 << 30,
                    )
                )
                new_cursor["completed"] = status
            drafts = self._with_causation(change_id, ref, [replace(item, role=role) for item in drafts])

        status_text = str(run.get("status") or "").strip().lower()
        updated = DevelopmentSession(
            change_id=change_id,
            host=ref.host,
            session_id=ref.session_id,
            parent_session_id=parent,
            model=model,
            purpose=purpose if purpose else (record.purpose if record is not None else ""),
            role=role,
            binding=record.binding if record is not None else "observed",
            task=str(run.get("task") or ""),
            status=status_text,
            started_at=normalize_at(run.get("created_at")),
            ended_at=normalize_at(run.get("updated_at")) if status_text in {"done", "completed", "failed"} else "",
            cursor_json=_dump_cursor(new_cursor),
        )
        return _SessionPlan(key=key, drafts=tuple(drafts), session=updated, expected_cursor=stored_cursor)

    def _role(
        self,
        record: DevelopmentSession | None,
        *,
        purpose: str,
        parent: str,
        known: Mapping[str, DevelopmentSession],
        has_edit: bool,
        has_verify: bool,
    ) -> str:
        if purpose in {"benchmark", "system"}:
            return "excluded"
        current = record.role if record is not None else "observer"
        if current == "excluded":
            return "excluded"
        if has_edit or current == "author":
            return "author"
        explicit = record is not None and record.binding == "explicit"
        parent_is_author = bool(parent) and any(
            item.session_id == parent and item.role == "author" for item in known.values()
        )
        if has_verify and (explicit or parent_is_author or current == "verifier"):
            return "verifier"
        if current in VISIBLE_ROLES:
            return current
        if (explicit and purpose == "investigate") or parent_is_author:
            return "investigator"
        return "observer"

    def _with_causation(
        self,
        change_id: str,
        ref: LedgerRef,
        drafts: list[DevelopmentDraft],
    ) -> list[DevelopmentDraft]:
        """Stamp work that followed a Review handoff to this exact session."""

        if not drafts:
            return drafts
        handoffs = sorted(
            (parsed, causation)
            for at, causation in self.development.handoffs_for(change_id, ref.host, ref.session_id)
            if (parsed := parse_at(at)) is not None
        )
        if not handoffs:
            return drafts
        out: list[DevelopmentDraft] = []
        for draft in drafts:
            when = parse_at(draft.at)
            causation = ""
            if when is not None and draft.kind not in {"task_started", "intent"}:
                for at, candidate in handoffs:
                    if at <= when:
                        causation = candidate
            out.append(replace(draft, causation_id=causation) if causation else draft)
        return out


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return ""


_INGESTORS: dict[tuple[str, str], DevelopmentIngestor] = {}
_INGESTORS_GUARD = threading.Lock()


def ingestor_for(store: ReviewStore, *, source_store_root: Path, repo_root: Path) -> DevelopmentIngestor:
    """One ingestor (and its caches/throttle) per (store, repository) per process."""

    key = (str(store.root), str(repo_root.expanduser().resolve()))
    with _INGESTORS_GUARD:
        found = _INGESTORS.get(key)
        if found is None:
            found = DevelopmentIngestor(store, source_store_root=source_store_root, repo_root=repo_root)
            _INGESTORS[key] = found
        return found


def sync_before_capture(
    store: ReviewStore,
    session: ReviewSession,
    *,
    repo_root: Path,
    source_store_root: Path,
) -> None:
    """Best-effort catch-up so a new Snapshot's watermark includes recent work."""

    if session.range_mode not in {"working_tree", "staged"}:
        return
    with contextlib.suppress(Exception):
        ingestor_for(store, source_store_root=source_store_root, repo_root=repo_root).sync(session, force=True)


__all__ = [
    "DevelopmentIngestor",
    "LedgerRef",
    "infer_purpose",
    "ingestor_for",
    "map_ledger_event",
    "scoped_path",
    "state_drafts",
    "sync_before_capture",
]
