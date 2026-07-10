"""Unified savings/cost computation for all hooks and host integrations.

Single source of truth for:
- Claude transcript discovery and per-model cost parsing
- Session savings aggregation (live events + session_stats)
- savings --segment output formatting (consumed by statusline.sh via ``atelier savings --segment``)

Previously this logic was spread across:
- integrations/claude/plugin/scripts/statusline.sh (inline Python heredoc)
- integrations/claude/plugin/hooks/stop.py (_read_transcript_stats, _estimate_cost_usd, etc.)
- plugin_runtime.py (load_live_savings_summary)
"""

import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Model helpers
# ---------------------------------------------------------------------------

# Map display names (as returned by Claude Code's context_window.model.display_name)
# to canonical model IDs (as used by the Anthropic API / LiteLLM catalog).
_DISPLAY_NAME_MODEL_MAP: dict[str, str] = {
    "fable 5": "claude-fable-5",
    "mythos 5": "claude-mythos-5",
    "mythos preview": "claude-mythos-preview",
    "sonnet 5": "claude-sonnet-5",
    "opus 4.8": "claude-opus-4-8",
    "opus 4.7": "claude-opus-4-7",
    "opus 4.6": "claude-opus-4-6",
    "opus 4.5": "claude-opus-4-5",
    "opus 4.1": "claude-opus-4-1",
    "opus 4": "claude-opus-4-0",
    "sonnet 4.7": "claude-sonnet-4-7",
    "sonnet 4.6": "claude-sonnet-4-6",
    "sonnet 4": "claude-sonnet-4-0",
    "haiku 4.7": "claude-haiku-4-7",
    "haiku 4.6": "claude-haiku-4-6",
    "haiku 4.5": "claude-haiku-4-5",
}


def is_real_model(raw: object) -> bool:
    """Return True when *raw* is a genuine model identifier (not a placeholder)."""
    if not isinstance(raw, str):
        return False
    candidate = raw.strip()
    return bool(candidate and not candidate.startswith("<") and candidate not in {"_default", "unknown", "none"})


def resolve_model_id(raw: str | None) -> str:
    """Map a display name (``"Opus 4.7"``) to a canonical model id when possible.

    Falls back to returning *raw* unchanged when it already looks canonical
    (e.g. ``"claude-opus-4-7"``).
    """
    if not raw:
        return ""
    key = raw.strip().lower()
    # Strip a trailing descriptor like " (1m context)" so display variants
    # ("Opus 4.8 (1M context)") still resolve to the canonical id.
    key = re.sub(r"\s*\([^)]*\)\s*$", "", key).strip()
    if key in _DISPLAY_NAME_MODEL_MAP:
        return _DISPLAY_NAME_MODEL_MAP[key]
    return raw.strip()


def estimate_cost_usd(
    *,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
    cache_write_1h_tokens: int = 0,
    long_context: bool = False,
) -> float:
    """Estimate cost using the per-model rate card.

    ``cache_write_tokens`` is the 5m-TTL portion when ``cache_write_1h_tokens``
    is supplied (1h writes bill at a higher rate). ``long_context=True`` prices
    the bucket at the model's >200k per-request premium rates.

    Falls back to Sonnet 4.6 rates when the model is unknown so we never
    silently show $0 for an active session.
    """
    try:
        from atelier.core.capabilities.pricing import get_model_pricing

        pricing = get_model_pricing(model_id) if model_id else None
        if pricing is None or not pricing.known or pricing.input <= 0:
            pricing = get_model_pricing("claude-sonnet-4-5")
        return pricing.request_cost_usd(
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            cache_read_tokens=int(cache_read_tokens or 0),
            cache_write_tokens=int(cache_write_tokens or 0),
            cache_write_1h_tokens=int(cache_write_1h_tokens or 0),
            long_context=long_context,
        )
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return ((input_tokens or 0) * 3 + (output_tokens or 0) * 15) / 1_000_000


# ---------------------------------------------------------------------------
# Claude transcript helpers
# ---------------------------------------------------------------------------


def claude_transcript_candidates(session_id: str) -> list[Path]:
    """Return all Claude transcript JSONL paths for *session_id*, newest first.

    Searches:
    - ``$CLAUDE_CONFIG_DIR/projects/*/<session_id>.jsonl``
    - ``$CLAUDE_CONFIG_DIR/projects/*/*/subagents/<session_id>.jsonl``
    - Falls back to ``~/.claude/projects/...``
    """
    session_id = session_id.strip()
    if not session_id:
        return []
    claude_root = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_HOME") or ""
    projects = Path(claude_root) / "projects" if claude_root else Path.home() / ".claude" / "projects"
    if not projects.is_dir():
        return []
    paths: list[Path] = []
    try:
        paths.extend(projects.glob(f"*/{session_id}.jsonl"))
        paths.extend(projects.glob(f"*/*/subagents/{session_id}.jsonl"))
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return []
    return sorted((p for p in paths if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)


_CTX_TAIL_BYTES = 65536


def transcript_context_state(session_id: str) -> tuple[int, str]:
    """Return (live context tokens, model) for a Claude session.

    Context = the most recent assistant turn's input + cache reads + cache
    writes — i.e. what the next turn will re-read. Tail-reads the newest
    transcript so it is cheap enough to call from per-tool-call hooks.
    Returns ``(0, "")`` when the session or usage cannot be located.
    """
    candidates = claude_transcript_candidates(session_id)
    if not candidates:
        return 0, ""
    newest = candidates[0]
    try:
        with newest.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - _CTX_TAIL_BYTES))
            lines = fh.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return 0, ""
    for raw in reversed(lines):
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            continue  # first tail line may be partial
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message") or {}
        usage = msg.get("usage") if isinstance(msg, dict) else None
        if not isinstance(usage, dict):
            continue
        ctx = (
            int(usage.get("input_tokens", 0) or 0)
            + int(usage.get("cache_read_input_tokens", 0) or 0)
            + int(usage.get("cache_creation_input_tokens", 0) or 0)
        )
        if ctx <= 0:
            continue
        model = str(msg.get("model") or "").strip()
        return ctx, model
    return 0, ""


@dataclass
class TranscriptStats:
    """Parsed statistics from a Claude transcript JSONL file."""

    tool_calls: int = 0
    # Distinct assistant turns (one per assistant message id with usage).
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    est_cost_usd: float = 0.0
    model: str = ""
    models_used: list[str] = field(default_factory=list)
    tools_used: dict[str, int] = field(default_factory=dict)
    # Per-model token buckets: {model_id: {in, out, cR, cW}} for weighted pricing.
    per_model: dict[str, dict[str, int]] = field(default_factory=dict)
    # Last model seen in transcript (most recent turn). Differs from `model`
    # (first seen) for resumed sessions where user switched models mid-session.
    last_model: str = ""
    # ISO timestamps of assistant turns with usage — drives the carry credit.
    turn_timestamps: list[str] = field(default_factory=list)
    # Per-subagent assistant-turn timestamps (one inner list per subagent
    # transcript). Drives per-window carry: a token a subagent saved carries
    # across that subagent's own later turns, not the main thread's.
    subagent_turn_timestamps: list[list[str]] = field(default_factory=list)
    # Per-main-turn request usage rows ({ts, model, in, out, cR, cW}) — drives
    # the long-context cliff-avoidance credit (:func:`_cliff_credit`).
    turn_usage: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_write_tokens

    def savings_input_rate(self) -> float | None:
        """Weighted $/input-token rate across all models used in this session.

        Saved tokens are context tokens NOT sent to the model — they would have
        been charged as NEW INPUT tokens.  We weight each model's input rate by
        the number of input tokens it actually processed.
        """
        from atelier.core.capabilities.pricing import get_model_pricing

        if not self.per_model:
            return None
        total_input = sum(b.get("in", 0) for b in self.per_model.values())
        if total_input <= 0:
            for m in self.per_model:
                p = get_model_pricing(m)
                if p and p.known and p.input > 0:
                    return p.input / 1_000_000
            return None
        weighted = 0.0
        for m, b in self.per_model.items():
            p = get_model_pricing(m)
            if p and p.known and p.input > 0:
                weighted += p.input / 1_000_000 * b.get("in", 0)
        return weighted / total_input if weighted > 0 else None


# --- stop-hook savings block embedded in the transcript -------------------
# The stop hook writes its session summary into the conversation, so the
# numbers persist inside the session file itself. The middle dot appears
# either raw (·) or JSON-escaped (·) depending on nesting depth.
_STOP_SEP = r"(?:\\u00b7|·)"
_STOP_EST_COST_RE = re.compile(r"est\. cost: ~\$([0-9][0-9.,]*)")
_STOP_SAVINGS_RE = re.compile(
    rf"savings: \$([0-9][0-9.,]*) {_STOP_SEP} ([0-9,]+) tokens saved {_STOP_SEP} ([0-9,]+) calls avoided"
)
_STOP_CARRY_RE = re.compile(
    rf"context carry: \$([0-9][0-9.,]*)"
    rf"(?:{_STOP_SEP} ([0-9,]+) tokens)?"  # token count optional in older hook format
)
# Older format: carry embedded inline in the savings line as "· incl. context carry $X"
_STOP_CARRY_INLINE_RE = re.compile(r"incl\. context carry \$([0-9][0-9.,]*)")
_STOP_CALLS_RE = re.compile(rf"([0-9,]+) turns {_STOP_SEP} ([0-9,]+) tool calls")


@dataclass
class TranscriptSavingsBlock:
    """Savings summary recovered from a stop-hook block inside a transcript."""

    est_cost_usd: float = 0.0
    saved_usd: float = 0.0
    saved_tokens: int = 0
    calls_avoided: int = 0
    carry_usd: float = 0.0
    carry_tokens: int = 0
    # Main-transcript counters from the same block; consumers can cross-check
    # these against trace-derived numbers to catch import regressions.
    turns: int = 0
    tool_calls: int = 0


