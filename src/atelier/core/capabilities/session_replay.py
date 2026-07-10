"""Counterfactual session replay: full transcript + Atelier short-circuits.

Reconstructs a *recorded* coding session from its transcript — every assistant
message, thinking block, tool call (input) and, where available, its output —
and overlays what Atelier would have done differently: the grep→read loops the
agent actually walked, marked and collapsed into the single ``code_search`` call
that would have returned the answer in one turn.

**No model is re-run.** This reads JSONL off disk (Claude / Codex / opencode via
the shared :func:`parse_session_turns`), so it is deterministic, instant, and
costs nothing. Savings are *inferred* from the loop structure (calls and turns
eliminated) and labelled as such — they are not a re-measured A/B (that stays
``atelier benchmark local``).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atelier.core.capabilities.prompt_compilation.tokens import estimate_tokens
from atelier.gateway.hosts.session_parsers._session_parser import parse_session_turns

SUPPORTED_HOSTS = ("claude", "codex", "opencode")

# Argument keys that indicate a *ranged* (targeted) read rather than a whole-file
# read. A ranged read means the agent already knew where to look, so it is not
# part of a wasteful search loop.
_RANGE_ARG_KEYS = ("offset", "limit", "line_start", "line_end", "start_line", "end_line", "range", "view_range")


# --------------------------------------------------------------------------- #
# Tool classification
# --------------------------------------------------------------------------- #


def _tool_name(turn: dict[str, Any]) -> str:
    return str(turn.get("tool_name") or "")


def _is_atelier_search(turn: dict[str, Any]) -> bool:
    n = _tool_name(turn).lower()
    return "code_search" in n or "explore" in n


# Shell commands that are really a code search (agents grep via Bash, not the
# Grep tool). A single Atelier code_search replaces these too.
_SHELL_SEARCH_RE = re.compile(
    r"(?:^|[|;&]|\bxargs\s+)\s*(?:sudo\s+)?(?:git\s+)?(grep|egrep|fgrep|rg|ag|ack)\b|\bfind\b[^|]*-(?:i?name|i?path|regex)\b"
)


def _shell_is_search(command: str) -> bool:
    return bool(_SHELL_SEARCH_RE.search(command or ""))


def _shell_search_query(command: str) -> str:
    command = command or ""
    raw = ""
    m = re.search(r"""(['\"])(.+?)\1""", command)
    if m and m.group(2).strip():
        raw = m.group(2).strip()
    else:
        m2 = re.search(r"\b(?:grep|egrep|fgrep|rg|ag|ack)\b\s+((?:-\S+\s+)*)(\S+)", command)
        if m2 and m2.group(2) and not m2.group(2).startswith("-"):
            raw = m2.group(2).strip("'\"")
    if not raw:
        return ""
    # Grep patterns are often regex alternations (a\|b\|c); take the first branch
    # and strip regex noise so it reads as a code_search query.
    first = re.split(r"\\\||\|", raw)[0].strip()
    first = first.strip("^$.*+?()[]{}\\").strip()
    return first or raw


def _is_grep(turn: dict[str, Any]) -> bool:
    if _is_atelier_search(turn):
        return False
    kind = turn.get("kind")
    if kind == "tool_call":
        n = _tool_name(turn).lower()
        return "grep" in n or "glob" in n or n == "search"
    if kind == "shell_command":
        return _shell_is_search(str(turn.get("content") or ""))
    return False


def _is_whole_file_read(turn: dict[str, Any]) -> bool:
    if turn.get("kind") != "tool_call" or _is_atelier_search(turn):
        return False
    n = _tool_name(turn).lower()
    if "read" not in n and n != "cat":
        return False
    args = turn.get("arguments") or {}
    if isinstance(args, dict) and any(k in args for k in _RANGE_ARG_KEYS):
        return False  # targeted read — not wasteful
    return True


def _is_collapsible(turn: dict[str, Any]) -> bool:
    return _is_grep(turn) or _is_whole_file_read(turn)


def _grep_query(turn: dict[str, Any]) -> str:
    if turn.get("kind") == "shell_command":
        return _shell_search_query(str(turn.get("content") or ""))
    args = turn.get("arguments") or {}
    if isinstance(args, dict):
        for key in ("pattern", "query", "content_regex", "regex", "q"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _read_path(turn: dict[str, Any]) -> str:
    args = turn.get("arguments") or {}
    if isinstance(args, dict):
        for key in ("file_path", "filePath", "path", "filename", "file"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


# --------------------------------------------------------------------------- #
# Episode detection
# --------------------------------------------------------------------------- #


@dataclass
class Episode:
    """A grep/read loop that a single ``code_search`` would collapse."""

    id: int
    turn_indices: list[int]
    grep_count: int
    read_count: int
    query: str
    after_index: int  # render the collapse card right after this turn index
    atelier: dict[str, Any] | None = None  # real code_search output (attached by live enrichment)

    @property
    def calls_saved(self) -> int:
        return max(0, len(self.turn_indices) - 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "turn_indices": list(self.turn_indices),
            "grep_count": self.grep_count,
            "read_count": self.read_count,
            "query": self.query,
            "after_index": self.after_index,
            "calls_saved": self.calls_saved,
            "atelier": self.atelier,
        }


def detect_episodes(turns: list[dict[str, Any]]) -> list[Episode]:
    """Find runs of grep/whole-file-read turns a single code_search would replace.

    Text and thinking turns are transparent (exploration narration, they do not
    break a run). A run ends at any concrete action — an edit, a shell command, a
    targeted read, a user message, or an actual code_search. A run qualifies as
    an episode when it has at least one grep and at least two collapsible calls.
    """
    episodes: list[Episode] = []
    run: list[int] = []

    def flush() -> None:
        nonlocal run
        if run:
            greps = sum(1 for i in run if _is_grep(turns[i]))
            reads = sum(1 for i in run if _is_whole_file_read(turns[i]))
            if len(run) >= 2 and greps >= 1:
                query = ""
                for i in run:
                    if _is_grep(turns[i]):
                        query = _grep_query(turns[i])
                        if query:
                            break
                if not query:
                    for i in run:
                        p = _read_path(turns[i])
                        if p:
                            query = Path(p).stem
                            break
                episodes.append(
                    Episode(
                        id=len(episodes) + 1,
                        turn_indices=list(run),
                        grep_count=greps,
                        read_count=reads,
                        query=query or "(unknown)",
                        after_index=run[-1],
                    )
                )
        run = []

    for idx, turn in enumerate(turns):
        kind = turn.get("kind")
        if kind in ("thinking", "agent_message"):
            continue  # transparent narration
        if _is_atelier_search(turn):
            flush()
        elif _is_collapsible(turn):  # grep/glob tool, shell grep/find, or whole-file read
            run.append(idx)
        else:
            flush()  # edit, non-search shell command, user message, subagent, etc.
    flush()
    return episodes


@dataclass
class Batch:
    """A run of consecutive same-kind calls Atelier would issue as one batch.

    Atelier ``read`` takes ``files=[...]`` and ``edit`` takes ``edits=[...]``, so
    N adjacent whole-file reads or N adjacent edits become a single call.
    """

    id: int
    kind: str  # "read" | "edit"
    turn_indices: list[int]
    after_index: int
    atelier: dict[str, Any] | None = None

    @property
    def calls_saved(self) -> int:
        return max(0, len(self.turn_indices) - 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "turn_indices": list(self.turn_indices),
            "after_index": self.after_index,
            "calls_saved": self.calls_saved,
            "atelier": self.atelier,
        }


def _batch_kind(turn: dict[str, Any]) -> str | None:
    kind = turn.get("kind")
    if kind == "file_edit":
        return "edit"
    if kind == "tool_call" and _is_whole_file_read(turn):
        return "read"
    return None


def detect_batches(turns: list[dict[str, Any]], exclude: set[int]) -> list[Batch]:
    """Find runs of >=2 adjacent same-kind reads/edits Atelier would batch into one.

    ``exclude`` holds turn indices already collapsed by a grep episode so a read
    is never double-counted. Text/thinking turns are transparent.
    """
    batches: list[Batch] = []
    run: list[int] = []
    run_kind: str | None = None

    def flush() -> None:
        nonlocal run, run_kind
        if len(run) >= 2 and run_kind:
            batches.append(Batch(id=len(batches) + 1, kind=run_kind, turn_indices=list(run), after_index=run[-1]))
        run = []
        run_kind = None

    for idx, turn in enumerate(turns):
        if idx in exclude:
            flush()
            continue
        kind = turn.get("kind")
        if kind in ("thinking", "agent_message"):
            continue  # transparent narration
        this = _batch_kind(turn)
        if this is None:
            flush()
            continue
        if run and run_kind != this:
            flush()
        run.append(idx)
        run_kind = this
    flush()
    return batches


# --------------------------------------------------------------------------- #
# Tool-result join (Claude transcripts carry results as user tool_result blocks)
# --------------------------------------------------------------------------- #


def _tool_results_from_content(content: str) -> dict[str, str]:
    """Map ``tool_use_id -> result text`` from a Claude JSONL transcript.

    Best-effort and format-tolerant: other hosts that do not carry tool_result
    blocks simply yield an empty map, and the renderers degrade to showing the
    call input without an output pane.
    """
    results: dict[str, str] = {}
    for line in content.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = ev.get("message") or {}
        blocks = msg.get("content")
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tuid = str(block.get("tool_use_id") or "").strip()
            if not tuid:
                continue
            results[tuid] = _result_text(block.get("content"))
    return results


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


# --------------------------------------------------------------------------- #
# Replay model
# --------------------------------------------------------------------------- #


@dataclass
class ReplaySummary:
    total_turns: int
    total_tool_calls: int
    kept_tool_calls: int
    calls_saved: int
    episode_count: int
    batch_count: int = 0
    search_calls_saved: int = 0
    batch_calls_saved: int = 0
    verbose_output_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_turns": self.total_turns,
            "total_tool_calls": self.total_tool_calls,
            "kept_tool_calls": self.kept_tool_calls,
            "calls_saved": self.calls_saved,
            "episode_count": self.episode_count,
            "batch_count": self.batch_count,
            "search_calls_saved": self.search_calls_saved,
            "batch_calls_saved": self.batch_calls_saved,
            "verbose_output_tokens": self.verbose_output_tokens,
        }


@dataclass
class Replay:
    host: str
    session_id: str
    model: str
    task: str
    turns: list[dict[str, Any]]
    collapsed_indices: list[int]
    episodes: list[Episode]
    batches: list[Batch] = field(default_factory=list)
    batched_indices: list[int] = field(default_factory=list)
    tool_results: dict[str, str] = field(default_factory=dict)
    summary: ReplaySummary | None = None
    source_path: str | None = None
    is_subagent: bool = False
    subagent_replays: list["Replay"] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "session_id": self.session_id,
            "model": self.model,
            "task": self.task,
            "turns": self.turns,
            "collapsed_indices": list(self.collapsed_indices),
            "episodes": [e.to_dict() for e in self.episodes],
            "batches": [b.to_dict() for b in self.batches],
            "batched_indices": list(self.batched_indices),
            "summary": self.summary.to_dict() if self.summary else None,
            "savings": estimate_savings(self),
            "subagents": [sr.to_dict() for sr in self.subagent_replays],
        }


def _first_text(turns: list[dict[str, Any]], kind: str) -> str:
    for turn in turns:
        if turn.get("kind") == kind:
            text = str(turn.get("content") or "").strip()
            if text:
                return text
    return ""


def _model_of(turns: list[dict[str, Any]]) -> str:
    for turn in turns:
        model = turn.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
    return ""


def build_replay(content: str, *, host: str, session_id: str) -> Replay:
    """Parse a transcript and build the annotated replay model."""
    turns = parse_session_turns(content, host)
    episodes = detect_episodes(turns)
    collapsed = sorted({i for e in episodes for i in e.turn_indices})
    batches = detect_batches(turns, set(collapsed))
    batched = sorted({i for b in batches for i in b.turn_indices})

    tool_kinds = {"tool_call", "file_edit", "shell_command"}
    total_tool_calls = sum(1 for t in turns if t.get("kind") in tool_kinds)
    search_saved = sum(e.calls_saved for e in episodes)
    batch_saved = sum(b.calls_saved for b in batches)
    calls_saved = search_saved + batch_saved
    summary = ReplaySummary(
        total_turns=len(turns),
        total_tool_calls=total_tool_calls,
        kept_tool_calls=max(0, total_tool_calls - calls_saved),
        calls_saved=calls_saved,
        episode_count=len(episodes),
        batch_count=len(batches),
        search_calls_saved=search_saved,
        batch_calls_saved=batch_saved,
        verbose_output_tokens=_verbose_output_tokens(turns),
    )
    results = _tool_results_from_content(content) if host == "claude" else {}
    return Replay(
        host=host,
        session_id=session_id,
        model=_model_of(turns),
        task=_first_text(turns, "user_message"),
        turns=turns,
        collapsed_indices=collapsed,
        episodes=episodes,
        batches=batches,
        batched_indices=batched,
        tool_results=results,
        summary=summary,
    )


def _round_usage(replay: Replay) -> tuple[list[dict[str, Any]], list[int]]:
    """Extract per-assistant-round token usage and the round indices touched by
    collapsed loop calls, for the savings estimator.

    A round begins on the turn carrying the round's usage (the parser puts usage
    on the first block of each assistant message; later blocks carry {}). This is
    pure transcript parsing -- the SAVINGS MATH lives in
    ``savings_summary.estimate_collapse_saving_fraction`` (single source).
    """
    round_tokens: list[dict[str, Any]] = []
    round_of: list[int] = []
    cur = -1
    for turn in replay.turns:
        tok = turn.get("tokens")
        if isinstance(tok, dict) and tok:
            cur += 1
            round_tokens.append(tok)
        round_of.append(cur)
    loop_rounds = sorted({round_of[i] for i in replay.collapsed_indices if 0 <= i < len(round_of) and round_of[i] >= 0})
    return round_tokens, loop_rounds


def estimate_savings(replay: Replay) -> dict[str, Any]:
    """Cost + what Atelier would cost + savings, from real sources where possible.

    ``atelier_cost_usd`` answers "what would this task cost run with Atelier?":
    - benchmark session -> the paired atelier arm's REAL cost (results.jsonl);
    - session that ran with Atelier -> its own cost;
    - otherwise -> ``cost - opportunity`` where opportunity is a conservative
      carry-based estimate (priced via the canonical estimate_cost_usd).
    All pricing/savings numbers come from ``savings_summary`` -- no local math.

    No local pricing/savings math (that drifts): everything comes from
    ``savings_summary`` — the same engine the dashboard, badges and statusline
    use.

    - ``total_cost_usd``: real baseline cost via ``read_transcript_stats.est_cost_usd``.
    - ``measured_saved_usd`` / ``measured_time_saved_seconds``: the recorded
      Atelier savings for THIS session via ``compute_savings_summary`` (its
      ``total_saved_usd`` = saved_usd + carry_usd). **0 for a session that ran
      without Atelier** (e.g. a benchmark baseline) — the real A/B saving is a
      cross-run difference, not reconstructable from one transcript.
    - ``calls_saved`` / ``collapsed_output_tokens``: the STRUCTURAL counterfactual
      this replay illustrates (grep/read loops + read/edit batches collapsed).
      Not priced — shown as "what would collapse", not a dollar figure.
    """
    total_cost = 0.0
    measured_saved = 0.0
    measured_time = 0.0
    is_atelier = False
    if replay.source_path:
        try:
            from atelier.core.capabilities.savings_summary import read_transcript_stats

            st = read_transcript_stats(replay.source_path)
            if st is not None:
                total_cost = float(st.est_cost_usd)
        except Exception:  # noqa: BLE001
            pass
    # Subagents have no own savings sidecar, so compute_savings_summary would fall
    # back to the PARENT session's total (double-counting -- the parent already
    # includes its subagents). Only attribute MEASURED savings to a top-level
    # session; a subagent falls through to the per-session estimate below.
    if not replay.is_subagent:
        try:
            from atelier.core.capabilities.savings_summary import compute_savings_summary

            summ = compute_savings_summary(replay.session_id)
            measured_saved = float(summ.total_saved_usd)
            measured_time = float(summ.time_saved_seconds)
            is_atelier = measured_saved > 0 or summ.smart_calls > 0
            if not total_cost:
                total_cost = float(summ.est_cost_usd)
        except Exception:  # noqa: BLE001
            pass

    model = replay.model or "claude-sonnet-4-5"
    tr = replay.tool_results
    collapsed_chars = sum(
        len(tr.get(str(replay.turns[i].get("tool_use_id") or ""), ""))
        for i in replay.collapsed_indices
        if 0 <= i < len(replay.turns)
    )
    for turn in replay.turns:
        a = turn.get("atelier")
        if isinstance(a, dict) and a.get("tool") == "bash" and a.get("mode") == "simulated":
            collapsed_chars += int(a.get("chars_omitted", 0) or 0)
    calls_saved = replay.summary.calls_saved if replay.summary else 0

    # Did this session RUN with Atelier (used code_search / an mcp__atelier tool)?
    ran_with_atelier = any(
        "atelier" in str(t.get("tool_name") or "").lower() or "code_search" in str(t.get("tool_name") or "").lower()
        for t in replay.turns
    )

    # --- What would this task cost run with Atelier? --------------------- #
    if is_atelier:
        # Ran with Atelier AND has its own recorded savings -> show what it SAVED.
        atelier_cost = total_cost
        saved = measured_saved
        baseline_ref = total_cost + saved  # what it would have cost without Atelier
        time_saved = measured_time
        atelier_measured = True
        saved_measured = True
    elif ran_with_atelier:
        # Ran with Atelier but has no per-node recorded savings -- e.g. a subagent,
        # whose savings are billed to the PARENT session. Estimating "what Atelier
        # would save" is meaningless here (it already used Atelier), so show
        # neither an estimate nor a fake 0 -- the render surfaces "ran with Atelier".
        atelier_cost = total_cost
        saved = 0.0
        baseline_ref = total_cost
        time_saved = 0.0
        atelier_measured = True
        saved_measured = False
    else:
        # Vanilla session -- the ONLY thing a real user has. Estimate the saving
        # from THIS session alone via the canonical savings engine: what fraction
        # of the cost collapsing the grep/read loops would save (removed round-trips
        # + leaner surviving context). Applied to the canonical est_cost so 'Cost'
        # stays consistent with the dashboard/session-stats surfaces.
        round_tokens, loop_rounds = _round_usage(replay)
        fraction = 0.0
        try:
            from atelier.core.capabilities.savings_summary import estimate_collapse_saving_fraction

            fraction = estimate_collapse_saving_fraction(round_tokens, loop_rounds, model)
        except Exception:  # noqa: BLE001
            fraction = 0.0
        saved = total_cost * fraction
        atelier_cost = max(0.0, total_cost - saved)
        baseline_ref = total_cost
        try:
            from atelier.core.capabilities.savings_summary import estimate_time_saved_seconds

            time_saved = estimate_time_saved_seconds(calls_avoided=calls_saved)
        except Exception:  # noqa: BLE001
            time_saved = float(calls_saved) * 4.5
        atelier_measured = False
        saved_measured = False

    # Saving is always a fraction of the WITHOUT-Atelier (baseline) cost.
    saved_pct = round(100.0 * saved / baseline_ref, 1) if baseline_ref > 0 else 0.0
    return {
        "model": model,
        "total_cost_usd": round(total_cost, 4),
        "atelier_cost_usd": round(atelier_cost, 4),
        "atelier_cost_is_measured": atelier_measured,
        "saved_usd": round(saved, 4),
        "saved_pct": saved_pct,
        "saved_is_measured": saved_measured,
        "time_saved_seconds": round(time_saved, 1),
        "is_atelier_session": is_atelier,
        "ran_with_atelier": ran_with_atelier,
        "calls_saved": calls_saved,
        "collapsed_output_tokens": collapsed_chars // 4,
    }


def _verbose_output_tokens(turns: list[dict[str, Any]]) -> int:
    return sum(estimate_tokens(str(t.get("content") or "")) for t in turns if t.get("kind") == "agent_message")


# --------------------------------------------------------------------------- #
# Transcript discovery
# --------------------------------------------------------------------------- #


def _opencode_roots() -> list[Path]:
    roots: list[Path] = []
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        roots.append(Path(xdg) / "opencode")
    roots.append(Path.home() / ".local" / "share" / "opencode")
    roots.append(Path.home() / ".opencode")
    return [r for r in roots if r.is_dir()]


def _codex_root() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "sessions"


def locate_transcript(host: str, session_id: str) -> Path | None:
    """Find the transcript file for ``session_id`` under ``host``'s store."""
    session_id = session_id.strip()
    if not session_id:
        return None
    if host == "claude":
        from atelier.core.capabilities.savings_summary import claude_transcript_candidates

        candidates = claude_transcript_candidates(session_id)
        return candidates[0] if candidates else None
    if host == "codex":
        root = _codex_root()
        if not root.is_dir():
            return None
        return _match_by_id(sorted(root.rglob("*.jsonl")), session_id)
    if host == "opencode":
        for root in _opencode_roots():
            hit = _match_by_id(sorted(root.rglob("*.jsonl")), session_id)
            if hit:
                return hit
        return None
    return None


def _match_by_id(paths: list[Path], session_id: str) -> Path | None:
    # Prefer a filename match; fall back to a content scan of the head.
    for path in paths:
        if session_id in path.stem:
            return path
    for path in paths:
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                head = fh.read(4096)
        except OSError:
            continue
        if session_id in head:
            return path
    return None


def recent_transcripts(host: str, limit: int) -> list[Path]:
    """Return the ``limit`` most recently modified transcripts for ``host``."""
    if host == "claude":
        claude_root = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_HOME") or ""
        projects = Path(claude_root) / "projects" if claude_root else Path.home() / ".claude" / "projects"
        paths = list(projects.rglob("*.jsonl")) if projects.is_dir() else []
    elif host == "codex":
        root = _codex_root()
        paths = list(root.rglob("*.jsonl")) if root.is_dir() else []
    elif host == "opencode":
        paths = [p for root in _opencode_roots() for p in root.rglob("*.jsonl")]
    else:
        paths = []
    paths = [p for p in paths if p.is_file() and "subagents" not in p.parts]
    return sorted(paths, key=lambda p: p.stat().st_mtime, reverse=True)[: max(1, limit)]


def _session_id_from_path(path: Path) -> str:
    return path.stem


def load_replays(
    *,
    host: str,
    session_id: str | None = None,
    file: Path | None = None,
    last: int = 1,
) -> list[Replay]:
    """Load and build replays from an explicit file, a session id, or recents."""
    sources: list[tuple[str, Path]] = []
    if file is not None:
        sources.append((session_id or _session_id_from_path(file), file))
    elif session_id:
        hit = locate_transcript(host, session_id)
        if hit is not None:
            sources.append((session_id, hit))
    else:
        sources.extend((_session_id_from_path(p), p) for p in recent_transcripts(host, last))

    replays: list[Replay] = []
    for sid, path in sources:
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        replay = build_replay(content, host=host, session_id=sid)
        replay.source_path = str(path)
        replay.subagent_replays = _load_subagents(path, host)
        replays.append(replay)
    return replays


def _load_subagents(path: Path, host: str) -> list[Replay]:
    """Build a nested replay for each subagent (sidechain) transcript, if any."""
    if host != "claude":
        return []
    try:
        from atelier.core.capabilities.savings_summary import _subagent_transcripts

        subpaths = _subagent_transcripts(path)
    except Exception:  # noqa: BLE001
        return []
    out: list[Replay] = []
    for sub in subpaths:
        try:
            sr = build_replay(sub.read_text(encoding="utf-8", errors="replace"), host=host, session_id=sub.stem)
            sr.source_path = str(sub)
            sr.is_subagent = True
            out.append(sr)
        except OSError:
            continue
    return out
