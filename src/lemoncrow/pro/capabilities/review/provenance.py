"""Correlate a host agent session to the change set a review packet describes.

Why this module exists: nothing in the session substrate stores a commit sha, a
branch or a worktree id, so "which agent produced this diff" is not a join --
it is a scored guess over workspace path, edited-file overlap and wall-clock
proximity. A confident wrong attribution is strictly worse than an honest gap,
so three gates stand between the score and a named author, and failing any one
of them collapses every field of the record back to its default with ``status``
``"unknown"``:

1. **The floor.** Below ``_AMBIGUOUS_FLOOR`` nothing is reported at all.
2. **Authorship.** Workspace path and wall-clock proximity are presence, not
   authorship. A session that recorded no edit to any reviewed file did not
   write the diff, whatever it scores -- without this gate a read-only session
   sitting in the right directory at the right time scores 0.45 + 0.15 and gets
   printed as the author of every file in the range.
3. **The margin.** A tie or near-tie is two answers, not one. Naming either
   side's host and model publishes the winner of a coin flip.

What survives all three is still a guess, and says so: ``certainty`` is
``"exact"`` only for the two anchors below, and every renderer prints that word
next to the host it qualifies. Keeping the heuristic and its gates in one module
is what stops the packet orchestrator from ever having to decide how much
evidence is enough.

Three signals the substrate does record exactly are handled before any scoring:
an explicit ``--session-id`` from the user; a run ledger whose ``file_edit``
events name a reviewed path, stamped by the host's own hook at the moment of
the edit; and -- for runs written after the ``run.json`` ``"git"`` anchor
landed -- a run ledger whose recorded HEAD equals the reviewed head sha. The
second is the only one that fires in ``working_tree`` mode, where there is no
head sha to anchor on, which is the default mode of ``lc review``. Everything
else is a heuristic and says so in ``ProvenanceRecord.match_reason``, which is
always populated so a wrong match stays auditable rather than silent.

Evidence is deliberately thin: ``Trace.validation_results`` is hardcoded empty
on every import path and ``RunLedger.record_test`` has no callers, so the only
real signal is a recorded shell command that carries an exit code. Nothing here
ever synthesises a ``PASS``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath

from lemoncrow.core.foundation.models import Trace

from .models import (
    EvidenceRecord,
    EvidenceStatus,
    ImpactSite,
    ProvenanceCertainty,
    ProvenanceRecord,
    ProvenanceStatus,
    unknown_provenance,
)

# --------------------------------------------------------------------------- #
# Scoring constants. Every one of these is a policy decision, not a tuning     #
# knob discovered by fitting -- there is no labelled corpus of (session,       #
# commit) pairs to fit against, which is exactly why the floor matters.        #
# --------------------------------------------------------------------------- #

_WORKSPACE_WEIGHT = 0.45
"""``Trace.workspace_path`` is NULL for several importers, so this can honestly be 0."""
_OVERLAP_WEIGHT = 0.40
_TIME_WEIGHT = 0.15

_MATCH_FLOOR = 0.60
"""Below this nothing is ever reported as ``matched``."""
_MATCH_MARGIN = 0.15
"""A winner must also be this far clear of the runner-up, or it is ambiguous."""
_AMBIGUOUS_FLOOR = 0.35
"""Below this the record is ``unknown`` with every other field at its default."""

_CANDIDATE_WINDOW = timedelta(days=7)
_CANDIDATE_LIMIT = 200
_SESSION_LOOKUP_LIMIT = 500
_GIT_ANCHOR_PROBES = 25
"""How many of the most recent candidates get a run.json probe.

``find_session_dir`` globs the whole session tree per call, and the ``"git"``
key only exists for runs written after that forward fix shipped, so probing the
recency head is both cheap and exactly where the anchor can be found.
"""

_FULL_CREDIT_BEFORE = timedelta(hours=24)
_FULL_CREDIT_AFTER = timedelta(hours=1)
_ZERO_CREDIT_AT = timedelta(hours=72)

_PATH_CAP = 500
_COMMAND_CAP = 50
_COMMAND_CHARS = 200
_DETAIL_CHARS = 120
_TASK_CHARS = 200

_GENERIC_BASENAMES = frozenset(
    {
        "__init__.py",
        "index.js",
        "index.ts",
        "index.tsx",
        "main.go",
        "mod.rs",
        "package.json",
        "types.ts",
        "utils.py",
    }
)
"""Basenames too common to carry evidence on their own.