def read_transcript_savings_block(transcript_path: str | Path) -> TranscriptSavingsBlock | None:
    """Parse the LAST stop-hook savings block embedded in a transcript JSONL.

    Only hook attachment entries (``type: "attachment"`` with attachment type
    ``hook_system_message`` / ``hook_success``) are considered — never free
    conversation text, which may quote savings blocks from other sessions.
    This recovers savings, context carry, and the estimated cost from the
    session file alone — no Atelier-local sidecars or run ledger required —
    so it also works on session files copied from another machine.
    Returns ``None`` when no block is present (session never displayed one).
    """
    p = Path(transcript_path)
    last_text = ""
    try:
        with p.open(encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if "savings:" not in raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "attachment":
                    continue
                attachment = entry.get("attachment") or {}
                if not isinstance(attachment, dict):
                    continue
                if attachment.get("type") not in {"hook_system_message", "hook_success"}:
                    continue
                text = attachment.get("content") or attachment.get("stdout") or ""
                if isinstance(text, str) and _STOP_SAVINGS_RE.search(text):
                    last_text = text
    except OSError:
        return None
    if not last_text:
        return None

    def _usd(raw: str) -> float:
        return float(raw.replace(",", ""))

    def _num(raw: str) -> int:
        return int(raw.replace(",", ""))

    block = TranscriptSavingsBlock()
    savings = _STOP_SAVINGS_RE.search(last_text)
    if savings:
        block.saved_usd = _usd(savings.group(1))
        block.saved_tokens = _num(savings.group(2))
        block.calls_avoided = _num(savings.group(3))
    carry = _STOP_CARRY_RE.search(last_text)
    if carry:
        block.carry_usd = _usd(carry.group(1))
        block.carry_tokens = _num(carry.group(2)) if carry.group(2) else 0
    elif carry_inline := _STOP_CARRY_INLINE_RE.search(last_text):
        # Older format: carry was part of savings line, no token count available
        block.carry_usd = _usd(carry_inline.group(1))
    cost = _STOP_EST_COST_RE.search(last_text)
    if cost:
        block.est_cost_usd = _usd(cost.group(1))
    calls = _STOP_CALLS_RE.search(last_text)
    if calls:
        block.turns = _num(calls.group(1))
        block.tool_calls = _num(calls.group(2))
    return block


def _subagent_transcripts(transcript_path: Path) -> list[Path]:
    """Return subagent (sidechain) transcripts recorded for a session.

    Claude Code stores Agent-tool transcripts under
    ``<project>/<session-id>/subagents/*.jsonl`` next to the main
    ``<session-id>.jsonl``. Their usage is billed to the session (and is
    included in Claude's own ``cost.total_cost_usd``), so pricing must
    include them.
    """
    subagent_dir = transcript_path.parent / transcript_path.stem / "subagents"
    if not subagent_dir.is_dir():
        return []
    return sorted(subagent_dir.glob("*.jsonl"))


def _long_context_threshold(model: str, cache: dict[str, int]) -> int:
    """Per-request long-context threshold for *model* (0 = no premium), cached."""
    if model not in cache:
        try:
            from atelier.core.capabilities.pricing import get_model_pricing

            cache[model] = get_model_pricing(resolve_model_id(model)).long_context_threshold()
        except Exception:
            logging.exception("Recovered from broad exception handler")
            cache[model] = 0
    return cache[model]


def _bucket_cost_usd(model_id: str, b: dict[str, int]) -> float:
    """Price one per-model bucket: base portion + >200k premium portion.

    ``in``/``out``/``cR``/``cW`` are totals; ``*_lc`` keys hold the subset from
    messages over the long-context threshold; ``cW1`` is the 1h-TTL cache-write
    subset of ``cW``.
    """
    lc = {k: b.get(f"{k}_lc", 0) for k in ("in", "out", "cR", "cW", "cW1")}
    cw1 = b.get("cW1", 0)
    cost = estimate_cost_usd(
        model_id=model_id,
        input_tokens=b["in"] - lc["in"],
        output_tokens=b["out"] - lc["out"],
        cache_read_tokens=b["cR"] - lc["cR"],
        cache_write_tokens=(b["cW"] - cw1) - (lc["cW"] - lc["cW1"]),
        cache_write_1h_tokens=cw1 - lc["cW1"],
    )
    if any(lc.values()):
        cost += estimate_cost_usd(
            model_id=model_id,
            input_tokens=lc["in"],
            output_tokens=lc["out"],
            cache_read_tokens=lc["cR"],
            cache_write_tokens=lc["cW"] - lc["cW1"],
            cache_write_1h_tokens=lc["cW1"],
            long_context=True,
        )
    return cost


# Cursor cache for read_transcript_stats: transcripts are append-only JSONL,
# so instead of re-parsing the whole file on every call (O(session) per turn,
# O(session²) over a session's life once transcripts reach tens of MB) we keep
# a per-source byte offset plus the running fold state and parse only appended
# lines. A source that shrinks (rewrite/truncation) forces a full rebuild.
_transcript_stats_cache: dict[str, dict[str, Any]] = {}  # main path → {cursors, fold, stats}
_TRANSCRIPT_CACHE_MAX = 8  # main transcripts tracked per process


class _TranscriptFold:
    """Running accumulator for one transcript + its subagent transcripts.

    Holds every counter and dedup structure the single-pass parse builds so
    parsing can resume from a byte offset instead of restarting. The per-line
    logic mirrors the pre-incremental ``read_transcript_stats`` exactly.
    """

    def __init__(self) -> None:
        self.tool_calls = 0
        self.turns = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.tools_used: dict[str, int] = {}
        self.model_id = ""
        self.last_model_id = ""  # most recently seen model (resumed sessions)
        self.per_model: dict[str, dict[str, int]] = {}
        self.turn_timestamps: list[str] = []
        # Per-main-turn request usage — drives the long-context cliff credit.
        self.turn_usage: list[dict[str, Any]] = []
        # Per-subagent turn timestamps keyed by subagent transcript path so an
        # incremental append lands in the right bucket.
        self.sub_ts: dict[str, list[str]] = {}
        self.seen_usage_message_ids: set[str] = set()
        self.seen_tool_use_ids: set[str] = set()
        self.lc_thresholds: dict[str, int] = {}

    def fold_line(self, raw: str, *, is_main: bool, source_key: str) -> None:
        try:
            entry = json.loads(raw)
        except json.JSONDecodeError:
            return
        if not isinstance(entry, dict):
            return
        msg = entry.get("message") or {}
        if not isinstance(msg, dict):
            return
        msg_id = str(msg.get("id") or "").strip()

        candidate = msg.get("model") or entry.get("model") or ""
        if is_main and is_real_model(candidate):
            candidate_str = str(candidate).strip()
            if not self.model_id:
                self.model_id = candidate_str
            self.last_model_id = candidate_str

        usage = msg.get("usage") or {}
        if not isinstance(usage, dict):
            return
        in_t = int(usage.get("input_tokens", 0) or 0)
        out_t = int(usage.get("output_tokens", 0) or 0)
        cr_t = int(usage.get("cache_read_input_tokens", 0) or 0)
        cw_t = int(usage.get("cache_creation_input_tokens", 0) or 0)
        cache_creation = usage.get("cache_creation") or {}
        cw1_t = int(cache_creation.get("ephemeral_1h_input_tokens", 0) or 0) if isinstance(cache_creation, dict) else 0
        cw1_t = min(cw1_t, cw_t)
        has_usage = bool(in_t or out_t or cr_t or cw_t)
        count_usage = has_usage
        if has_usage and msg_id:
            if msg_id in self.seen_usage_message_ids:
                count_usage = False
            else:
                self.seen_usage_message_ids.add(msg_id)
        if count_usage:
            self.input_tokens += in_t
            self.output_tokens += out_t
            self.cache_read_tokens += cr_t
            self.cache_write_tokens += cw_t
            # A turn = one assistant message with non-zero usage.
            # Dedup on msg_id (same dedup as token accumulation).
            ts_raw = str(entry.get("timestamp") or "")
            if is_main:
                self.turns += 1
                if ts_raw:
                    self.turn_timestamps.append(ts_raw)
            elif ts_raw:
                # Subagent assistant turn — bucketed per subagent so carry
                # credit attributes a subagent-saved token to that subagent's
                # own context window, not the main thread's.
                self.sub_ts.setdefault(source_key, []).append(ts_raw)

            turn_model = str(msg.get("model") or entry.get("model") or "").strip()
            if is_main and ts_raw and is_real_model(turn_model):
                # Per-turn request usage — lets _cliff_credit reprice turns
                # that stayed under the >200k threshold only thanks to savings.
                self.turn_usage.append(
                    {"ts": ts_raw, "model": turn_model, "in": in_t, "out": out_t, "cR": cr_t, "cW": cw_t}
                )
            if is_real_model(turn_model):
                bucket = self.per_model.setdefault(
                    turn_model,
                    {"in": 0, "out": 0, "cR": 0, "cW": 0, "cW1": 0}
                    | {f"{k}_lc": 0 for k in ("in", "out", "cR", "cW", "cW1")},
                )
                bucket["in"] += in_t
                bucket["out"] += out_t
                bucket["cR"] += cr_t
                bucket["cW"] += cw_t
                bucket["cW1"] += cw1_t
                # Per-request long-context premium: the whole message bills at
                # premium rates once its context crosses the model's threshold
                # (e.g. 200k).
                threshold = _long_context_threshold(turn_model, self.lc_thresholds)
                if threshold and (in_t + cr_t + cw_t) > threshold:
                    bucket["in_lc"] += in_t
                    bucket["out_lc"] += out_t
                    bucket["cR_lc"] += cr_t
                    bucket["cW_lc"] += cw_t
                    bucket["cW1_lc"] += cw1_t

        if not is_main:
            return
        for index, block in enumerate(msg.get("content") or []):
            if not isinstance(block, dict):
                continue
            if block.get("type") != "tool_use":
                continue
            name = block.get("name") or "unknown"
            tool_use_id = str(block.get("id") or "").strip()
            tool_key = tool_use_id or (f"{msg_id}:{index}:{name}" if msg_id else "")
            if tool_key:
                if tool_key in self.seen_tool_use_ids:
                    continue
                self.seen_tool_use_ids.add(tool_key)
            self.tools_used[name] = self.tools_used.get(name, 0) + 1
            self.tool_calls += 1

    def finalize(self) -> "TranscriptStats":
        resolved_model = resolve_model_id(self.model_id)
        resolved_last_model = resolve_model_id(self.last_model_id) if self.last_model_id else resolved_model
        if self.per_model:
            est_cost_usd = sum(_bucket_cost_usd(resolve_model_id(m), b) for m, b in self.per_model.items())
        else:
            est_cost_usd = estimate_cost_usd(
                model_id=resolved_model,
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                cache_read_tokens=self.cache_read_tokens,
                cache_write_tokens=self.cache_write_tokens,
            )
        # Copies for the containers the fold keeps mutating, so a stats object
        # returned now never changes under a caller on a later fold.
        return TranscriptStats(
            tool_calls=self.tool_calls,
            turns=self.turns,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            est_cost_usd=est_cost_usd,
            model=resolved_model,
            last_model=resolved_last_model,
            models_used=(
                sorted(resolve_model_id(m) for m in self.per_model)
                if self.per_model
                else ([resolved_model] if resolved_model else [])
            ),
            tools_used=dict(self.tools_used),
            per_model={resolve_model_id(m): dict(b) for m, b in self.per_model.items()},
            turn_timestamps=list(self.turn_timestamps),
            subagent_turn_timestamps=[list(ts) for ts in self.sub_ts.values() if ts],
            turn_usage=[dict(u) for u in self.turn_usage],
        )


def read_transcript_stats(transcript_path: str | Path) -> "TranscriptStats | None":
    """Parse a Claude transcript JSONL and return session stats.

    Cost is computed per model per turn because users can switch models
    mid-conversation (e.g. Opus → Sonnet).  Each token bucket is priced with
    its own rate card and summed.

    Token buckets and cost also include the session's subagent transcripts
    (``<session-id>/subagents/*.jsonl``) — their usage is billed to the
    session. Turn count, tool counts, and the session model fields remain
    main-transcript-only.

    Incremental: transcripts are append-only, so only bytes past each source's
    consumed offset are parsed (a partially written tail line is left for the
    next call). A shrunken source forces a full rebuild.
    """
    p = Path(transcript_path)
    if not p.exists():
        return None
    key = str(p)
    sources: list[tuple[Path, bool]] = [(p, True)]
    sources.extend((sub, False) for sub in _subagent_transcripts(p))
    sizes: dict[str, int] = {}
    for source, _ in sources:
        try:
            sizes[str(source)] = source.stat().st_size
        except OSError:
            sizes[str(source)] = 0

    entry = _transcript_stats_cache.get(key)
    if entry is not None and any(sizes.get(k, 0) < off for k, off in entry["cursors"].items()):
        entry = None  # a source shrank (rewrite): fold state is invalid
    if (
        entry is not None
        and entry.get("stats") is not None
        and all(sizes[str(s)] <= entry["cursors"].get(str(s), 0) for s, _ in sources)
    ):
        return entry["stats"]  # type: ignore[no-any-return]
    if entry is None:
        entry = {"cursors": {}, "fold": _TranscriptFold(), "stats": None}

    fold: _TranscriptFold = entry["fold"]
    cursors: dict[str, int] = entry["cursors"]
    for source, is_main in sources:
        skey = str(source)
        offset = cursors.get(skey, 0)
        if sizes.get(skey, 0) <= offset:
            continue
        try:
            with source.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read()
        except OSError:
            continue
        # Only consume complete lines; a partially written tail line stays
        # unconsumed (the cursor stops at the last newline) for the next call.
        last_nl = chunk.rfind(b"\n")
        if last_nl < 0:
            continue
        cursors[skey] = offset + last_nl + 1
        for raw in chunk[: last_nl + 1].decode("utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                fold.fold_line(raw, is_main=is_main, source_key=skey)
            except Exception:
                logging.exception("Recovered from broad exception handler")
                continue

    stats = fold.finalize()
    entry["stats"] = stats
    _transcript_stats_cache[key] = entry
    while len(_transcript_stats_cache) > _TRANSCRIPT_CACHE_MAX:
        _transcript_stats_cache.pop(next(iter(_transcript_stats_cache)))
    return stats


# ---------------------------------------------------------------------------
# Savings aggregation
# ---------------------------------------------------------------------------


# --- Estimated time saved ("faster") --------------------------------------
# Atelier's speed win is fewer round-trips: every tool call it avoids removes a
# full model round-trip (emit the call, wait on the tool, re-ingest the result),
# and every line of prose it does not emit is generation time not spent. There is
# no live baseline to diff wall-clock against, so time saved is ESTIMATED from
# the same avoided quantities the cost-savings engine already tracks, via two
# constants calibrated against SWE-bench Verified (250 baseline vs 250 Atelier
# runs: ~2,533 tool calls avoided coincided with ~3.4h less wall-clock, i.e.
# ~4.8s per avoided call -- rounded down to 4.5s to stay conservative).
_SECONDS_PER_AVOIDED_CALL = 4.5
_GENERATION_TOKENS_PER_SECOND = 50.0


def estimate_time_saved_seconds(*, calls_avoided: int, output_saved_tokens: int = 0) -> float:
    """Estimated wall-clock seconds saved: avoided round-trips + unemitted prose.

    Conservative and monotonic -- proportional to the tool calls Atelier avoided,
    plus a small credit for output tokens it did not have to generate. Never
    negative. Single source of truth for every "faster" surface.
    """
    calls = max(0, int(calls_avoided or 0))
    out = max(0, int(output_saved_tokens or 0))
    return calls * _SECONDS_PER_AVOIDED_CALL + out / _GENERATION_TOKENS_PER_SECOND


def fmt_duration(seconds: float) -> str:
    """Compact human duration: ``45s`` / ``12m`` / ``1.5h`` / ``128h``."""
    s = max(0.0, float(seconds or 0.0))
    if s < 60:
        return f"{int(s)}s"
    if s < 3600:
        return f"{round(s / 60)}m"
    hours = s / 3600.0
    if hours < 10:
        return f"{hours:.1f}h"
    return f"{round(hours)}h"


@dataclass
class SavingsSummary:
    saved_usd: float = 0.0
    ctx_saved: int = 0
    smart_calls: int = 0
    carry_tokens: int = 0  # saved tokens x later turns (context-carry volume)
    # Context-carry credit: cache-read rate on later turns that re-read tokens
    # already saved once, plus the long-context cliff-avoidance credit. This is
    # a RECURRING credit across future turns, not a one-time savings bucket, so
    # — unlike output/routing/read below — it is deliberately never folded into
    # saved_usd (surfaces that want a "Total saved" figure add carry_usd to
    # saved_usd explicitly; see savings_frames).
    carry_usd: float = 0.0
    # Model-routing savings (cheaper-tier model choice). Tracked separately AND
    # included in saved_usd (folded in by compute_savings_summary), same as
    # output_saved_usd/read_saved_usd below.
    routing_saved_usd: float = 0.0
    # Telegraphic output-style breakdown (kind=="output_style" rows): prose the
    # model did NOT emit. Tracked separately AND included in saved_usd/ctx_saved;
    # surfaced separately so the statusline/stop hook can label output
    # reductions with ↓O.
    output_saved_usd: float = 0.0
    output_saved_tokens: int = 0
    # Per-turn context-style breakdown (kind=="input_style" rows): cache-read
    # tokens NOT re-sent because atelier's context stays leaner turn over turn
    # (orthogonal to turn_cut, which credits whole avoided turns). Tracked
    # separately AND included in saved_usd/ctx_saved; surfaced separately so
    # the statusline/stop hook can label input reductions with ↓In.
    input_saved_usd: float = 0.0
    input_saved_tokens: int = 0
    # Read/retrieval-side savings (search_read, cached_read, scoped_recall
    # levers). Tracked separately AND included in saved_usd/ctx_saved; surfaced
    # separately so surfaces can label a Read component.
    read_saved_usd: float = 0.0
    read_saved_tokens: int = 0
    est_cost_usd: float = 0.0  # baseline cost from terminated session transcript
    total_tokens: int = 0  # cumulative session tokens (in+out+cR+cW) from transcript
    display_input_tokens: int = 0  # cumulative fresh input = input + cache_write
    display_cache_tokens: int = 0  # cumulative cache reads
    display_output_tokens: int = 0  # cumulative output
    status_text: str = ""
    saved_pct: float = 0.0
    carry_pct: float = 0.0
    # N4 — per-tool exact in/out token ledger (additive; not part of the
    # statusline segment fields). Keyed by tool name -> {calls, input_tokens,
    # output_tokens}.
    tool_token_ledger: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def time_saved_seconds(self) -> float:
        """Estimated wall-clock time saved this session (avoided round-trips +
        unemitted prose). Derived, so it always tracks smart_calls/output."""
        return estimate_time_saved_seconds(
            calls_avoided=self.smart_calls,
            output_saved_tokens=self.output_saved_tokens,
        )

    tool_ledger_input_tokens: int = 0
    tool_ledger_output_tokens: int = 0

    @property
    def total_saved_usd(self) -> float:
        """Canonical "Total saved" figure: saved_usd (which already folds in
        read/output/routing) plus carry_usd (deliberately excluded from
        saved_usd itself — see the carry_usd field comment). Every surface
        that wants a single headline total should read this property instead
        of re-deriving the sum."""
        return self.saved_usd + self.carry_usd


def _price_savings_row(ev: dict[str, Any]) -> tuple[int, float, int, float, int]:
    """Price ONE ``savings.jsonl`` row — the single rule every surface shares.

    Returns ``(priced_tokens, priced_usd, calls, calls_usd, unpriced_tokens)``.

    The statusline, stop hook, ``atelier savings`` CLI, dashboard, and web
    Savings page all run rows through this one function so their realized-savings
    numbers agree.  The rule mirrors the long-standing live/statusline pricing:

    * ``calls`` and the avoided-call credit are counted for every row.  The
      credit was priced at write time (stored as ``calls_usd`` / the older
      ``calls_cost_saved_usd``): each avoided roundtrip re-reads the live
      context at the cache-read rate and, on newer rows, also carries the
      turn's average output (billed at the output rate and re-entering context
      at the cache-write rate).
    * tokens above the 2M per-call sanity cap are dropped (pre-fce2110
      inflation bug).
    * ``kind == "compaction"`` rows mark a Claude-managed context reset. They
      bound carry windows but are not counted as Atelier-generated savings.
    * ``kind == "output_style"`` (telegraphic prose the model did not emit) and
      ``kind == "external_compactor"`` (rtk-measured bash reduction) rows carry
      ``tokens`` + ``cost_saved_usd`` and count as normal savings rows.
    * every other row uses the pre-priced ``cost_saved_usd`` the dispatcher
      wrote (priced at the model in use at write time); rows that predate that
      field are re-priced at the row model's input rate.  Rows with neither a
      stored cost nor a priceable model are returned as ``unpriced_tokens`` so
      the caller can apply a single weighted fallback without distorting the
      usd/token ratio.
    """
    from atelier.core.capabilities.pricing import get_model_pricing

    tokens = max(0, int(ev.get("tokens") or ev.get("tokens_saved") or 0))
    calls = max(0, int(ev.get("calls") or ev.get("calls_saved") or 0))
    calls_usd = max(0.0, float(ev.get("calls_usd") or ev.get("calls_cost_saved_usd") or 0.0))
    if tokens > 2_000_000:
        tokens = 0
    if str(ev.get("kind") or "") == "compaction":
        return 0, 0.0, 0, 0.0, 0
    if tokens <= 0:
        return 0, 0.0, calls, calls_usd, 0
    # Prefer the cost the dispatcher pre-priced at write time; re-price only the
    # legacy rows that predate that field.
    stored = ev.get("cost_saved_usd")
    if stored is not None:
        return tokens, max(0.0, float(stored or 0.0)), calls, calls_usd, 0
    model_raw = str(ev.get("model") or "").strip()
    pricing = get_model_pricing(resolve_model_id(model_raw)) if model_raw else None
    if pricing is not None and pricing.known and pricing.input > 0:
        return tokens, pricing.input / 1_000_000 * tokens, calls, calls_usd, 0
    return 0, 0.0, calls, calls_usd, tokens


def _find_savings_sidecar(session_id: str, root: Path) -> Path:
    """Locate savings.jsonl for *session_id* under the canonical session dir.

    Host-agnostic: :func:`~atelier.core.foundation.paths.find_session_dir`
    globs by session id alone. When no directory exists yet (first write for
    a brand-new session), falls back to today's dir for the detected host so
    the caller's ``path.parent.mkdir(parents=True, exist_ok=True)`` creates
    the right tree.
    """
    from atelier.core.foundation.paths import detect_host, find_session_dir, session_dir

    existing = find_session_dir(root, session_id)
    if existing is not None:
        return existing / "savings.jsonl"
    return session_dir(root, detect_host(), session_id) / "savings.jsonl"


def _read_claude_session_savings(session_id: str, atelier_root: Path) -> tuple[int, int, float, int]:
    """Return ``(tokens_saved, calls_saved, usd_saved, unpriced_tokens)``.

    Every row is priced through :func:`_price_savings_row` — the shared rule the
    statusline, stop hook, CLI, dashboard, and web Savings page all use — so the
    per-session live total and the windowed totals never disagree.  Rows with no
    priceable model are returned via ``unpriced_tokens`` so the caller can apply
    a single weighted fallback rate without distorting the usd/token ratio.
    """
    if not session_id:
        return 0, 0, 0.0, 0
    path = _find_savings_sidecar(session_id, atelier_root)
    if not path.exists():
        return 0, 0, 0.0, 0

    priced_tokens = 0
    calls_total = 0
    usd_total = 0.0
    unpriced_tokens = 0
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            except Exception:
                logging.exception("Recovered from broad exception handler")
                continue
            pt, usd, c, calls_usd, up = _price_savings_row(ev)
            priced_tokens += pt
            usd_total += usd + calls_usd
            calls_total += c
            unpriced_tokens += up
    except OSError:
        pass
    return priced_tokens, calls_total, usd_total, unpriced_tokens


def _read_session_routing_usd(session_id: str, atelier_root: Path) -> float:
    """Sum model-routing savings from the per-session sidecar.

    The MCP server appends a ``kind == "routing"`` row (priced at decision time)
    to ``sessions/<id>/savings.jsonl`` for every routing saving. These rows'
    ``tokens`` field is absent/zero, so :func:`_read_claude_session_savings`'s
    row loop never counts them — this dedicated reader is the only place that
    sums them, both for the statusline's ↓routing breakdown AND (via
    :func:`compute_savings_summary`, which folds this value into ``saved_usd``)
    for the realized-savings total. Read from the small per-session file rather
    than scanning the large ``live_savings_events.jsonl`` on every render.
    """
    if not session_id:
        return 0.0
    path = _find_savings_sidecar(session_id, atelier_root)
    if not path.exists():
        return 0.0
    total = 0.0
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if str(ev.get("kind") or "") == "routing":
                total += max(0.0, float(ev.get("usd") or 0.0))
    except OSError:
        pass
    return round(total, 6)


def read_session_end_carry(session_id: str, atelier_root: Path) -> tuple[float, int] | None:
    """Persisted carry from this session's newest ``session_end`` row.

    This is the SAME value :func:`_fold_session_file` folds into the
    day-bucketed aggregate that backs every windowed savings surface
    (statusline, CLI stats, web Savings page) -- frozen at the moment the
    Stop hook last fired. ``compute_savings_summary``'s ``carry_usd`` instead
    re-derives carry live via :func:`_carry_credit`, which re-prices every
    carried row through the CURRENT pricing table; if that table's
    resolution for a model drifts between the Stop fire and a later caller
    (e.g. a pricing data update), the live figure silently diverges from the
    frozen snapshot the aggregate already committed to. Callers that want a
    number guaranteed to reconcile with the statusline/aggregate for an
    already-Stopped session should prefer this over a live recompute.

    Returns ``None`` when no ``session_end`` row exists yet (session never
    Stopped -- callers should fall back to a live recompute in that case).
    """
    if not session_id:
        return None
    path = _find_savings_sidecar(session_id, atelier_root)
    if not path.exists():
        return None
    end_ts = 0.0
    end_carry = 0.0
    end_carry_tokens = 0
    found = False
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if row.get("kind") != "session_end":
                continue
            ts = _row_epoch(str(row.get("ts", "")))
            if ts is None or ts < end_ts:
                continue
            end_ts = ts
            end_carry = float(row.get("carry_usd") or 0.0)
            end_carry_tokens = int(row.get("carry_tokens") or 0)
            found = True
    except OSError:
        return None
    if not found:
        return None
    return round(end_carry, 6), end_carry_tokens


def _read_session_output_style(session_id: str, atelier_root: Path) -> tuple[int, float]:
    """Sum telegraphic output-style savings (kind=="output_style" rows).

    These rows are already inside the shared totals (:func:`_price_savings_row`
    counts them); this reader only provides the ↓O display breakdown.
    """
    if not session_id:
        return 0, 0.0
    path = _find_savings_sidecar(session_id, atelier_root)
    if not path.exists():
        return 0, 0.0
    tokens = 0
    usd = 0.0
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if str(ev.get("kind") or "") == "output_style":
                tokens += max(0, int(ev.get("tokens") or 0))
                usd += max(0.0, float(ev.get("cost_saved_usd") or 0.0))
    except OSError:
        pass
    return tokens, round(usd, 6)


def _read_session_input_style(session_id: str, atelier_root: Path) -> tuple[int, float]:
    """Sum per-turn context-style savings (kind=="input_style" rows).

    These rows are already inside the shared totals (:func:`_price_savings_row`
    counts them); this reader only provides the ↓In display breakdown.
    """
    if not session_id:
        return 0, 0.0
    path = _find_savings_sidecar(session_id, atelier_root)
    if not path.exists():
        return 0, 0.0
    tokens = 0
    usd = 0.0
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if str(ev.get("kind") or "") == "input_style":
                tokens += max(0, int(ev.get("tokens") or 0))
                usd += max(0.0, float(ev.get("cost_saved_usd") or 0.0))
    except OSError:
        pass
    return tokens, round(usd, 6)


def _is_read_lever(tool: str) -> bool:
    """True when *tool* (a savings.jsonl row's ``tool`` field) normalizes to a
    read-side lever: ``search_read``, ``cached_read``, or ``scoped_recall``.

    Mirrors the substring rules of ``mcp_server.py:_lever_for_tool`` (tool-name
    based — what these sidecar rows actually carry) and the read-lever subset
    of ``api.py:_normalize_lever`` (operation-string based, used elsewhere for
    a differently-shaped event log), duplicated in miniature here rather than
    imported: ``api.py`` already imports ``aggregate_window_savings`` from this
    module, so importing either classifier back would create a circular import.
    """
    ident = (tool or "").strip().lower().replace("-", "_").replace(" ", "_")
    if not ident:
        return False
    if ident in {"read", "search"} or ident.endswith("_read") or ident.endswith("_search"):
        return True  # search_read
    if "cache" in ident:
        return True  # cached_read
    if ident == "memory" or ident.endswith("_memory") or "recall" in ident:
        return True  # scoped_recall
    return False


def _read_session_read_savings(session_id: str, atelier_root: Path) -> tuple[int, float]:
    """Sum read/retrieval-side savings (search_read, cached_read, scoped_recall).

    These are plain per-tool-call rows (no ``kind`` field — routing,
    compaction, output-style, turn-cut, and external-compactor rows all carry
    a ``kind`` and are excluded here) already inside the shared totals via
    :func:`_read_claude_session_savings`; this reader classifies each row's
    ``tool`` name with :func:`_is_read_lever` to provide the Read display
    breakdown, the same way :func:`_read_session_output_style` provides Output.
    """
    if not session_id:
        return 0, 0.0
    path = _find_savings_sidecar(session_id, atelier_root)
    if not path.exists():
        return 0, 0.0
    tokens = 0
    usd = 0.0
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if ev.get("kind"):
                continue
            if not _is_read_lever(str(ev.get("tool") or "")):
                continue
            t = max(0, int(ev.get("tokens") or ev.get("tokens_saved") or 0))
            if t > 2_000_000:
                continue
            tokens += t
            usd += max(0.0, float(ev.get("cost_saved_usd") or 0.0))
    except OSError:
        pass
    return tokens, round(usd, 6)


def _resolve_workspace_session_id(workspace: str | None, root_path: Path) -> str:
    """Read the active session_id from workspace/session_state.json.

    Used as fallback when the caller-supplied session_id has no savings
    (e.g. subagent sessions that don't have their own MCP sidecar).
    """
    if not workspace:
        return ""

    try:
        from atelier.core.foundation.paths import workspace_key

        ws_hash = workspace_key(Path(workspace).resolve())
        state_path = root_path / "workspaces" / ws_hash / "session_state.json"
        if not state_path.is_file():
            return ""
        data = json.loads(state_path.read_text(encoding="utf-8"))
        return str(data.get("session_id") or "")
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return ""


def _carry_credit(
    session_id: str,
    atelier_root: Path,
    turn_timestamps: list[str],
    subagent_turn_timestamps: list[list[str]] | None = None,
    *,
    context_ceiling: int = 0,
) -> tuple[int, float]:
    """Context-carry credit for saved tokens, attributed per context window.

    A token kept out of context at turn N is also NOT re-read at the cache-read
    rate on every later assistant turn that re-sends that window. Each subagent
    runs in its *own* context window: a token a subagent saved carries across
    that subagent's own later turns only — the main thread never re-reads it
    (the subagent's context is discarded on return) and neither do sibling
    subagents (fresh contexts). So a savings row is credited against the turns
    of the window it was generated in: if its timestamp falls inside a
    subagent's lifetime it carries over that subagent's turns; otherwise over
    the main thread's turns (until the next compaction drops it).

    Subagent rows land in the *parent* session's savings.jsonl (the shared MCP
    process keys by the parent session id and cannot tell a subagent call from
    a main-loop call), so attribution is reconstructed here from the row
    timestamp and the per-subagent turn windows parsed from the transcript.

    Fully measured: row timestamps from the sidecar, turn timestamps from the
    transcript, rates from the per-row model. Rows with unknown models
    contribute nothing. Returned separately — never folded into saved_usd.

    Carried tokens are capped at the model's context window: a token can only be
    re-read while it stays resident, so the tokens carried into any single later
    turn never exceed the window (and a compaction drops them entirely). Replaced
    tool calls are NOT a carry multiplier — an avoided call never ran, so it
    re-reads nothing; those calls belong only in the one-time ``saved`` figure.
    Rows stamped ``long_context`` (written while the window was past the model's
    >200k premium threshold) price their carry at the premium cache-read rate,
    matching the cost side.
    """
    if not session_id:
        return 0, 0.0
    path = _find_savings_sidecar(session_id, atelier_root)
    if not path.exists():
        return 0, 0.0
    import bisect
    from datetime import datetime

    def _parse(ts: str) -> datetime | None:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

    main_turns = sorted(t for t in (_parse(x) for x in turn_timestamps) if t is not None)

    # One (start, end, sorted_turns) window per subagent transcript, sorted
    # latest-start-first so an overlapping row (parallel subagents) is
    # attributed to the most-recently-spawned containing window — a
    # deterministic tiebreak.
    sub_windows: list[tuple[datetime, datetime, list[datetime]]] = []
    for sub in subagent_turn_timestamps or []:
        ts_list = sorted(t for t in (_parse(x) for x in sub) if t is not None)
        if ts_list:
            sub_windows.append((ts_list[0], ts_list[-1], ts_list))
    sub_windows.sort(key=lambda w: w[0], reverse=True)

    if not main_turns and not sub_windows:
        return 0, 0.0
    from atelier.core.capabilities.pricing import get_model_pricing

    carry_tokens = 0
    carry_usd = 0.0
    # Fallback context ceiling for models whose pricing card omits the window.
    # Conservative on purpose: under-crediting an unknown model beats the
    # unbounded blow-up a missing cap produced.
    default_ctx_window = 200_000
    # Model-string → pricing card, resolved once per distinct model per call
    # (previously re-resolved per row-x-turn pair — a measured pricing hotspot).
    _MEMO_MISS = object()
    _pricing_memo: dict[str, Any] = {}
    # (ts, tokens, model, long_context) for one savings row.
    Row = tuple[datetime, int, str, bool]
    try:
        events: list[dict[str, Any]] = []
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(ev, dict):
                events.append(ev)

        compactions = sorted(
            ts
            for ev in events
            if str(ev.get("kind") or "") == "compaction"
            if (ts := _parse(str(ev.get("ts") or ""))) is not None
        )

        # Parse savings rows once, bucketed by the window they were generated in
        # (None = main thread, i = sub_windows[i]). Avoided-call counts are NOT a
        # carry multiplier: a replaced call never ran, so it re-reads no carried
        # token — it belongs only in the one-time ``saved`` figure.
        rows_by_window: dict[int | None, list[Row]] = {}
        for ev in events:
            ev_kind = str(ev.get("kind") or "")
            if ev_kind == "compaction":
                continue  # Claude reset boundary, not an Atelier saving row.
            if ev_kind in ("input_style", "turn_cut"):
                # Both are already recurring, per-turn credits re-earned fresh at
                # every Stop fire (input_style: this turn's resend-volume gap;
                # turn_cut: priced via calls/calls_usd, never tokens). Neither
                # row represents a token that newly entered context and would
                # sit there to be re-read later -- carrying them forward would
                # compound the same per-turn gap on top of itself every
                # subsequent turn, growing unboundedly with session length.
                # Only rows for content that would have been WRITTEN into
                # context (realized reads, output_style prose) belong here.
                continue
            row_dt = _parse(str(ev.get("ts") or ""))
            if row_dt is None:
                continue
            t = max(0, int(ev.get("tokens") or ev.get("tokens_saved") or 0))
            if t <= 0 or t > 2_000_000:
                continue
            model = str(ev.get("model") or "").strip()
            pricing = _pricing_memo.get(model, _MEMO_MISS)
            if pricing is _MEMO_MISS:
                pricing = _pricing_memo[model] = get_model_pricing(resolve_model_id(model))
            if pricing is None or not pricing.known or pricing.cache_read <= 0:
                continue  # unknown/unpriceable model — never guess a rate
            widx = next((i for i, w in enumerate(sub_windows) if w[0] <= row_dt <= w[1]), None)
            rows_by_window.setdefault(widx, []).append((row_dt, t, model, bool(ev.get("long_context"))))

        def _window_carry(seg_rows: list[Row], seg_turns: list[datetime]) -> None:
            """Turn-centric carry for one window, capped at the context window.

            Each saved row keeps its own model and long-context stamp. That keeps
            mixed-model sessions honest instead of pricing a whole segment at the
            last row's rate.

            Greedy oldest-first consumption up to the cap is a prefix scan over
            time-sorted rows, so the per-row adjusted tokens and full-row USD are
            prefix-summed ONCE and each turn is priced with a bisect plus at most
            one partial-row pricing call — O((rows+turns)·log rows). The previous
            per-turn re-walk of every row was O(turns*rows) with a pricing lookup
            per pair: measured 3-4s of GIL-held CPU per statusline refresh on a
            long session, stalling concurrent MCP tool calls. Same carry per turn
            (min(tok, row_cap, remaining_cap), oldest first); only float
            association order changes.
            """
            nonlocal carry_tokens, carry_usd
            if not seg_rows or not seg_turns:
                return
            sorted_rows = sorted(seg_rows, key=lambda row: row[0])
            # Time-ordered priceable rows, flattened for bisect.
            row_dts: list[datetime] = []
            row_adj: list[int] = []
            row_pricings: list[Any] = []
            row_long: list[bool] = []
            for row_dt, tok, model, long_ctx in sorted_rows:
                row_pricing = _pricing_memo.get(model, _MEMO_MISS)
                if row_pricing is _MEMO_MISS:
                    row_pricing = _pricing_memo[model] = get_model_pricing(resolve_model_id(model))
                if row_pricing is None or not row_pricing.known or row_pricing.cache_read <= 0:
                    continue
                adjusted = min(tok, row_pricing.context_window or default_ctx_window)
                if adjusted <= 0:
                    continue
                row_dts.append(row_dt)
                row_adj.append(adjusted)
                row_pricings.append(row_pricing)
                row_long.append(long_ctx)
            if not row_dts:
                return
            # prefix_tok[i] / prefix_usd[i]: first i rows carried in full.
            prefix_tok: list[int] = [0]
            prefix_usd: list[float] = [0.0]
            for adjusted, row_pricing, long_ctx in zip(row_adj, row_pricings, row_long, strict=True):
                prefix_tok.append(prefix_tok[-1] + adjusted)
                prefix_usd.append(
                    prefix_usd[-1] + row_pricing.request_cost_usd(cache_read_tokens=adjusted, long_context=long_ctx)
                )
            for turn_dt in seg_turns:
                cap = context_ceiling if context_ceiling > 0 else default_ctx_window
                k = bisect.bisect_left(row_dts, turn_dt)  # rows with row_dt < turn_dt
                if k <= 0:
                    continue
                if prefix_tok[k] <= cap:
                    carry_tokens += prefix_tok[k]
                    carry_usd += prefix_usd[k]
                    continue
                # Largest full-row prefix under the cap, then one partial row.
                m = bisect.bisect_right(prefix_tok, cap, 0, k + 1) - 1
                carry_tokens += prefix_tok[m]
                carry_usd += prefix_usd[m]
                partial = cap - prefix_tok[m]
                if partial > 0 and m < k:
                    carry_tokens += partial
                    carry_usd += row_pricings[m].request_cost_usd(cache_read_tokens=partial, long_context=row_long[m])

        # Main thread: a saved token carries across later main turns until the
        # next compaction drops it, so split rows and turns into per-compaction
        # segments and credit each independently.
        main_rows = sorted(rows_by_window.get(None, []))
        seg_count = len(compactions) + 1
        main_row_segs: list[list[Row]] = [[] for _ in range(seg_count)]
        for r in main_rows:
            main_row_segs[bisect.bisect_right(compactions, r[0])].append(r)
        main_turn_segs: list[list[datetime]] = [[] for _ in range(seg_count)]
        for turn_dt in main_turns:
            main_turn_segs[bisect.bisect_right(compactions, turn_dt)].append(turn_dt)
        for seg_rows, seg_turns in zip(main_row_segs, main_turn_segs, strict=True):
            _window_carry(seg_rows, seg_turns)

        # Subagents: each carries across its own later turns only — its context
        # is discarded on return, never re-read by the main thread or siblings.
        for i, (_, _, sub_t) in enumerate(sub_windows):
            _window_carry(sorted(rows_by_window.get(i, [])), sub_t)
    except OSError:
        return 0, 0.0
    return carry_tokens, round(carry_usd, 6)


def _cliff_credit(
    session_id: str,
    atelier_root: Path,
    turn_usage: list[dict[str, Any]],
    subagent_turn_timestamps: list[list[str]] | None = None,
) -> float:
    """Long-context cliff-avoidance credit, fully measured.

    Anthropic reprices the ENTIRE request at premium (>200k) rates the moment
    its context crosses the model's long-context threshold. A main-thread turn
    that stayed at or under the threshold, but whose window WOULD have crossed
    it had Atelier's saved tokens still been in context, avoided the
    premium-minus-base delta on every token it actually billed. Saved tokens
    counted are main-window rows since the last compaction (subagent rows
    never inflate the main window); the delta uses the turn's own model rates.
    Turns already over the threshold get nothing here — their savings rows
    are premium-priced at write time instead (no double count).
    """
    if not session_id or not turn_usage:
        return 0.0
    path = _find_savings_sidecar(session_id, atelier_root)
    if not path.exists():
        return 0.0
    import bisect
    from datetime import datetime

    def _parse(ts: str) -> datetime | None:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

    sub_windows: list[tuple[datetime, datetime]] = []
    for sub in subagent_turn_timestamps or []:
        ts_list = sorted(t for t in (_parse(x) for x in sub) if t is not None)
        if ts_list:
            sub_windows.append((ts_list[0], ts_list[-1]))

    saved_rows: list[tuple[datetime, int]] = []
    compactions: list[datetime] = []
    try:
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                ev = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if not isinstance(ev, dict):
                continue
            row_dt = _parse(str(ev.get("ts") or ""))
            if row_dt is None:
                continue
            if str(ev.get("kind") or "") == "compaction":
                compactions.append(row_dt)
                continue
            t = max(0, int(ev.get("tokens") or ev.get("tokens_saved") or 0))
            if t <= 0 or t > 2_000_000:
                continue
            if any(s <= row_dt <= e for s, e in sub_windows):
                continue  # subagent-saved tokens never inflate the main window
            saved_rows.append((row_dt, t))
    except OSError:
        return 0.0
    if not saved_rows:
        return 0.0
    saved_rows.sort(key=lambda r: r[0])
    compactions.sort()
    row_ts = [dt for dt, _ in saved_rows]
    row_cum: list[int] = []
    running = 0
    for _, t in saved_rows:
        running += t
        row_cum.append(running)

    from atelier.core.capabilities.pricing import get_model_pricing

    credit = 0.0
    for turn in turn_usage:
        turn_dt = _parse(str(turn.get("ts") or ""))
        if turn_dt is None:
            continue
        model = resolve_model_id(str(turn.get("model") or "").strip())
        if not model:
            continue
        pricing = get_model_pricing(model)
        if pricing is None or not pricing.known:
            continue
        threshold = pricing.long_context_threshold()
        if threshold <= 0:
            continue
        in_t = int(turn.get("in") or 0)
        out_t = int(turn.get("out") or 0)
        cr_t = int(turn.get("cR") or 0)
        cw_t = int(turn.get("cW") or 0)
        request_ctx = in_t + cr_t + cw_t
        if request_ctx <= 0 or request_ctx > threshold:
            continue  # already premium — covered by write-time premium pricing
        # Saved-and-still-out tokens: main-window rows since the last compaction.
        lo = 0
        comp_idx = bisect.bisect_right(compactions, turn_dt)
        if comp_idx > 0:
            lo = bisect.bisect_right(row_ts, compactions[comp_idx - 1])
        hi = bisect.bisect_right(row_ts, turn_dt)
        saved_before = (row_cum[hi - 1] - (row_cum[lo - 1] if lo else 0)) if hi > lo else 0
        if request_ctx + saved_before <= threshold:
            continue
        credit += pricing.request_cost_usd(
            input_tokens=in_t,
            output_tokens=out_t,
            cache_read_tokens=cr_t,
            cache_write_tokens=cw_t,
            long_context=True,
        ) - pricing.request_cost_usd(
            input_tokens=in_t,
            output_tokens=out_t,
            cache_read_tokens=cr_t,
            cache_write_tokens=cw_t,
        )
    return round(max(0.0, credit), 6)


def compute_savings_summary(
    session_id: str = "",
    *,
    atelier_root: str | Path | None = None,
    workspace: str | None = None,
) -> SavingsSummary:
    """Aggregate savings for a session.

    Token savings come from ``sessions/<session_id>/savings.jsonl`` —
    the MCP dispatcher appends one row per tool call there (keyed by the
    Claude session UUID that SessionStart writes to session_state.json).

    If ``session_id`` has no savings and ``workspace`` is provided, falls back
    to the session_id stored in the workspace's session_state.json (for
    subagent scenarios where the subagent doesn't have its own sidecar).

    Cost baseline (``est_cost_usd``) still comes from the Claude transcript
    since Claude Code does preserve token-usage entries there.
    """
    result = SavingsSummary()
    # A missing live session id means Claude has not bound this statusline frame
    # to a concrete session yet. In that state we must not borrow savings from
    # the workspace's previous session, or brand-new sessions appear to start
    # with non-zero savings before the first prompt.
    if not session_id:
        return result
    root_path: Path
    if atelier_root is not None:
        root_path = Path(atelier_root)
    else:
        env_root = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT")
        root_path = Path(env_root) if env_root else Path.home() / ".atelier"

    # --- savings rows (primary source) ---
    priced_tokens, calls, row_usd, unpriced_tokens = (
        _read_claude_session_savings(session_id, root_path) if session_id else (0, 0, 0.0, 0)
    )

    # Fallback: subagent sessions have no sidecar — look for parent session in transcript.
    # Discriminator: a *subagent* transcript has NO entries whose sessionId matches
    # the current session_id (all lines reference the parent session).  A main
    # session (post-compact, post-clear, fresh) has at least one own entry;
    # skipping those prevents borrowing stale savings from a prior session whose
    # sessionId happens to appear early in a resumed/compacted transcript.
    if priced_tokens == 0 and unpriced_tokens == 0 and calls == 0:
        # Extract parent session_id from subagent transcript if possible
        parent_id = None
        for cand in claude_transcript_candidates(session_id):
            try:
                candidate_parent: str | None = None
                has_own_entries = False
                with cand.open(encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        entry_sid = entry.get("sessionId")
                        if not entry_sid:
                            continue
                        if entry_sid == session_id:
                            # Found an entry owned by this session — it's a main
                            # session, not a subagent. Bail immediately.
                            has_own_entries = True
                            break
                        candidate_parent = entry_sid
                if not has_own_entries and candidate_parent:
                    parent_id = candidate_parent
                    break
            except Exception:
                logging.exception("Recovered from broad exception handler")
                continue

        if parent_id and parent_id != session_id:
            priced_tokens, calls, row_usd, unpriced_tokens = _read_claude_session_savings(parent_id, root_path)
            if priced_tokens > 0 or unpriced_tokens > 0 or calls > 0:
                session_id = parent_id  # use the found session for transcript lookup too

    if priced_tokens == 0 and unpriced_tokens == 0 and calls == 0 and workspace:
        workspace_session_id = _resolve_workspace_session_id(workspace, root_path)
        if workspace_session_id and workspace_session_id != session_id:
            ws_tokens, ws_calls, ws_usd, ws_unpriced = _read_claude_session_savings(workspace_session_id, root_path)
            if ws_tokens > 0 or ws_unpriced > 0 or ws_calls > 0:
                session_id = workspace_session_id
                priced_tokens, calls, row_usd, unpriced_tokens = ws_tokens, ws_calls, ws_usd, ws_unpriced

    result.smart_calls = calls
    # Per-session model-routing savings: read cheaply from the sidecar
    # (kind="routing" rows); folded into saved_usd below (still tracked here
    # separately for the ↓routing display breakdown), same as output.
    result.routing_saved_usd = _read_session_routing_usd(session_id, root_path)
    # Telegraphic output-style breakdown (inside the totals; shown as ↓O).
    result.output_saved_tokens, result.output_saved_usd = _read_session_output_style(session_id, root_path)
    # Per-turn context-style breakdown (inside the totals; shown as ↓In).
    result.input_saved_tokens, result.input_saved_usd = _read_session_input_style(session_id, root_path)
    # Read/retrieval-side breakdown (inside the totals; shown as ↓Read).
    result.read_saved_tokens, result.read_saved_usd = _read_session_read_savings(session_id, root_path)

    # --- cost baseline + model from transcript ---
    paths = claude_transcript_candidates(session_id) if session_id else []
    stats = read_transcript_stats(paths[0]) if paths else None
    if stats is not None:
        result.est_cost_usd = stats.est_cost_usd
        result.total_tokens = stats.total_tokens
        result.display_input_tokens = stats.input_tokens + stats.cache_write_tokens
        result.display_cache_tokens = stats.cache_read_tokens
        result.display_output_tokens = stats.output_tokens
    # --- context-carry credit (separate display line; never in saved_usd) ---
    if stats is not None and (stats.turn_timestamps or stats.subagent_turn_timestamps):
        # Cap carry at the context the session actually operated in, not the
        # model's theoretical window (which can be several x larger).
        peak_ctx = 0
        for _t in stats.turn_usage:
            peak_ctx = max(peak_ctx, int(_t.get("in", 0)) + int(_t.get("cR", 0)) + int(_t.get("cW", 0)))
        result.carry_tokens, result.carry_usd = _carry_credit(
            session_id,
            root_path,
            stats.turn_timestamps,
            stats.subagent_turn_timestamps,
            context_ceiling=peak_ctx,
        )
    # --- long-context cliff-avoidance, folded into the carry line: both are
    # measured context effects, and one field keeps every display surface
    # (statusline, stop hook, history snapshots) consistent ---
    if stats is not None and stats.turn_usage:
        cliff = _cliff_credit(session_id, root_path, stats.turn_usage, stats.subagent_turn_timestamps)
        if cliff > 0:
            result.carry_usd = round(result.carry_usd + cliff, 6)

    # --- price unpriced tokens at the session's weighted input rate ---
    # Per-row prices are exact (model captured at write time).  For rows that
    # arrived without a model (older format, or before the SessionStart bridge
    # registered one), apply the transcript's weighted input rate so the user
    # sees a single, consistent (usd / tokens) ratio.  If we can't derive any
    # rate, those tokens are dropped from the display entirely — never count
    # something we can't price.
    extra_usd = 0.0
    extra_tokens = 0
    if unpriced_tokens > 0:
        rate: float | None = stats.savings_input_rate() if stats is not None else None
        if rate is None:
            try:
                from atelier.core.capabilities.pricing import get_model_pricing

                for mid in (stats.last_model if stats else "", "claude-sonnet-4-5"):
                    if not mid:
                        continue
                    pricing = get_model_pricing(resolve_model_id(mid))
                    if pricing is not None and pricing.known and pricing.input > 0:
                        rate = pricing.input / 1_000_000
                        break
            except Exception:
                logging.exception("Recovered from broad exception handler")
                rate = None
        if rate and rate > 0:
            extra_usd = rate * unpriced_tokens
            extra_tokens = unpriced_tokens

    result.ctx_saved = priced_tokens + extra_tokens
    # Routing is folded in here (unlike carry_usd, a recurring re-read credit
    # that stays separate) — mirrors output_saved_usd, which is already inside
    # row_usd because output-style rows are priced as normal rows in the loop
    # above (see _price_savings_row).
    result.saved_usd = row_usd + extra_usd + result.routing_saved_usd

    total_baseline = result.saved_usd + result.carry_usd + result.est_cost_usd
    if total_baseline > 0:
        result.saved_pct = (result.saved_usd / total_baseline) * 100
        result.carry_pct = (result.carry_usd / total_baseline) * 100

    # --- N4: per-tool exact in/out token ledger (additive surface) ---
    try:
        from atelier.core.capabilities.tool_token_ledger import load_tool_token_ledger

        ledger = load_tool_token_ledger(root_path)
        result.tool_token_ledger = {name: counts.to_dict() for name, counts in ledger.per_tool.items()}
        result.tool_ledger_input_tokens = ledger.total_input_tokens()
        result.tool_ledger_output_tokens = ledger.total_output_tokens()
    except Exception:
        logging.exception("Recovered from broad exception handler")

    return result


# Rotating statusline feature tips. Grounded in what the installed plugin
# actually ships (the agents/ and skills/ staged by install_claude.sh) — when
# a mode or skill is added or removed there, update its tip here.
_STATUS_TIPS: tuple[str, ...] = (
    "`/atelier:code` — main coding mode: indexed search, batched edits, owned completion",
    "`/atelier:explore` — read-only explorer: files, symbols, patterns; never edits",
    "`/atelier:plan` — turn grounded context into a concrete, reviewable plan first",
    "`/atelier:execute` — apply an accepted plan with surgical, minimal edits",
    "`/atelier:review` — adversarial review: verified findings, ranked by severity",
    "`/atelier:research` — fetch web pages, repos, and docs into a cited memo",
    "`/atelier:solve` — own a task end-to-end; ship early, iterate against the real check",
    "`/atelier:recall` — what Atelier learned from your past sessions, on demand",
    "`/atelier:ux-review` — WCAG + design-token gates, verified in a real browser",
    "`/atelier:perf-review` — latency/memory/scaling gates measured by running it",
    "`/atelier:orchestrate` — one structured run: subagent vs isolated worktree",
    "`/atelier:swarm` — parallel multi-worktree swarm runs on your repo",
    "`/atelier:benchmark` — Atelier vs vanilla Claude Code on your repo: cost, turns, time",
    "`atelier savings` — realized savings: this session, 1d, 7d, 30d",
)


def _status_tip() -> str:
    """A rotating feature tip (changes ~every 90s so it isn't flickery)."""
    return _STATUS_TIPS[int(time.time() // 90) % len(_STATUS_TIPS)]


def _colorize_tip(text: str, c_dim: str, c_tool: str, c_reset: str) -> str:
    """Highlight backtick-wrapped tool/command names; wrap the rest in dim.

    ``text`` is left unmodified when all color strings are empty (no-color mode).
    """
    colored = re.sub(
        r"`([^`]+)`",
        lambda m: f"{c_reset}{c_tool}{m.group(1)}{c_reset}{c_dim}",
        text,
    )
    return f"{c_dim}{colored}{c_reset}"


def _resolve_status_text(atelier_root: str | Path | None = None) -> str:
    """Return update / login / subscription warning text for the statusline.

    Falls back to a rotating feature tip (when ``statusLineTips`` is enabled)
    so the lowest-priority slot coaches the user toward Atelier features.
    """
    root = Path(atelier_root) if atelier_root else None
    if root is None:
        root_env = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT") or ""
        root = Path(root_env) if root_env else None
    if root is None:
        return ""

    def _read(name: str) -> dict[str, Any]:
        p = root / name
        if not p.is_file():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            logging.exception("Recovered from broad exception handler")
            return {}

    auth = _read("auth.json")
    if ((not auth) or auth.get("authenticated") is False) and os.environ.get("ATELIER_HIDE_MISSING_LOGIN") != "1":
        return "login"
    update = _read("update.json")
    if update.get("toVersion") and update.get("toVersion") != update.get("fromVersion"):
        return f"update {update.get('toVersion')}"
    subscription = _read("subscription.json")
    if subscription.get("warning"):
        return str(subscription.get("message") or "subscription")[:40]
    # Lowest priority: a rotating feature tip, when statusLineTips is enabled.
    raw = _read("plugin_settings.json")
    nested = raw.get("atelier")
    settings = nested if isinstance(nested, dict) else raw
    if settings.get("statusLineTips", True) is not False:
        return _status_tip()
    return ""


def _fmt_usd(v: float) -> str:
    """Shared currency formatter: 2 decimal places. Plain module-level function
    (importable by CLI commands, session_report.py, dashboard.py, audit.py,
    api.py, etc.) so every Python surface renders dollar amounts the same way.
    """
    v = float(v or 0.0)
    return f"${v:,.2f}"


def _fmt_tok(n: int) -> str:
    """Shared token-count formatter: <1k literal int, 1k-1M as N.Nk, >=1M as N.NNM."""
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def _fmt_pct(p: float) -> str:
    """Shared percent formatter: one decimal. *p* is an already-scaled 0-100
    number (matching this file's saved_pct/carry_pct fields), not a 0-1 fraction.
    """
    return f"{float(p or 0.0):.1f}%"


def load_usage_breakdown(root: str | Path) -> dict[str, Any]:
    """Aggregate project-wide token usage and cost from atelier.db."""
    root_path = Path(root)
    db_path = root_path / "atelier.db"
    if not db_path.exists():
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "cost_usd": 0.0,
            "breakdown": {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0},
        }

    from atelier.core.capabilities.pricing import usage_cost_breakdown_usd, usage_cost_usd

    input_tokens = 0
    output_tokens = 0
    cache_read_tokens = 0
    cache_write_tokens = 0
    total_cost = 0.0
    breakdown = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}

    try:
        from atelier.core.foundation.store import ContextStore

        # Token/model rows come straight from atelier.db's traces table (see
        # ContextStore.token_rows) -- json_extract on the payload, not a full
        # Trace parse per row.
        for row in ContextStore(root_path).token_rows():
            inp = int(row["input_tokens"] or 0)
            out = int(row["output_tokens"] or 0)
            cr = int(row["cached_input_tokens"] or 0)
            model_id = resolve_model_id(row["model"]) or "claude-sonnet-4-5"

            input_tokens += inp
            output_tokens += out
            cache_read_tokens += cr

            total_cost += usage_cost_usd(model_id, input_tokens=inp, output_tokens=out, cache_read_tokens=cr)
            b = usage_cost_breakdown_usd(model_id, input_tokens=inp, output_tokens=out, cache_read_tokens=cr)
            breakdown["input"] += b["input"]
            breakdown["output"] += b["output"]
            breakdown["cache_read"] += b["cache_read"]
            breakdown["cache_write"] += b["cache_write"]

    except Exception:
        logging.exception("Failed to load usage breakdown from DB")

    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cost_usd": round(total_cost, 6),
        "breakdown": {k: round(v, 6) for k, v in breakdown.items()},
    }


def render_savings_summary(payload: dict[str, Any]) -> str:
    """Render the default ``atelier savings`` view as a compact human summary.

    Surfaces the headline numbers, the 1/7/30-day window table, and the plan
    line from the ``build_savings_report`` payload. The full raw structure
    stays available via ``atelier savings --json``.
    """

    def _int(v: Any) -> str:
        return f"{int(v or 0):,}"

    saved = float(payload.get("saved_usd") or 0.0)
    calls = int(payload.get("calls_avoided") or 0)
    tokens = int(payload.get("tokens_saved") or 0)
    breakdown = payload.get("summary_breakdown") or {}
    d30 = breakdown.get("30D") or {}
    spend30 = float(d30.get("spend") or 0.0)

    lines: list[str] = ["Atelier savings", "─" * 56]

    if spend30 > 0:
        pct = saved / spend30 * 100
        lines.append(f"  Saved            {_fmt_usd(saved)}   ({_fmt_pct(pct)} of {_fmt_usd(spend30)} spend · 30d)")
    else:
        lines.append(f"  Saved            {_fmt_usd(saved)}")
    lines.append(f"  Calls avoided    {_int(calls)}")
    lines.append(f"  Tokens kept out  {_fmt_tok(tokens)}")
    faster_s = float(payload.get("time_saved_seconds") or 0.0)
    if faster_s >= 60:
        lines.append(f"  Faster (est.)    ~{fmt_duration(faster_s)}   (fewer round-trips)")
    routing_total = float((payload.get("live") or {}).get("routing_saved_usd") or 0.0)
    if routing_total > 0:
        lines.append(f"  Routing saved    {_fmt_usd(routing_total)}   (model routing · included in Saved)")

    if breakdown:
        has_routing = any(float((breakdown.get(k) or {}).get("routing") or 0.0) > 0 for k in ("1D", "7D", "30D"))
        lines.append("")
        header = f"  {'By window':<12}{'calls':>8}{'saved':>11}{'tokens':>10}"
        if has_routing:
            header += f"{'routing':>11}"
        lines.append(header)
        for key, label in (("1D", "1 day"), ("7D", "7 days"), ("30D", "30 days")):
            w = breakdown.get(key) or {}
            row = f"    {label:<10}{_int(w.get('calls')):>8}{_fmt_usd(float(w.get('usd') or 0.0)):>11}{_fmt_tok(int(w.get('tokens') or 0)):>10}"
            if has_routing:
                row += f"{_fmt_usd(float(w.get('routing') or 0.0)):>11}"
            lines.append(row)

    sub = payload.get("subscription") or {}
    plan = str(sub.get("plan") or "").strip()
    if plan:
        status = str(sub.get("status") or "").strip().lower()
        lines.append("")
        lines.append(f"  Plan  {plan}" + (f" ({status})" if status else ""))

    note = str(payload.get("local_note") or "").strip()
    if note:
        lines.append(f"  {note}")

    lines.append("")
    lines.append("  detail: atelier savings detail      json: atelier savings --json")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rotating statusline segment
# ---------------------------------------------------------------------------

_SEGMENT_INTERVAL_S: int = 5  # seconds before advancing to the next frame


# Spend cache freshness: an active session's transcript changes every turn, so
# re-pricing it on every statusline render would be wasteful. Reuse cached
# per-turn costs for this long even when the transcript mtime moved.
_SPEND_CACHE_TTL_S = 60.0

# In-memory TTL cache of windowed savings results: the statusline refreshes
# every ~5s but savings data only changes when a tool call completes. Keyed
# on (days, root_str); entries expire after this many seconds. Expiry never
# triggers a sessions/** scan — totals are re-derived from the persisted
# day-bucketed aggregate (see reconcile_savings_aggregate) and live rows are
# folded in O(1) on write via _bump_historical_savings_cache.
_HISTORICAL_SAVINGS_CACHE_TTL_S: float = 60.0
_historical_savings_cache: dict[
    tuple[int, str], tuple[float, tuple[float, int, int, int, float, float, float, float, int, int]]
] = {}


def _transcript_turn_costs(transcript_path: str | Path) -> list[tuple[float, float]]:
    """Per-assistant-turn ``(epoch_ts, cost_usd)`` for a transcript + subagents.

    Summing the costs reconciles with :func:`read_transcript_stats`'s
    ``est_cost_usd`` (same per-turn, per-model pricing incl. the long-context
    premium); the timestamps let callers window spend the *same* per-turn way
    savings rows are windowed, instead of attributing a whole session's cost at
    its end. Usage is de-duplicated by message id across the main and subagent
    transcripts, matching the stats parser.
    """
    from datetime import datetime

    p = Path(transcript_path)
    if not p.exists():
        return []
    sources: list[Path] = [p, *_subagent_transcripts(p)]
    seen_ids: set[str] = set()
    lc_thresholds: dict[str, int] = {}
    out: list[tuple[float, float]] = []
    for source in sources:
        try:
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                entry = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            msg = entry.get("message") or {}
            if not isinstance(msg, dict):
                continue
            usage = msg.get("usage") or {}
            if not isinstance(usage, dict):
                continue
            in_t = int(usage.get("input_tokens", 0) or 0)
            out_t = int(usage.get("output_tokens", 0) or 0)
            cr_t = int(usage.get("cache_read_input_tokens", 0) or 0)
            cw_t = int(usage.get("cache_creation_input_tokens", 0) or 0)
            if not (in_t or out_t or cr_t or cw_t):
                continue
            msg_id = str(msg.get("id") or "").strip()
            if msg_id:
                if msg_id in seen_ids:
                    continue
                seen_ids.add(msg_id)
            try:
                dt = datetime.fromisoformat(str(entry.get("timestamp") or "").replace("Z", "+00:00"))
                ts_epoch = (dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt).timestamp()
            except (ValueError, TypeError, OSError, OverflowError):
                continue
            model = str(msg.get("model") or entry.get("model") or "").strip()
            cache_creation = usage.get("cache_creation") or {}
            cw1_t = (
                int(cache_creation.get("ephemeral_1h_input_tokens", 0) or 0) if isinstance(cache_creation, dict) else 0
            )
            cw1_t = min(cw1_t, cw_t)
            threshold = _long_context_threshold(model, lc_thresholds) if model else 0
            long_ctx = bool(threshold and (in_t + cr_t + cw_t) > threshold)
            cost = estimate_cost_usd(
                model_id=resolve_model_id(model),
                input_tokens=in_t,
                output_tokens=out_t,
                cache_read_tokens=cr_t,
                cache_write_tokens=cw_t - cw1_t,
                cache_write_1h_tokens=cw1_t,
                long_context=long_ctx,
            )
            out.append((ts_epoch, cost))
    return out


def _claude_transcript_index() -> dict[str, Path]:
    """Map ``session_id -> newest main transcript`` from ONE projects/ listing.

    :func:`claude_transcript_candidates` globs the whole projects tree per
    session; resolving hundreds of sessions that way is O(sessions x projects).
    One listing serves them all.
    """
    claude_root = os.environ.get("CLAUDE_CONFIG_DIR") or os.environ.get("CLAUDE_HOME") or ""
    projects = Path(claude_root) / "projects" if claude_root else Path.home() / ".claude" / "projects"
    index: dict[str, Path] = {}
    if not projects.is_dir():
        return index
    try:
        mtimes: dict[str, float] = {}
        for p in projects.glob("*/*.jsonl"):
            try:
                mt = p.stat().st_mtime
            except OSError:
                continue
            if p.stem not in index or mt > mtimes[p.stem]:
                index[p.stem] = p
                mtimes[p.stem] = mt
    except OSError:
        pass
    return index


def _session_turn_costs(
    session_id: str,
    root: Path,
    *,
    sidecar: Path | None = None,
    transcript: Path | None = None,
) -> list[tuple[float, float]] | None:
    """Per-turn ``(epoch_ts, cost)`` for *session_id*, from the transcript.

    Lets callers window spend the same per-turn way savings rows are windowed,
    so a session that ran across several days contributes only its in-window
    turns to each window (fixing the "7d spend == 1d spend" artifact of
    end-of-session attribution). Pairs are cached in
    ``sessions/<id>/spend_cache.json`` keyed on transcript mtime (short TTL for
    the still-growing active session) so no render path re-parses the
    transcript. Returns ``None`` when no transcript exists so the caller can
    fall back to ``session_end`` rows.

    ``sidecar``/``transcript`` let a bulk caller that already resolved the
    session's paths skip the per-session directory globs.
    """
    if not session_id:
        return None
    if transcript is None:
        candidates = claude_transcript_candidates(session_id)
        if not candidates:
            return None
        transcript = candidates[0]
    try:
        mtime = transcript.stat().st_mtime
    except OSError:
        return None
    now = time.time()
    base = sidecar if sidecar is not None else _find_savings_sidecar(session_id, root)
    cache_path = base.with_name("spend_cache.json")
    turns: list[Any] | None = None
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            isinstance(cached, dict)
            and isinstance(cached.get("turns"), list)
            and (
                cached.get("transcript_mtime") == mtime
                or (now - float(cached.get("computed_at") or 0)) < _SPEND_CACHE_TTL_S
            )
        ):
            turns = cached["turns"]
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        turns = None
    if turns is None:
        turns = [[ts, cost] for ts, cost in _transcript_turn_costs(transcript)]
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic rename: concurrent writers (statusline render + stop hook)
            # then always leave a complete JSON snapshot, never a torn file.
            tmp_path = cache_path.with_name(f"{cache_path.name}.{os.getpid()}.tmp")
            tmp_path.write_text(
                json.dumps({"transcript_mtime": mtime, "computed_at": now, "turns": turns}),
                encoding="utf-8",
            )
            os.replace(tmp_path, cache_path)
        except OSError:
            pass
    out: list[tuple[float, float]] = []
    for item in turns:
        try:
            out.append((float(item[0]), float(item[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _bump_historical_savings_cache(row: dict[str, Any]) -> None:
    """Fold ONE just-appended savings row into every cached window total.

    O(1) alternative to full invalidation: appends are the hot path (every
    savings-bearing tool call), and invalidating forced a full sessions/**
    re-scan on the next statusline render — O(store) every ~5s during active
    work. Cached entries keep their timestamps, so the normal TTL still
    refreshes them from the day-bucketed aggregate within
    ``_HISTORICAL_SAVINGS_CACHE_TTL_S`` to pick up other processes' writes.
    Mirrors :func:`_fold_session_file`'s per-row math. Spend/carry/carry_tokens
    are session_end-only (never bumped here — TTL-refreshed from the aggregate
    instead), matching :func:`_fold_session_file`.
    """
    if not _historical_savings_cache:
        return
    if str(row.get("kind") or "") == "routing":
        routing_inc = max(0.0, float(row.get("usd") or 0.0))
        if routing_inc <= 0:
            return
        for cache_key, (cached_ts, val) in list(_historical_savings_cache.items()):
            u, t, c, turns, spend, carry, routing, read_usd, read_tok, carry_tok = val
            _historical_savings_cache[cache_key] = (
                cached_ts,
                (u, t, c, turns, spend, carry, routing + routing_inc, read_usd, read_tok, carry_tok),
            )
        return
    pt, usd, calls, calls_usd, up = _price_savings_row(row)
    row_usd = usd + calls_usd
    row_tok = pt + up
    if row_usd <= 0 and row_tok <= 0 and calls <= 0:
        return
    turns_inc = 1 if (row_usd > 0 or row_tok > 0) else 0
    read_usd_inc = 0.0
    read_tok_inc = 0
    if not row.get("kind") and _is_read_lever(str(row.get("tool") or "")):
        rt = max(0, int(row.get("tokens") or row.get("tokens_saved") or 0))
        if rt <= 2_000_000:
            read_tok_inc = rt
            read_usd_inc = max(0.0, float(row.get("cost_saved_usd") or 0.0))
    for cache_key, (cached_ts, val) in list(_historical_savings_cache.items()):
        u, t, c, turns, spend, carry, routing, read_usd, read_tok, carry_tok = val
        _historical_savings_cache[cache_key] = (
            cached_ts,
            (
                u + row_usd,
                t + row_tok,
                c + calls,
                turns + turns_inc,
                spend,
                carry,
                routing,
                read_usd + read_usd_inc,
                read_tok + read_tok_inc,
                carry_tok,
            ),
        )


def _read_historical_savings(
    days: int, root: Path
) -> tuple[float, int, int, int, float, float, float, float, int, int]:
    """Windowed savings/spend for ONE trailing window — blocking surface.

    Used by :func:`aggregate_window_savings` (CLI breakdown, web Savings page,
    reports): reconciles any session ledgers the persisted aggregate has not
    folded yet before answering, so explicit surfaces always reflect the
    on-disk ledger. The statusline path uses the non-blocking
    :func:`_read_historical_savings_many` directly.

    Returns (savings_usd, tokens_saved, calls_saved, turns_saved, spend_usd,
    carry_usd, routing_usd, read_saved_usd, read_saved_tokens, carry_tokens).
    """
    return _read_historical_savings_many((int(days),), root, block=True)[int(days)]


# ---------------------------------------------------------------------------
# Persisted day-bucketed savings aggregate
# ---------------------------------------------------------------------------
#
# ``<root>/savings_aggregate.json`` holds, PER SESSION, day-bucketed sums of
# (usd, tok, calls, turns, spend, carry_usd, routing, read_usd, read_tok,
# carry_tokens) plus the ledger's (mtime, size) at fold time and a store-wide
# watermark / first_ts. Rolling 1d/7d/30d windows are answered by summing <=
# days+1 buckets — no request path ever re-reads sessions/**. The service
# daemon reconciles periodically (see atelier.core.service.savings_reconcile);
# a session process folds only the ledgers written since the persisted
# aggregate last saw them. Per-session buckets make the fold idempotent:
# re-folding a changed ledger REPLACES its old contribution instead of double
# counting.

_SAVINGS_AGGREGATE_FILENAME = "savings_aggregate.json"
# v3: day buckets grew read_usd/read_tok (columns 7-8, classified with the
# shared _is_read_lever rule) and carry_tokens (column 9, from the
# session_end row's carry_tokens field); version gate forces a from-scratch
# rebuild of any persisted v1/v2 aggregate on first read.
_SAVINGS_AGGREGATE_VERSION = 3

_aggregate_lock = threading.Lock()
_aggregate_state: dict[str, dict[str, Any]] = {}  # root_str -> aggregate
_aggregate_refreshed_at: dict[str, float] = {}  # root_str -> last reconcile time
_aggregate_refreshing: set[str] = set()  # roots with an in-flight refresh


def _day_key(epoch: float) -> str:
    """UTC calendar-day bucket key (ISO date) for *epoch*."""
    st = time.gmtime(epoch)
    return f"{st.tm_year:04d}-{st.tm_mon:02d}-{st.tm_mday:02d}"


def _row_epoch(ts_str: str) -> float | None:
    from datetime import datetime

    try:
        # Rows are stamped naive-UTC (datetime.utcnow); pin the zone so the
        # epoch matches time.time() exactly.
        return datetime.fromisoformat(ts_str).replace(tzinfo=UTC).timestamp()
    except (ValueError, TypeError, OSError, OverflowError):
        return None


def _fold_session_file(p: Path, root: Path, transcript_for: Callable[[str], Path | None]) -> dict[str, list[float]]:
    """Day-bucketed ``[usd, tok, calls, turns, spend, carry_usd, routing,
    read_usd, read_tok, carry_tokens]`` for ONE ledger.

    Savings rows are priced via :func:`_price_savings_row` (the shared rule, so
    windowed totals reconcile exactly with the live statusline/stop-hook
    figure) and bucketed by row ts. Spend/carry keep whole-session attribution:
    the NEWEST ``session_end`` snapshot (cumulative per Stop fire — older
    snapshots are superseded, never summed) lands on its end day; a session
    without a usable snapshot — or one resumed after Stop, whose snapshot
    undercounts — is back-filled from the Claude transcript's per-turn costs,
    each on its turn day; the stale snapshot stays the fallback when the
    transcript is gone (it still beats reporting zero). Model-routing rows
    (``kind == "routing"``, ``usd`` field) fold into their own column; the
    composed :func:`aggregate_window_savings` still folds routing into its
    headline ``saved_usd`` (mirroring ``compute_savings_summary``), the same
    way ``read_usd`` below is tracked separately but always inside the total.
    Read/retrieval-side rows (plain per-tool-call rows — no ``kind`` — whose
    ``tool`` classifies as a read lever via :func:`_is_read_lever`, the same
    classifier :func:`_read_session_read_savings` uses for the per-session
    breakdown) additionally land in the read_usd/read_tok columns.
    """
    days: dict[str, list[float]] = {}

    def _bucket(ts: float) -> list[float]:
        return days.setdefault(_day_key(ts), [0.0] * 12)

    end_ts = 0.0
    end_spend = 0.0
    end_carry = 0.0
    end_carry_tokens = 0.0
    last_row_ts = 0.0
    with p.open(encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            ts = _row_epoch(str(row.get("ts", "")))
            if ts is None:
                continue
            if row.get("kind") == "session_end":
                if ts >= end_ts:
                    end_ts = ts
                    end_spend = float(row.get("est_cost_usd") or 0)
                    end_carry = float(row.get("carry_usd") or 0)
                    end_carry_tokens = float(row.get("carry_tokens") or 0)
                continue
            last_row_ts = max(last_row_ts, ts)
            if row.get("kind") == "routing":
                # Routing credit rides its own column — never folded into the
                # context-savings usd (kept separate on every surface).
                _bucket(ts)[6] += max(0.0, float(row.get("usd") or 0.0))
                continue
            pt, row_usd, row_calls, row_calls_usd, up = _price_savings_row(row)
            row_usd += row_calls_usd
            row_tok = pt + up
            b = _bucket(ts)
            b[0] += row_usd
            b[1] += row_tok
            b[2] += row_calls
            if row_usd > 0 or row_tok > 0:
                b[3] += 1
            if str(row.get("kind") or "") == "output_style":
                # Telegraphic output-compression breakdown (already inside
                # b[0]/b[1]; tracked separately so the daily rollup can push it).
                b[10] += max(0, int(row.get("tokens") or 0))
                b[11] += max(0.0, float(row.get("cost_saved_usd") or 0.0))
            if not row.get("kind") and _is_read_lever(str(row.get("tool") or "")):
                rt = max(0, int(row.get("tokens") or row.get("tokens_saved") or 0))
                if rt <= 2_000_000:
                    b[7] += max(0.0, float(row.get("cost_saved_usd") or 0.0))
                    b[8] += rt
    if end_ts > 0 and (end_carry > 0 or end_carry_tokens > 0):
        # Attribute the session's carry credit to the day each saved token was
        # generated (proportional to that day's saved-token share) rather than
        # dumping the whole-session total on the end day — otherwise every
        # trailing window inherits the same lump and 1d/7d/30d don't scale.
        # carry_tokens (col 9) rides the same per-day share as carry_usd.
        total_tok = sum(v[1] for v in days.values())
        if total_tok > 0:
            for v in days.values():
                if v[1] > 0:
                    share = v[1] / total_tok
                    v[5] += end_carry * share
                    v[9] += end_carry_tokens * share
        else:
            b = _bucket(end_ts)
            b[5] += end_carry
            b[9] += end_carry_tokens
    # A savings row NEWER than the last snapshot means the session resumed
    # after Stop — the snapshot undercounts, so prefer the transcript.
    resumed = last_row_ts > end_ts > 0.0
    if end_spend > 0 and not resumed:
        _bucket(end_ts)[4] += end_spend
        return days
    # Index miss (other-host session, subagent-keyed sidecar, transcript
    # layout drift) falls back to the per-session candidates() glob inside
    # _session_turn_costs, so a layout the index cannot see degrades to the
    # slow path instead of silently dropping the session's spend.
    session_turns = _session_turn_costs(p.parent.name, root, sidecar=p, transcript=transcript_for(p.parent.name))
    if session_turns is not None:
        for turn_ts, cost in session_turns:
            _bucket(turn_ts)[4] += cost
    elif end_spend > 0:
        _bucket(end_ts)[4] += end_spend
    return days


def _scan_savings_files(root: Path) -> list[tuple[Path, float, int]]:
    """``(path, mtime, size)`` for every session ledger under ``sessions/``.

    Manual scandir walk that stops descending below any SESSION directory —
    recognized by its ledger or sidecar marker files: ledgers live AT the
    session-dir leaf (legacy flat and dated ``YYYY/MM/DD[/host]/sid`` layouts
    alike), and everything beneath (``raw/`` captures etc.) is session-payload
    noise. A recursive ``**`` glob walked 36k+ directories on a grown store;
    this visits one scandir per date/host dir plus one per session.
    """
    markers = {"savings.jsonl", "meta.json", "run.json", "events.jsonl"}
    out: list[tuple[Path, float, int]] = []
    stack = [root / "sessions"]
    while stack:
        d = stack.pop()
        subdirs: list[Path] = []
        is_session_dir = False
        try:
            with os.scandir(d) as it:
                for entry in it:
                    try:
                        if entry.name in markers and entry.is_file():
                            is_session_dir = True
                            if entry.name == "savings.jsonl":
                                st = entry.stat()
                                out.append((Path(entry.path), st.st_mtime, st.st_size))
                        elif entry.is_dir(follow_symlinks=False):
                            subdirs.append(Path(entry.path))
                    except OSError:
                        continue
        except OSError:
            continue
        if not is_session_dir:
            stack.extend(subdirs)
    return out


def _session_key(p: Path, root: Path) -> str:
    try:
        return str(p.parent.relative_to(root / "sessions"))
    except ValueError:
        return str(p.parent)


def _empty_savings_aggregate() -> dict[str, Any]:
    return {"version": _SAVINGS_AGGREGATE_VERSION, "watermark": 0.0, "first_ts": 0.0, "sessions": {}}


def _fold_files_into(agg: dict[str, Any], files: list[tuple[Path, float, int]], root: Path) -> None:
    """Fold *files* into *agg*, replacing each session's previous buckets."""
    index: dict[str, Path] | None = None

    def _transcript_for(sid: str) -> Path | None:
        # One projects/ listing serves every session that needs the transcript
        # spend fallback (instead of one glob per session), built lazily.
        nonlocal index
        if index is None:
            index = _claude_transcript_index()
        return index.get(sid)

    for p, mtime, size in files:
        try:
            day_totals = _fold_session_file(p, root, _transcript_for)
        except OSError:
            continue
        agg["sessions"][_session_key(p, root)] = {"mtime": mtime, "size": size, "days": day_totals}
        agg["watermark"] = max(float(agg["watermark"]), mtime)
        first = float(agg["first_ts"])
        # The oldest ledger only ever gets older: fold min().
        agg["first_ts"] = mtime if first <= 0 else min(first, mtime)


def recompute_savings_aggregate(root: Path) -> dict[str, Any]:
    """From-scratch rebuild of the aggregate — the correctness oracle.

    Reads EVERY ``sessions/**/savings.jsonl``; only the daemon reconciler and
    tests use it. Request paths always start from the persisted aggregate.
    """
    agg = _empty_savings_aggregate()
    _fold_files_into(agg, _scan_savings_files(root), root)
    return agg


def _savings_aggregate_path(root: Path) -> Path:
    return root / _SAVINGS_AGGREGATE_FILENAME


def _load_persisted_aggregate(root: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(_savings_aggregate_path(root).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None
    if (
        not isinstance(data, dict)
        or data.get("version") != _SAVINGS_AGGREGATE_VERSION
        or not isinstance(data.get("sessions"), dict)
    ):
        return None
    return data


def _persist_savings_aggregate(root: Path, agg: dict[str, Any]) -> None:
    """Atomic rename: concurrent writers (daemon + session refresh threads)
    always leave a complete snapshot. Any complete snapshot is safe — readers
    re-fold whatever it has not seen yet."""
    path = _savings_aggregate_path(root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(agg), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        logging.debug("savings aggregate persist failed", exc_info=True)


def reconcile_savings_aggregate(root: Path, *, full: bool = False) -> dict[str, Any]:
    """Fold ledgers the persisted aggregate has not seen, persist, return it.

    Incremental: only savings files whose (mtime, size) differ from the folded
    entry are re-read; re-folding replaces that session's buckets, so the fold
    is idempotent. ``full=True`` rebuilds from scratch (self-healing oracle
    pass). The service daemon calls this periodically; session processes call
    it lazily (in a background thread on the statusline path).
    """
    if full:
        agg = recompute_savings_aggregate(root)
        _persist_savings_aggregate(root, agg)
        return agg
    agg = _load_persisted_aggregate(root) or _empty_savings_aggregate()
    sessions = agg["sessions"]
    stale: list[tuple[Path, float, int]] = []
    for p, mtime, size in _scan_savings_files(root):
        entry = sessions.get(_session_key(p, root))
        if not isinstance(entry, dict) or entry.get("mtime") != mtime or entry.get("size") != size:
            stale.append((p, mtime, size))
    if stale:
        _fold_files_into(agg, stale, root)
        _persist_savings_aggregate(root, agg)
    return agg


def _window_from_aggregate(
    agg: dict[str, Any], days: int, now: float
) -> tuple[float, int, int, int, float, float, float, float, int, int]:
    """Trailing-*days* totals from the day buckets (<= days+1 buckets summed;
    day granularity rounds the window start down to 00:00 UTC of the cutoff day).

    Returns (usd, tok, calls, turns, spend, carry_usd, routing, read_usd,
    read_tok, carry_tokens).
    """
    cutoff_day = _day_key(max(0.0, now - days * 86_400))
    totals = [0.0] * 10
    for entry in agg.get("sessions", {}).values():
        for day, vals in (entry.get("days") or {}).items():
            if day >= cutoff_day:
                for i, v in enumerate(vals[:10]):
                    totals[i] += float(v)
    return (
        totals[0],
        int(totals[1]),
        int(totals[2]),
        int(totals[3]),
        totals[4],
        totals[5],
        totals[6],
        totals[7],
        int(totals[8]),
        int(totals[9]),
    )


def _get_aggregate_state(root: Path) -> dict[str, Any]:
    """In-memory aggregate for *root* (one small JSON read on first touch).

    A store with no persisted aggregate yet is bootstrapped synchronously ONCE
    (fresh stores are small; grown stores always have the file) and persisted
    so no later process ever rebuilds it.
    """
    root_str = str(root)
    with _aggregate_lock:
        cached = _aggregate_state.get(root_str)
    if cached is not None:
        return cached
    agg = _load_persisted_aggregate(root)
    if agg is not None:
        with _aggregate_lock:
            _aggregate_state.setdefault(root_str, agg)
            # Persisted state may lag the ledgers — a refresh is due immediately.
            _aggregate_refreshed_at.setdefault(root_str, 0.0)
            return _aggregate_state[root_str]
    agg = reconcile_savings_aggregate(root)
    with _aggregate_lock:
        _aggregate_state[root_str] = agg
        _aggregate_refreshed_at[root_str] = time.time()
    return agg


def _refresh_aggregate_state(root: Path) -> None:
    """Reconcile, swap the in-memory aggregate, and rewrite cached window totals."""
    root_str = str(root)
    try:
        agg = reconcile_savings_aggregate(root)
        now = time.time()
        with _aggregate_lock:
            _aggregate_state[root_str] = agg
            _aggregate_refreshed_at[root_str] = now
        for days, key_root in list(_historical_savings_cache):
            if key_root == root_str:
                _historical_savings_cache[(days, key_root)] = (now, _window_from_aggregate(agg, days, now))
    except Exception:
        logging.exception("Recovered refreshing savings aggregate")
    finally:
        with _aggregate_lock:
            _aggregate_refreshing.discard(root_str)


def _maybe_refresh_aggregate(root: Path, *, block: bool) -> None:
    root_str = str(root)
    now = time.time()
    with _aggregate_lock:
        if now - _aggregate_refreshed_at.get(root_str, 0.0) < _HISTORICAL_SAVINGS_CACHE_TTL_S:
            return
        if root_str in _aggregate_refreshing:
            return
        _aggregate_refreshing.add(root_str)
    if block:
        _refresh_aggregate_state(root)
    else:
        threading.Thread(
            target=_refresh_aggregate_state, args=(root,), name="atelier-savings-aggregate", daemon=True
        ).start()


def _read_historical_savings_many(
    days_list: tuple[int, ...], root: Path, *, block: bool = False
) -> dict[int, tuple[float, int, int, int, float, float, float, float, int, int]]:
    """Windowed savings for SEVERAL trailing windows from the day-bucketed
    aggregate — never a sessions/** scan on the caller's thread (except a
    store's one-time bootstrap).

    ``block=True`` (CLI / API / report surfaces): reconcile unfolded ledgers
    synchronously first so explicit surfaces always reflect the on-disk ledger.
    ``block=False`` (statusline sidecar): stale-while-revalidate — serve the
    current totals (live rows already folded in O(1) by
    :func:`_bump_historical_savings_cache`) and refresh in a background thread
    that rewrites the cache for the next read.
    """
    now = time.time()
    root_str = str(root)
    results: dict[int, tuple[float, int, int, int, float, float, float, float, int, int]] = {}
    missing: list[int] = []
    for days in days_list:
        cached = _historical_savings_cache.get((days, root_str))
        if cached is not None and now - cached[0] < _HISTORICAL_SAVINGS_CACHE_TTL_S:
            results[days] = cached[1]
        else:
            missing.append(days)
    if not missing:
        return results
    _get_aggregate_state(root)
    _maybe_refresh_aggregate(root, block=block)
    with _aggregate_lock:
        agg = _aggregate_state[root_str]
    for days in missing:
        expired = _historical_savings_cache.get((days, root_str))
        if not block and expired is not None:
            # Expired-but-present entries carry live O(1) bumps the aggregate
            # may not have folded yet — keep serving them; the background
            # refresh rewrites them from the fresh aggregate when it lands.
            result = expired[1]
        else:
            result = _window_from_aggregate(agg, days, now)
        _historical_savings_cache[(days, root_str)] = (now, result)
        results[days] = result
    return results


@dataclass
class WindowSavings:
    """Realized savings over a trailing window, from ``sessions/*/savings.jsonl``."""

    saved_usd: float = 0.0
    tokens_saved: int = 0
    calls_saved: int = 0
    turns: int = 0
    spend_usd: float = 0.0
    carry_usd: float = 0.0
    carry_tokens: int = 0
    # Model-routing credit — tracked separately AND folded into saved_usd,
    # mirroring SavingsSummary.routing_saved_usd (compute_savings_summary
    # folds it into saved_usd the same way; see that dataclass's doc comment).
    routing_usd: float = 0.0
    # Read/retrieval-side savings (search_read, cached_read, scoped_recall
    # levers, classified by :func:`_is_read_lever`) — tracked separately AND
    # included in saved_usd, mirroring SavingsSummary.read_saved_usd/
    # read_saved_tokens.
    read_saved_usd: float = 0.0
    read_saved_tokens: int = 0

    @property
    def time_saved_seconds(self) -> float:
        """Estimated wall-clock time saved over this window, from avoided tool
        round-trips (windows do not break out output tokens; see class doc)."""
        return estimate_time_saved_seconds(calls_avoided=self.calls_saved)

    @property
    def would_have_cost_usd(self) -> float:
        """What the window would have cost without the realized savings."""
        return self.saved_usd + self.spend_usd

    @property
    def saved_pct(self) -> float:
        """Realized savings as a share of the would-have-cost baseline."""
        whc = self.would_have_cost_usd
        return round(100.0 * self.saved_usd / whc, 2) if whc > 0 else 0.0

    @property
    def total_saved_usd(self) -> float:
        """Canonical "Total saved" figure: saved_usd (already folds in
        read/routing) plus carry_usd (deliberately excluded from saved_usd
        itself — a recurring future-turns credit, not a one-time bucket).
        Mirrors SavingsSummary.total_saved_usd; every surface that wants a
        single headline total should read this property instead of
        re-deriving the sum."""
        return self.saved_usd + self.carry_usd


def aggregate_window_savings(root: str | Path, *, days: int) -> WindowSavings:
    """Realized savings over the last *days* from the canonical per-session ledger.

    Single source of truth for every windowed savings surface (CLI breakdown,
    web Savings page, dashboard).  Built from ``sessions/*/savings.jsonl`` and
    priced with :func:`_price_savings_row`, so it always reconciles with the
    statusline/stop-hook live total. Routing is folded into ``saved_usd`` here
    (composition time), not inside the per-row day buckets, mirroring how
    :func:`compute_savings_summary` folds ``routing_saved_usd`` into its
    ``saved_usd`` — both still expose the routing figure separately too.
    """
    usd, tok, calls, turns, spend, carry, routing, read_usd, read_tok, carry_tok = _read_historical_savings(
        int(days), Path(root)
    )
    return WindowSavings(
        saved_usd=round(usd + routing, 6),
        tokens_saved=int(tok),
        calls_saved=int(calls),
        turns=int(turns),
        spend_usd=round(spend, 6),
        carry_usd=round(carry, 6),
        carry_tokens=int(carry_tok),
        routing_usd=round(routing, 6),
        read_saved_usd=round(read_usd, 6),
        read_saved_tokens=int(read_tok),
    )


def aggregate_savings_since_day(
    root: str | Path, *, since_day: str, today: str
) -> tuple[dict[str, float | int], str | None]:
    """Sum realized savings, across every session, for each UTC calendar day
    that fully elapsed strictly after ``since_day`` and before ``today``
    (today's bucket is still accumulating, so it is never included).

    Reads the same day-bucketed aggregate that backs every other windowed
    savings surface (:func:`aggregate_window_savings`, the CLI, the web
    Savings page), reconciling any session ledgers it has not folded yet
    first. Used by the public rollup's daily flush so each calendar day of a
    user's local savings is reported to the public counters exactly once, no
    matter how often (or rarely) the flush actually runs.

    Returns ``(totals, last_day)`` where ``last_day`` is the newest complete
    day folded in (``None`` if there was nothing new to report).
    """
    agg = reconcile_savings_aggregate(Path(root))
    totals: dict[str, float | int] = {
        "saved_usd": 0.0,
        "tokens_saved": 0,
        "calls_avoided": 0,
        "turn_count": 0,
        "est_cost_usd": 0.0,
        "carry_usd": 0.0,
        "output_saved_tokens": 0,
        "output_saved_usd": 0.0,
    }
    last_day: str | None = None
    for entry in agg.get("sessions", {}).values():
        for day, vals in (entry.get("days") or {}).items():
            if day >= today or day <= since_day:
                continue
            totals["saved_usd"] = float(totals["saved_usd"]) + float(vals[0])
            totals["tokens_saved"] = int(totals["tokens_saved"]) + int(vals[1])
            totals["calls_avoided"] = int(totals["calls_avoided"]) + int(vals[2])
            totals["turn_count"] = int(totals["turn_count"]) + int(vals[3])
            totals["est_cost_usd"] = float(totals["est_cost_usd"]) + float(vals[4])
            totals["carry_usd"] = float(totals["carry_usd"]) + float(vals[5])
            totals["output_saved_tokens"] = int(totals["output_saved_tokens"]) + int(vals[10] if len(vals) > 10 else 0)
            totals["output_saved_usd"] = float(totals["output_saved_usd"]) + float(vals[11] if len(vals) > 11 else 0.0)
            if last_day is None or day > last_day:
                last_day = day
    return totals, last_day


def _first_savings_ts(root: Path) -> float:
    """Oldest folded savings-ledger mtime, from the aggregate (0.0 when none).

    A stored field of the persisted aggregate: the oldest session only ever
    gets older (folded with ``min()``), so no glob runs on the read path.
    """
    return float(_get_aggregate_state(root).get("first_ts") or 0.0)


def _read_review_verdict(session_id: str, root: Path) -> str:
    """Return 'NEEDS_FIX' when an unconsumed review verdict exists, else ''."""
    review_log = root / "reviews" / f"{session_id}.jsonl"
    if not review_log.exists():
        return ""
    try:
        with review_log.open(encoding="utf-8") as fh:
            for raw in fh:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    row = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict) and not row.get("consumed") and row.get("verdict") == "NEEDS_FIX":
                    return "NEEDS_FIX"
    except OSError:
        pass
    return ""


def _get_frame_index(state_path: Path, num_frames: int) -> int:
    """Return the current frame index, advancing the rolling counter every _SEGMENT_INTERVAL_S."""
    counter = 0
    now = time.time()
    # Cold start (no state file yet): no time has elapsed to justify an
    # advance, so frame 0 -- the live cost/savings frame -- must render
    # first. Defaulting last_ts to epoch 0 made `now - last_ts` huge on the
    # very first call, incrementing straight past frame 0 for every new
    # session before it ever got the chance to render.
    last_ts = now
    try:
        if state_path.exists():
            state = json.loads(state_path.read_text(encoding="utf-8"))
            counter = int(state.get("counter", 0))
            last_ts = float(state.get("ts", 0))
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        pass
    if now - last_ts >= _SEGMENT_INTERVAL_S:
        counter += 1
        try:
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(json.dumps({"counter": counter, "ts": now}), encoding="utf-8")
        except OSError:
            pass
    return counter % max(1, num_frames)


def _resolve_atelier_root(atelier_root: str | Path | None) -> Path:
    env_root = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT") or ""
    if atelier_root is not None:
        return Path(atelier_root)
    if env_root:
        return Path(env_root)
    return Path.home() / ".atelier"


def savings_frames(
    session_id: str = "",
    *,
    atelier_root: str | Path | None = None,
    live_cost_usd: float = 0.0,
    live_in_tok: int = 0,
    live_cache_tok: int = 0,
    live_out_tok: int = 0,
    no_color: bool = False,
) -> list[str]:
    """Return EVERY pre-formatted statusline frame, weighted, ready to print.

    Each entry is a complete drop-in segment (icon/separator prefix and the
    pinned review verdict included). The MCP sidecar writes all of them (one
    per line) so statusline.sh can rotate by wall clock BETWEEN writes instead
    of freezing on whichever single frame was current at write time.

    Frames (non-empty only):
      0  cost + I/C/O breakdown, then total saved (realized+output+routing+carry, usage-gated) w/ recycle+carry+output+routing breakdown (weighted 3x)
      1  1-day historical savings
      2  7-day historical savings
      3  30-day historical savings
      4  status tip / update notice
    """
    root = _resolve_atelier_root(atelier_root)

    # ANSI palette (mirrors statusline.sh)
    if no_color:
        C_BRAND = C_DIM = C_GREEN = C_COST = C_RED = C_RESET = ""
    else:
        C_BRAND = "\033[1;38;2;168;85;247m"  # purple  — carry / ♻
        C_DIM = "\033[2;38;2;200;200;200m"  # dim grey — separators, tips
        C_GREEN = "\033[1;38;2;72;199;116m"  # green   — savings / ↓
        C_COST = "\033[38;2;255;180;70m"  # amber   — cost / ↑
        C_RED = "\033[1;38;2;255;99;71m"  # red     — NEEDS_FIX
        C_RESET = "\033[0m"
    # Dim / used between label-value pairs on text-only frames.
    SEP = f"{C_DIM}|{C_RESET}"

    summary = compute_savings_summary(session_id, atelier_root=root)
    summary.status_text = _resolve_status_text(root)

    # Prefer transcript-derived cumulative I/C/O when available.
    if summary.display_input_tokens > 0 or summary.display_cache_tokens > 0 or summary.display_output_tokens > 0:
        eff_in = summary.display_input_tokens
        eff_cache = summary.display_cache_tokens
        eff_out = summary.display_output_tokens
    else:
        eff_in = live_in_tok
        eff_cache = live_cache_tok
        eff_out = live_out_tok

    # Historical savings — one sessions/** pass fills all three windows.
    hist = _read_historical_savings_many((1, 7, 30), root)
    usd_1d, tok_1d, calls_1d, _turns_1d, spend_1d, carry_1d, routing_1d, *_rest_1d = hist[1]
    usd_7d, tok_7d, calls_7d, _turns_7d, spend_7d, carry_7d, routing_7d, *_rest_7d = hist[7]
    usd_30d, tok_30d, calls_30d, _turns_30d, spend_30d, carry_30d, routing_30d, *_rest_30d = hist[30]
    first_ts = _first_savings_ts(root)
    days_active = (time.time() - first_ts) / 86_400 if first_ts > 0 else 0.0

    # --- Build frames as (has_icon, content) tuples.
    # has_icon=True  → ↑/↓/♻ leads the frame; no separator needed before it.
    # has_icon=False → plain text; SEP is prepended so it doesn't abut ctx% directly.
    frames: list[tuple[bool, str]] = []

    # Frame 0: $cost(I:.. C:.. O:..) ↓ $<total saved>(I:input C:cache K:carry O:output R:routing)
    # Single-letter labels: I=input, C=cache, K=carry/keep, O=output, R=routing.
    # cost segment uses I/C/O for usage totals; savings uses same letters for
    # savings breakdown — visually separated by the ↓ token.
    in_f, cache_f, out_f = _fmt_tok(eff_in), _fmt_tok(eff_cache), _fmt_tok(eff_out)
    has_usage = eff_in > 0 or eff_cache > 0
    display_cost = summary.est_cost_usd if summary.est_cost_usd > 0 else live_cost_usd
    combined = f"{C_COST}{_fmt_usd(display_cost)}{C_DIM}(I:{in_f} C:{cache_f} O:{out_f}){C_RESET}"
    if has_usage:
        # total_saved_usd already folds read/output/routing into saved_usd and
        # adds carry_usd on top (see the property docstring) -- use it
        # directly instead of hand-summing components. Summing saved_usd +
        # output_saved_usd here previously double-counted output, since
        # output_saved_usd is already inside saved_usd.
        total_saved = summary.total_saved_usd
        combined += f" {C_GREEN}↓ {_fmt_usd(total_saved)}{C_DIM}(I:{_fmt_tok(summary.ctx_saved)}"
        if summary.carry_usd >= 0.001:
            combined += f" K:{_fmt_tok(summary.carry_tokens)}"
        if summary.output_saved_usd >= 0.001:
            combined += f" O:{_fmt_tok(summary.output_saved_tokens)}"
        if summary.input_saved_usd >= 0.001:
            combined += f" C:{_fmt_tok(summary.input_saved_tokens)}"
        if summary.routing_saved_usd > 0:
            combined += f" R:{_fmt_usd(summary.routing_saved_usd)}"
        combined += f"){C_RESET}"
        # "faster" — estimated wall-clock saved, the speed companion to $saved.
        if summary.time_saved_seconds >= 60:
            combined += f" {C_BRAND}⚡ {fmt_duration(summary.time_saved_seconds)} faster{C_RESET}"
    frames.append((True, combined))

    def _hist_frame(label: str, usd: float, tok: int, calls: int, spend: float, carry: float, routing: float) -> str:
        """Format: label: ↑ $spent ↓ $total_saved · N turns (spend + carry + routing combined)."""
        # Combine all savings into one number: realized savings + carry + routing
        total_saved = usd + carry + routing
        dot = f" {C_DIM}·{C_RESET} "

        money: list[str] = []
        if spend > 0:
            money.append(f"{C_COST}↑ {_fmt_usd(spend)}{C_RESET}")
        if total_saved > 0:
            money.append(f"{C_GREEN}↓ {_fmt_usd(total_saved)}{C_RESET}")

        body = " ".join(money)
        if calls > 0:
            body += dot + f"{C_DIM}{calls} turns avoided{C_RESET}"
        _t = estimate_time_saved_seconds(calls_avoided=calls)
        if _t >= 60:
            body += dot + f"{C_BRAND}~{fmt_duration(_t)} faster{C_RESET}"

        return f"{C_DIM}{label}{C_RESET} {body}"

    # Frame 2: 1-day window — spent · saved · tokens less · calls fewer
    if usd_1d > 0 or carry_1d > 0 or spend_1d > 0 or routing_1d > 0:
        frames.append((False, _hist_frame("1d:", usd_1d, tok_1d, calls_1d, spend_1d, carry_1d, routing_1d)))

    # Frame 3: 7-day window — only after ≥1 day of usage.
    if (usd_7d > 0 or carry_7d > 0 or spend_7d > 0 or routing_7d > 0) and days_active >= 1:
        frames.append((False, _hist_frame("7d:", usd_7d, tok_7d, calls_7d, spend_7d, carry_7d, routing_7d)))

    # Frame 4: 30-day window — only after ≥7 days of usage.
    if (usd_30d > 0 or carry_30d > 0 or spend_30d > 0 or routing_30d > 0) and days_active >= 7:
        frames.append((False, _hist_frame("30d:", usd_30d, tok_30d, calls_30d, spend_30d, carry_30d, routing_30d)))

    # Frame 6: status tip / update notice (text-only)
    # Backtick-wrapped tool names are highlighted in brand purple; rest is dim.
    if summary.status_text:
        frames.append((False, _colorize_tip(summary.status_text, C_DIM, C_BRAND, C_RESET)))

    # Login nudge frame (free/unauthenticated only): fold a sign-in reminder
    # into the rotating frames so free users see it on every cycle, instead of
    # the once-a-day statusline.sh marker (which this replaces). Same auth
    # signal as the MCP FeatureLocked path -- ATELIER_AUTH_TOKEN env, then
    # <root>/auth_token (see licensing/store.py load_auth_token). Read from the
    # resolved `root` directly (not load_auth_token, which keys off
    # default_store_root and would ignore the atelier_root param). The
    # anonymous local-trial marker is a separate free-mode file and does NOT
    # count as signed in.
    try:
        _signed_in = bool(os.environ.get("ATELIER_AUTH_TOKEN", "").strip())
        if not _signed_in:
            _tok_file = root / "auth_token"
            _signed_in = _tok_file.exists() and bool(_tok_file.read_text(encoding="utf-8").strip())
    except OSError:
        _signed_in = True  # unknown auth state -> never nag
    if not _signed_in:
        frames.append((False, f"{C_DIM}not signed in -- {C_BRAND}/atelier login{C_DIM} to unlock Pro{C_RESET}"))

    # Frame 0 (cost+savings+carry) gets 3 slots at 5s each = ~15s; others get 5s
    # each. Weighting frame 0 higher than this made the line feel static — the
    # render path refreshes every 5-10s at best (sidecar rate-limit / cache TTL),
    # so a 30s hold on one frame read as "not rotating at all".
    weighted = [frames[0]] * 3 + frames[1:] if frames else frames

    # Review verdict: pinned — appended to every frame, never rotated away.
    pin = ""
    if session_id:
        verdict = _read_review_verdict(session_id, root)
        if verdict == "NEEDS_FIX":
            pin = f" {SEP} {C_RED}review: NEEDS_FIX{C_RESET}"

    # Icon-led frames (↑ ↓ ♻) are their own visual separator.
    # Text-only frames get SEP prepended so they don't abut ctx% directly.
    return [f" {content}{pin}" if has_icon else f" {SEP} {content}{pin}" for has_icon, content in weighted]


def savings_segment(
    session_id: str = "",
    *,
    atelier_root: str | Path | None = None,
    live_cost_usd: float = 0.0,
    live_in_tok: int = 0,
    live_cache_tok: int = 0,
    live_out_tok: int = 0,
    no_color: bool = False,
) -> str:
    """Return ONE pre-formatted rotating statusline frame.

    Subprocess/CLI path (``atelier savings --segment``): builds all frames via
    :func:`savings_frames` and picks the current one from the shared rolling
    counter at ``<root>/statusline_frame_state.json`` (advances every
    ``_SEGMENT_INTERVAL_S`` seconds). The MCP sidecar path writes the full
    frame list instead and lets statusline.sh rotate by wall clock.
    """
    frames = savings_frames(
        session_id,
        atelier_root=atelier_root,
        live_cost_usd=live_cost_usd,
        live_in_tok=live_in_tok,
        live_cache_tok=live_cache_tok,
        live_out_tok=live_out_tok,
        no_color=no_color,
    )
    if not frames:
        return ""
    root = _resolve_atelier_root(atelier_root)
    idx = _get_frame_index(root / "statusline_frame_state.json", len(frames))
    return frames[idx]