A basename-only hit is the weakest tier of path matching (see
:func:`_match_paths`); for these names it is noise, so it is refused outright.
"""

_TEST_RE = re.compile(r"\b(pytest|jest|go\s+test|cargo\s+test|npm\s+test)\b")
_TYPECHECK_RE = re.compile(r"\b(mypy|tsc|pyright)\b")
_LINT_RE = re.compile(r"\b(ruff|eslint|clippy|golangci(?:-lint)?)\b")
_SITE_LINE_RE = re.compile(r":L\d+$")

_PATHY_SUFFIXES = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".java", ".rb")
_WHOLE_TREE_TOKENS = frozenset({"./...", "...", "./", "."})


@dataclass(frozen=True)
class _Candidate:
    """One scored session, kept with the evidence that produced the score."""

    trace: Trace
    score: float
    reasons: tuple[str, ...]
    overlap: int = 0
    """How many reviewed files this session recorded an *edit* to.

    Kept separately from ``score`` because it is the only signal that speaks to
    authorship at all. Workspace path and wall-clock proximity say a session
    was in the room; they cannot say it wrote anything.
    """


@dataclass(frozen=True)
class _Correlation:
    """Correlator output plus the trace it settled on, for the evidence pass."""

    record: ProvenanceRecord
    trace: Trace | None
    degraded: tuple[str, ...]


@dataclass(frozen=True)
class _Command:
    """A recorded command flattened to the two fields evidence depends on."""

    text: str
    exit_code: int | None


# --------------------------------------------------------------------------- #
# Path handling                                                               #
# --------------------------------------------------------------------------- #


def _normalize(path: str) -> str:
    """Return *path* as a plain posix string, or ``""`` when it carries nothing."""

    text = str(path).strip().replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    return text


def _basename(path: str) -> str:
    return PurePosixPath(path).name


def _tail_match(longer: str, shorter: str) -> bool:
    return longer == shorter or longer.endswith("/" + shorter)


def _match_paths(recorded: Iterable[str], targets: Sequence[str]) -> frozenset[str]:
    """Return the subset of *targets* that some *recorded* path refers to.

    Recorded session paths are host-absolute; packet paths are repo-relative,
    so equality is never the right test. Three tiers, strongest first: exact
    string, relative-tail suffix, and -- only when the basename is unique in
    *targets* and not a generic filename -- basename alone. Every tier implies
    identical basenames, which is what lets the whole comparison run out of one
    basename bucket instead of a quadratic scan.
    """

    normalized_targets = [(item, _normalize(item)) for item in targets]
    normalized_targets = [pair for pair in normalized_targets if pair[1]]
    if not normalized_targets:
        return frozenset()

    bucket: dict[str, list[str]] = {}
    for item in recorded:
        norm = _normalize(item)
        if not norm:
            continue
        bucket.setdefault(_basename(norm), []).append(norm)
    if not bucket:
        return frozenset()

    counts: dict[str, int] = {}
    for _, norm in normalized_targets:
        name = _basename(norm)
        counts[name] = counts.get(name, 0) + 1

    hits: set[str] = set()
    for original, norm in normalized_targets:
        name = _basename(norm)
        candidates = bucket.get(name)
        if not candidates:
            continue
        allow_basename = counts.get(name, 0) == 1 and name not in _GENERIC_BASENAMES
        for recorded_path in candidates:
            if allow_basename or _tail_match(recorded_path, norm) or _tail_match(norm, recorded_path):
                hits.add(original)
                break
    return frozenset(hits)


def _site_file(site_path: str) -> str:
    """Strip the ``:L123`` suffix an impact site carries, leaving the file path."""

    return _SITE_LINE_RE.sub("", str(site_path).strip())


# --------------------------------------------------------------------------- #
# Trace projection                                                            #
# --------------------------------------------------------------------------- #


def split_files_touched(trace: Trace) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(read_paths, edited_paths)`` from ``Trace.files_touched``.

    The field mixes both: the Claude importer appends a read as a bare ``str``
    and an edit as a ``FileEditRecord``; every other importer records edits
    only, which is why ``read_paths`` is empty for most hosts (risk R2). A
    Claude edit whose diff could not be inferred also lands as a bare ``str``,
    so the read side is a superset of true reads -- fine for the one question
    it answers, "did the agent have this file in front of it".

    Both sides are deduplicated and sorted so a packet built twice from one
    trace is byte-identical.
    """

    reads: set[str] = set()
    edits: set[str] = set()
    for entry in trace.files_touched:
        if isinstance(entry, str):
            path = entry.strip()
            if path:
                reads.add(path)
            continue
        path = str(entry.path).strip()
        if path:
            edits.add(path)
    return tuple(sorted(reads)), tuple(sorted(edits))


def _clean(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _commands(trace: Trace) -> tuple[str, ...]:
    """Return the recorded command lines, first-seen order, deduped and capped."""

    out: list[str] = []
    seen: set[str] = set()
    for entry in trace.commands_run:
        text = (entry if isinstance(entry, str) else entry.command).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text[:_COMMAND_CHARS])
        if len(out) >= _COMMAND_CAP:
            break
    return tuple(out)


def _commands_with_codes(trace: Trace) -> tuple[_Command, ...]:
    """Return every recorded command with its exit code, ``None`` when unrecorded."""

    out: list[_Command] = []
    for entry in trace.commands_run:
        if isinstance(entry, str):
            text = entry.strip()
            if text:
                out.append(_Command(text=text, exit_code=None))
            continue
        text = entry.command.strip()
        if text:
            out.append(_Command(text=text, exit_code=entry.exit_code))
    return tuple(out)


def _subagents(trace: Trace) -> tuple[tuple[str, int], ...]:
    """Return sorted ``(agent_type, count)`` pairs; Claude-only in practice."""

    raw = trace.telemetry.get("subagent_names")
    if not isinstance(raw, dict):
        return ()
    items: list[tuple[str, int]] = []
    for name, count in raw.items():
        label = str(name).strip()
        if not label:
            continue
        try:
            number = int(count)
        except (TypeError, ValueError):
            continue
        if number > 0:
            items.append((label, number))
    return tuple(sorted(items))


def _aware(moment: datetime) -> datetime:
    """Return *moment* as timezone-aware UTC; stored timestamps are heterogeneous."""

    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Store access -- every path here degrades to "nothing found", never an error  #
# --------------------------------------------------------------------------- #


def load_trace_for_session(store_root: Path, session_id: str) -> Trace | None:
    """Return the trace whose session id is *session_id*, or ``None``.

    There is no indexed lookup by session id, so this scans the most recent
    page of traces. ``Trace.id`` is accepted as a fallback key because several
    importers mint the trace id from the host session id and users paste
    whichever one their host showed them.
    """

    wanted = str(session_id).strip()
    if not wanted:
        return None
    try:
        from lemoncrow.core.foundation.history_store import HistoryStore

        store = HistoryStore(store_root)
        if not store.db_path.exists():
            return None
        traces = store.list_traces(limit=_SESSION_LOOKUP_LIMIT)
    except Exception:
        # A missing, locked or schema-less store is a gap, not a failure: the
        # caller's contract is to return "unknown", never to raise.
        return None
    for trace in traces:
        if (trace.session_id or "") == wanted:
            return trace
    for trace in traces:
        if trace.id == wanted:
            return trace
    return None


def _load_candidates(store_root: Path, anchor: datetime) -> tuple[tuple[Trace, ...], tuple[str, ...]]:
    """Return recent traces around *anchor*, plus any degradation names."""

    try:
        from lemoncrow.core.foundation.history_store import HistoryStore

        store = HistoryStore(store_root)
        if not store.db_path.exists():
            return (), ("provenance_store_missing",)
        traces = store.list_traces(since=anchor - _CANDIDATE_WINDOW, limit=_CANDIDATE_LIMIT)
    except Exception:
        return (), ("provenance_store_missing",)
    return tuple(traces), ()


def _run_ledger_head(store_root: Path, session_id: str | None) -> str:
    """Return the HEAD sha the run ledger recorded for *session_id*, or ``""``.

    Reads ``run.json``'s ``"git"`` block directly rather than through
    ``RunLedger.load``: the ledger loader raises on a corrupt file and rebuilds
    a whole object, and all this needs is one string. Runs written before that
    key existed simply have no anchor, which is the honest answer.
    """

    if not session_id:
        return ""
    try:
        from lemoncrow.core.foundation.paths import find_session_dir

        directory = find_session_dir(store_root, session_id)
        if directory is None:
            return ""
        run_file = directory / "run.json"
        if not run_file.is_file():
            return ""
        payload = json.loads(run_file.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    git = payload.get("git")
    if not isinstance(git, dict):
        return ""
    head = git.get("head")
    return head.strip() if isinstance(head, str) else ""


def _session_dirs(store_root: Path) -> dict[str, Path]:
    """Return ``{session_id: directory}`` for every session under *store_root*.

    One walk of ``sessions/YYYY/MM/DD/<host>/<id>`` rather than
    :func:`find_session_dir`'s glob per lookup: the exact-edit pass asks about
    every candidate in the window, and 200 globs over one tree is the same walk
    done 200 times. Earliest date wins on a duplicated id, matching
    ``find_session_dir``'s ``sorted(...)[0]``. Degrades to ``{}``.
    """

    sessions_root = store_root / "sessions"
    out: dict[str, Path] = {}
    try:
        if not sessions_root.is_dir():
            return out
        for path in sorted(sessions_root.glob("*/*/*/*/*")):
            if path.is_dir():
                out.setdefault(path.name, path)
    except OSError:
        return out
    return out


def _recorded_edits(run_file: Path, needles: frozenset[str]) -> tuple[tuple[str, ...], str, str]:
    """Return ``(edited_paths, host, model)`` the run ledger recorded at edit time.

    Only ``file_edit`` events count: ``file_revert`` undoes authorship rather
    than establishing it. ``host`` and ``model`` come off the payload the hook
    stamped, which for a host whose ``Trace.workspace_path`` is NULL is the only
    place either was ever written down.

    *needles* are the reviewed basenames, used as a cheap superset prefilter: a
    file whose text contains none of them cannot claim one and is not worth
    parsing. Session ``run.json`` files reach several MB.

    The caller supplies each basename in **both** its literal form and its JSON
    escaping (``run_ledger`` writes with the default ``ensure_ascii=True``, so
    ``café.py`` is stored as ``caf\\u00e9.py``). Without the escaped form a
    non-ASCII path was silently invisible here and its session dropped to the
    heuristic ladder.
    """

    try:
        raw = run_file.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return (), "", ""
    if needles and not any(name in raw for name in needles):
        return (), "", ""
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return (), "", ""
    if not isinstance(payload, dict):
        return (), "", ""
    events = payload.get("events")
    if not isinstance(events, list):
        return (), "", ""
    paths: list[str] = []
    host = ""
    model = ""
    for event in events:
        if not isinstance(event, dict) or event.get("kind") != "file_edit":
            continue
        body = event.get("payload")
        if not isinstance(body, dict):
            continue
        recorded = body.get("path")
        if isinstance(recorded, str) and recorded.strip():
            paths.append(recorded.strip())
        if not host:
            host = str(body.get("host") or "").strip()
        if not model:
            model = str(body.get("model") or "").strip()
    return tuple(dict.fromkeys(paths)), host, model


def _under_repo(repo_root: str, recorded: str, *, workspace_is_repo: bool) -> bool:
    """Whether *recorded* is compatible with a review of *repo_root*.

    An absolute recording names the tree it was made in, so an edit to another
    checkout's ``src/app/service.py`` must not claim this one's -- the tail
    matching in :func:`_match_paths` cannot tell the two apart, and this path
    reports a fact rather than a score.

    A **relative** recording names no tree at all, and relative is the common
    case: the ``mcp__lc__edit`` surface records whatever string the agent typed
    (``mcp_server.py:6825``), and codex's ``file_change`` items are
    workspace-relative (``plugin_runtime.py:3964``). Accepting one unconditionally
    let a session that edited ``src/app.py`` in an unrelated project be reported
    as this review's author at ``certainty="exact"``. It is evidence only when
    the tree it was relative to is independently known to be this one -- i.e.
    the trace's ``workspace_path`` resolves to ``repo_root``. Otherwise it goes
    back to the heuristic ladder, which hedges.
    """

    norm = _normalize(recorded)
    if not norm.startswith("/"):
        return workspace_is_repo
    return norm == repo_root or norm.startswith(repo_root + "/")


def _exact_edit_anchor(
    store_root: Path,
    repo_root: Path,
    packet_paths: Sequence[str],
    candidates: Sequence[Trace],
) -> _Correlation | None:
    """Sessions that recorded editing a reviewed path, at edit time.

    This is the one path in this module that reports a fact. The hooks stamp
    ``path``/``host``/``model``/``at_head`` into ``run.json`` at the moment of
    the edit (plan Phase 0 item 2), so a session that names a reviewed file did
    write it -- no window, no weights, no floor.

    Reuses the caller's already-materialised candidate window
    (``_CANDIDATE_WINDOW`` = 7 days, ``_CANDIDATE_LIMIT`` = 200) and
    :func:`_match_paths`' relative-tail matching, since recorded paths are
    host-absolute and packet paths repo-relative. ``_GENERIC_BASENAMES`` is
    honoured through that same function: ``__init__.py`` is never evidence on
    its basename alone.

    Returns ``None`` when zero or more than one session claims the paths; the
    caller then falls through to the existing heuristic ladder, which keeps its
    own hedge. Two claimants is mixed authorship, which is real, and settling it
    by picking one would publish a coin flip as a recorded fact.

    Works in ``working_tree`` mode, where ``head_sha == ""`` and the run-ledger
    git anchor can never fire -- which is the default mode of ``lc review``.
    """

    targets = tuple(item for item in packet_paths if str(item).strip())
    if not targets or not candidates:
        return None
    directories = _session_dirs(store_root)
    if not directories:
        return None

    plain = [name for name in (_basename(_normalize(item)) for item in targets) if name]
    # Both spellings: the ledger is written with ensure_ascii=True, so a
    # non-ASCII basename never appears literally in the file's text.
    needles = frozenset(plain) | frozenset(json.dumps(name)[1:-1] for name in plain)
    root = _normalize(str(repo_root))
    claimants: list[tuple[Trace, str, str]] = []
    seen: set[str] = set()
    for trace in candidates:
        session_id = (trace.session_id or "").strip()
        if not session_id or session_id in seen:
            # A re-imported session appears twice; counting it twice would read
            # as two authors and refuse an answer it actually has.
            continue
        directory = directories.get(session_id)
        if directory is None:
            continue
        seen.add(session_id)
        recorded, host, model = _recorded_edits(directory / "run.json", needles)
        workspace_is_repo = _same_workspace(trace, repo_root)
        in_repo = tuple(item for item in recorded if _under_repo(root, item, workspace_is_repo=workspace_is_repo))
        if not in_repo or not _match_paths(in_repo, targets):
            continue
        claimants.append((trace, host, model))
        if len(claimants) > 1:
            return None
    if len(claimants) != 1:
        return None

    trace, host, model = claimants[0]
    record = _record_from_trace(
        trace,
        status="matched",
        confidence=1.0,
        certainty="exact",
        reason="recorded at edit time",
    )
    # The payload was written by the host, about itself, at edit time. It only
    # fills a gap the trace left -- it never overrides what the importer stored.
    if not record.host and host:
        record = replace(record, host=host)
    if not record.model and model:
        record = replace(record, model=model)
    return _Correlation(record, trace, _reads_degraded(record))


# --------------------------------------------------------------------------- #
# Scoring                                                                     #
# --------------------------------------------------------------------------- #


def _anchor(head_commit_time: datetime | None) -> datetime:
    """Return the time the window is measured from.

    ``working_tree`` and ``staged`` ranges have no commit, so there is no commit
    time to anchor on and "now" is the only truthful stand-in.
    """

    return _aware(head_commit_time) if head_commit_time is not None else datetime.now(tz=UTC)


def _time_score(created_at: datetime, anchor: datetime) -> float:
    """Return 1.0 inside the window, falling linearly to 0.0 at 72h either side."""

    moment = _aware(created_at)
    if anchor - _FULL_CREDIT_BEFORE <= moment <= anchor + _FULL_CREDIT_AFTER:
        return 1.0
    if moment < anchor:
        span = (_ZERO_CREDIT_AT - _FULL_CREDIT_BEFORE).total_seconds()
        distance = ((anchor - _FULL_CREDIT_BEFORE) - moment).total_seconds()
    else:
        span = (_ZERO_CREDIT_AT - _FULL_CREDIT_AFTER).total_seconds()
        distance = (moment - (anchor + _FULL_CREDIT_AFTER)).total_seconds()
    if span <= 0.0:
        return 0.0
    return max(0.0, 1.0 - distance / span)


def _same_workspace(trace: Trace, repo_root: Path) -> bool:
    raw = (trace.workspace_path or "").strip()
    if not raw:
        # NULL for antigravity/hermes/pi/normalized imports -- absence of
        # evidence, so it simply scores nothing.
        return False
    try:
        return Path(raw).resolve() == repo_root
    except OSError:
        return False


def _score(trace: Trace, repo_root: Path, targets: Sequence[str], anchor: datetime) -> _Candidate:
    """Score one candidate out of 1.0 and record which evidence fired."""

    score = 0.0
    reasons: list[str] = []
    overlap = 0

    if _same_workspace(trace, repo_root):
        score += _WORKSPACE_WEIGHT
        reasons.append("workspace path")

    _, edited = split_files_touched(trace)
    if targets and edited:
        hits = _match_paths(edited, targets)
        if hits:
            overlap = len(hits)
            score += _OVERLAP_WEIGHT * (overlap / max(1, len(targets)))
            reasons.append(f"{overlap}/{len(targets)} changed files")

    factor = _time_score(trace.created_at, anchor)
    if factor > 0.0:
        score += _TIME_WEIGHT * factor
        reasons.append("time window")

    return _Candidate(trace=trace, score=round(min(score, 1.0), 4), reasons=tuple(reasons), overlap=overlap)


def _reads_degraded(record: ProvenanceRecord) -> tuple[str, ...]:
    """Risk R2: a matched host that never records reads is a gap worth naming."""

    return () if record.files_inspected else ("agent_reads_unrecorded",)


def _unknown(reason: str) -> ProvenanceRecord:
    """Return the honest-gap record, carrying *reason* so the gap is auditable."""

    return replace(unknown_provenance(), match_reason=reason)


def _record_from_trace(
    trace: Trace,
    *,
    status: ProvenanceStatus,
    confidence: float,
    certainty: ProvenanceCertainty,
    reason: str,
) -> ProvenanceRecord:
    """Project a trace onto the record. ``uninspected_impacted`` is cross-filled later."""

    reads, edits = split_files_touched(trace)
    task = _clean(trace.task)
    return ProvenanceRecord(
        status=status,
        host=_clean(trace.host) or _clean(trace.agent),
        model=_clean(trace.model),
        session_id=_clean(trace.session_id),
        task=task[:_TASK_CHARS] if task else None,
        files_inspected=reads[:_PATH_CAP],
        files_changed=edits[:_PATH_CAP],
        commands_run=_commands(trace),
        subagents=_subagents(trace),
        match_confidence=round(min(max(confidence, 0.0), 1.0), 4),
        certainty=certainty,
        match_reason=reason,
    )


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #


def _correlate(
    store_root: Path,
    repo_root: Path,
    packet_paths: Sequence[str],
    *,
    head_sha: str,
    head_commit_time: datetime | None,
    session_id: str | None = None,
) -> _Correlation:
    """Run the whole correlation once, keeping the winning trace for the caller."""

    anchor = _anchor(head_commit_time)
    targets = tuple(item for item in packet_paths if str(item).strip())
    try:
        root = repo_root.resolve()
    except OSError:
        root = repo_root

    if session_id:
        # The user's escape hatch from the heuristic: explicit means explicit,
        # so a miss reports the miss instead of quietly scoring instead.
        trace = load_trace_for_session(store_root, session_id)
        if trace is None:
            return _Correlation(
                _unknown(f"explicit session id {session_id} not found in the local history store"),
                None,
                ("provenance_session_not_found",),
            )
        record = _record_from_trace(
            trace,
            status="matched",
            confidence=1.0,
            certainty="exact",
            reason="explicit --session-id",
        )
        return _Correlation(record, trace, _reads_degraded(record))

    candidates, degraded = _load_candidates(store_root, anchor)
    if not candidates:
        reason = "no session records in the local history store for this window"
        return _Correlation(_unknown(reason), None, degraded or ("provenance_unmatched",))

    # Before any scoring: a session that recorded editing one of these files, at
    # the moment it edited it, is not a candidate to be ranked. It is the answer.
    exact = _exact_edit_anchor(store_root, root, targets, candidates)
    if exact is not None:
        return _Correlation(exact.record, exact.trace, degraded + exact.degraded)

    if head_sha:
        for trace in candidates[:_GIT_ANCHOR_PROBES]:
            if _run_ledger_head(store_root, trace.session_id) != head_sha:
                continue
            record = _record_from_trace(
                trace,
                status="matched",
                confidence=1.0,
                certainty="exact",
                reason=f"run ledger git anchor (HEAD {head_sha[:12]})",
            )
            return _Correlation(record, trace, degraded + _reads_degraded(record))

    scored = sorted(
        (_score(trace, root, targets, anchor) for trace in candidates),
        key=lambda candidate: (-candidate.score, candidate.trace.id),
    )
    top = scored[0]

    if top.score < _AMBIGUOUS_FLOOR:
        reason = (
            f"no session scored above {_AMBIGUOUS_FLOOR:.2f} "
            f"(best {top.score:.2f} of {len(scored)} candidates in the 7-day window)"
        )
        return _Correlation(_unknown(reason), None, (*degraded, "provenance_unmatched"))

    # The authorship gate. Workspace path and wall-clock proximity say a session
    # was *near* this diff; only a recorded edit to a reviewed file says it wrote
    # one. Scoring them together lets 0.45 + 0.15 clear the floor on presence
    # alone, which is how a read-only session that authored nothing came to be
    # printed as the author of 82 files. A session that recorded no edit to any
    # reviewed path cannot be the author of it, at any score.
    authoring = tuple(item for item in scored if item.overlap > 0)
    if not authoring:
        reason = (
            f"no session recorded an edit to any of the {len(targets)} reviewed files "
            f"(best {top.score:.2f} of {len(scored)} candidates, scored on workspace and timing alone)"
        )
        return _Correlation(_unknown(reason), None, (*degraded, "provenance_unmatched"))

    best = authoring[0]
    runner_up = authoring[1].score if len(authoring) > 1 else 0.0
    margin = round(best.score - runner_up, 4)
    evidence = ", ".join(best.reasons) or "no positive signal"

    if len(authoring) > 1 and margin < _MATCH_MARGIN:
        # A tie is not a match, it is two answers. Naming either one's host and
        # model -- even under an "ambiguous" badge -- publishes the winner of a
        # coin flip as the author, so nothing is named at all.
        reason = (
            f"{len(authoring)} sessions edited these files and none stands clear "
            f"(best {best.score:.2f}, runner-up {runner_up:.2f}, inside the {_MATCH_MARGIN:.2f} margin)"
        )
        return _Correlation(_unknown(reason), None, (*degraded, "provenance_ambiguous"))

    if best.score >= _MATCH_FLOOR:
        reason = f"{evidence} (score {best.score:.2f}, next best {runner_up:.2f})"
        record = _record_from_trace(
            best.trace,
            status="matched",
            confidence=best.score,
            certainty="probable",
            reason=reason,
        )
        return _Correlation(record, best.trace, degraded + _reads_degraded(record))

    if best.score >= _AMBIGUOUS_FLOOR:
        reason = f"{evidence} (score {best.score:.2f}, below the {_MATCH_FLOOR:.2f} match floor)"
        record = _record_from_trace(
            best.trace,
            status="ambiguous",
            confidence=best.score,
            certainty="possible",
            reason=reason,
        )
        return _Correlation(record, best.trace, (*degraded, "provenance_ambiguous", *_reads_degraded(record)))

    reason = (
        f"the best-scoring session that edited a reviewed file scored {best.score:.2f}, "
        f"below the {_AMBIGUOUS_FLOOR:.2f} floor"
    )
    return _Correlation(_unknown(reason), None, (*degraded, "provenance_unmatched"))


def _status_for(matches: Sequence[_Command]) -> tuple[EvidenceStatus, _Command | None]:
    """Return the honest aggregate status for a category.

    Precedence is FAIL > UNKNOWN > PASS: one failing run makes the category
    failing, and one run whose exit code was never recorded makes the category
    unknown. A ``PASS`` is only ever reported when every matching command
    recorded exit 0.
    """

    if not matches:
        return "NOT_RUN", None
    failed = [item for item in matches if item.exit_code is not None and item.exit_code != 0]
    if failed:
        return "FAIL", failed[0]
    unrecorded = [item for item in matches if item.exit_code is None]
    if unrecorded:
        return "UNKNOWN", unrecorded[0]
    return "PASS", matches[0]


def _evidence(name: str, matches: Sequence[_Command], source: str) -> EvidenceRecord:
    status, chosen = _status_for(matches)
    if chosen is None:
        return EvidenceRecord(name=name, status=status, detail="", source="none")
    suffix = f" (exit {chosen.exit_code})" if chosen.exit_code is not None else " (no exit code recorded)"
    return EvidenceRecord(name=name, status=status, detail=f"{chosen.text[:_DETAIL_CHARS]}{suffix}", source=source)


def _names_a_path(command: str) -> bool:
    """Return whether the test invocation in *command* points at a specific path."""

    match = _TEST_RE.search(command)
    if match is None:
        return False
    for token in command[match.end() :].split():
        if token.startswith("-") or token in _WHOLE_TREE_TOKENS:
            continue
        if "/" in token or "::" in token or token.endswith(_PATHY_SUFFIXES):
            return True
    return False


def collect_evidence(store_root: Path, trace: Trace | None, session_id: str | None) -> tuple[EvidenceRecord, ...]:
    """Return the verification signals that actually ran during the session.

    Derived only from recorded shell commands and their exit codes (risk R14):
    ``Trace.validation_results`` is hardcoded empty on every import path and
    ``RunLedger.record_test`` has no callers, so anything else would be invented.
    A command recorded as a bare string carries no exit code and therefore
    yields ``UNKNOWN``, never ``PASS``.

    Returns an empty tuple when no session could be resolved: an evidence row is
    a claim about a specific run, and ``NOT_RUN`` for a session nobody
    identified would assert something this module does not know.
    """

    resolved = trace
    if resolved is None and session_id:
        resolved = load_trace_for_session(store_root, session_id)
    if resolved is None:
        return ()

    commands = _commands_with_codes(resolved)
    label = _clean(session_id) or _clean(resolved.session_id)
    source = f"session:{label}" if label else "none"

    tests = [item for item in commands if _TEST_RE.search(item.text)]
    focused = [item for item in tests if _names_a_path(item.text)]
    full = [item for item in tests if not _names_a_path(item.text)]
    typecheck = [item for item in commands if _TYPECHECK_RE.search(item.text)]
    lint = [item for item in commands if _LINT_RE.search(item.text)]

    return (
        _evidence("Focused tests", focused, source),
        _evidence("Full suite", full, source),
        _evidence("Typecheck", typecheck, source),
        _evidence("Lint", lint, source),
        # Nothing in the repo records a migration check today; saying so beats
        # a NOT_RUN that would read as "the agent skipped it".
        EvidenceRecord(name="Migration check", status="UNKNOWN", detail="", source="none"),
    )


def link_impact_to_provenance(
    provenance: ProvenanceRecord,
    impact: tuple[ImpactSite, ...],
) -> tuple[ProvenanceRecord, tuple[ImpactSite, ...]]:
    """Cross-fill the two fields that need both the impact and provenance passes.

    The two directions do not take the same evidence, which is the whole subtlety
    here. ``inspected_by_agent=True`` is safe off reads *or* edits: an agent that
    rewrote a file plainly had it in front of it. The negative -- "the agent
    never opened this" -- is a claim about *reads*, and most hosts record none
    (risk R2). Deriving it from the edit list alone turns "this host does not log
    reads" into a finding against the agent, printed one line under
    ``Agent inspected: not recorded for this host``.

    So ``uninspected_impacted`` and ``inspected_by_agent=False`` are populated
    only when the host actually recorded a read list. Without one, and when
    provenance is ``unknown``, nothing is claimed in either direction.
    """

    if provenance.status == "unknown":
        return provenance, impact
    opened = tuple(dict.fromkeys(provenance.files_inspected + provenance.files_changed))
    if not opened or not impact:
        return provenance, impact

    site_files = tuple(dict.fromkeys(path for path in (_site_file(site.path) for site in impact) if path))
    hits = _match_paths(opened, site_files)
    reads_recorded = bool(provenance.files_inspected)
    linked = tuple(
        replace(site, inspected_by_agent=True if _site_file(site.path) in hits else (False if reads_recorded else None))
        for site in impact
    )
    if not reads_recorded:
        return provenance, linked
    uninspected = tuple(sorted(path for path in site_files if path not in hits))
    return replace(provenance, uninspected_impacted=uninspected), linked


def collect_provenance(
    store_root: Path,
    repo_root: Path,
    packet_paths: Sequence[str],
    *,
    head_sha: str,
    head_commit_time: datetime | None,
    session_id: str | None = None,
) -> tuple[ProvenanceRecord, tuple[EvidenceRecord, ...], tuple[str, ...]]:
    """Return ``(record, evidence, degraded)`` -- the packet orchestrator's hook.

    Exists so ``packet.py::_collect_provenance`` stays a one-line delegation:
    correlation and evidence share the resolved trace, and the orchestrator
    never has to hold a ``Trace`` of its own.
    """

    try:
        correlation = _correlate(
            store_root,
            repo_root,
            packet_paths,
            head_sha=head_sha,
            head_commit_time=head_commit_time,
            session_id=session_id,
        )
    except Exception:
        # The contract is total. An unexpected failure costs a degradation
        # name, never the packet.
        return _unknown("provenance correlation failed"), (), ("provenance_failed",)

    extra: tuple[str, ...] = ()
    try:
        evidence = collect_evidence(store_root, correlation.trace, correlation.record.session_id)
    except Exception:
        evidence = ()
        extra += ("evidence_failed",)

    if correlation.trace is not None and not any(
        item.exit_code is not None for item in _commands_with_codes(correlation.trace)
    ):
        # R14: the session recorded commands with no exit codes (or none at
        # all), so no evidence row can ever be stronger than UNKNOWN.
        extra += ("test_evidence_unavailable",)

    return correlation.record, evidence, tuple(sorted(set(correlation.degraded + extra)))


__all__ = [
    "collect_evidence",
    "collect_provenance",
    "link_impact_to_provenance",
    "load_trace_for_session",
    "split_files_touched",
]
