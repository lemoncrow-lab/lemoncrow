"""Routing REPLAY benchmark - haiku via Claude Code CLI (no API key required).

Runs inside Claude Code using the CLI's own credentials. For each turn the
router would downgrade (actual=sonnet, recommended=haiku), we:

  1. Reconstruct the conversation context as readable text from the export.
  2. Call ``claude -p "..." --model <haiku> --no-session-persistence``
     asking haiku to declare what tool it would call next.
  3. Parse haiku's JSON response and compare to what sonnet actually did.

No ANTHROPIC_API_KEY required - Claude Code's existing auth is used.

What is measured (true counterfactual decisions)
-------------------------------------------------
  tool_match       - haiku chose the same tool as sonnet
  input_similarity - for matched tools, Jaccard similarity of inputs
  output_token_ratio - haiku_output_tokens / sonnet_output_tokens
                       (<0.5 = haiku was much terser)

Quality labels per turn
-----------------------
  match        - same tool, similar input (similarity >= 0.7)
  partial      - same tool, different input (0.3-0.7)
  diverge      - same tool, very different input (< 0.3)
  tool_mismatch - haiku chose a different tool
  parse_error  - haiku responded but JSON could not be parsed

Limitations
-----------
- Context is formatted as text (not structured tool_use), so haiku may
  respond slightly differently than in a live session.
- The Atelier plugin system prompt is included in every call, adding
  ~35K tokens of cache overhead. Cost is ~$0.01-0.03 per turn.
- Haiku's decision is isolated (single turn), not a full session replay.
  A wrong turn N decision compounding into N+1 is not captured.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from atelier.core.capabilities.model_routing.router import ModelRouter, ModelTier

# ---------------------------------------------------------------------------
# Model / tier mapping
# ---------------------------------------------------------------------------

_MODEL_TO_TIER: dict[str, ModelTier] = {
    "claude-haiku-4-5": "cheap",
    "claude-haiku-4-6": "cheap",
    "claude-sonnet-4-5": "medium",
    "claude-sonnet-4-6": "medium",
    "claude-sonnet-4.6": "medium",
    "claude-opus-4-5": "expensive",
    "claude-opus-4-7": "expensive",
    "claude-opus-4.7": "expensive",
}
_TIER_RANK: dict[ModelTier, int] = {"cheap": 0, "medium": 1, "expensive": 2}
_TIER_MODELS: dict[ModelTier, str] = {
    "cheap": "claude-haiku-4-5",
    "medium": "claude-sonnet-4-6",
    "expensive": "claude-opus-4-7",
}
_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _model_tier(model: str) -> ModelTier:
    m = (model or "").lower().strip()
    for key, tier in _MODEL_TO_TIER.items():
        if key in m:
            return tier
    return "medium"


# ---------------------------------------------------------------------------
# Context formatter
# ---------------------------------------------------------------------------

_MAX_TOOL_RESULT_CHARS = 200
_MAX_TEXT_CHARS = 250


def _fmt_content(content: Any) -> list[str]:
    """Format a message content block into readable text lines."""
    lines: list[str] = []
    if isinstance(content, str):
        if content.strip():
            lines.append(f"[User]: {content[:_MAX_TEXT_CHARS]}")
        return lines
    if not isinstance(content, list):
        return lines
    for b in content:
        if not isinstance(b, dict):
            continue
        btype = b.get("type", "")
        if btype == "text":
            text = str(b.get("text", "")).strip()
            if text:
                lines.append(f"[Assistant]: {text[:_MAX_TEXT_CHARS]}")
        elif btype == "tool_use":
            name = b.get("name", "")
            inp = b.get("input") or {}
            summary = ", ".join(f"{k}={str(v)[:50]}" for k, v in list(inp.items())[:3])
            lines.append(f"[Called {name}({summary})]")
        elif btype == "tool_result":
            rc = b.get("content", "")
            if isinstance(rc, list):
                rc = " ".join(str(x.get("text", "")) if isinstance(x, dict) else str(x) for x in rc)
            lines.append(f"[ToolResult]: {str(rc)[:_MAX_TOOL_RESULT_CHARS]}")
        # Skip thinking blocks - not useful for text context
    return lines


def _build_context_text(
    raw_events: list[dict[str, Any]],
    up_to_event_idx: int,
    last_n_lines: int = 30,
) -> str:
    """Convert export events to a readable text conversation snippet."""
    lines: list[str] = []
    last_fp: tuple[int, int, int, int] | None = None

    for i, ev in enumerate(raw_events):
        if i >= up_to_event_idx:
            break
        t = ev.get("type", "")
        msg = ev.get("message") or {}
        content = msg.get("content") or []

        if t == "user":
            last_fp = None
            lines.extend(_fmt_content(content))
        elif t == "assistant":
            usage = msg.get("usage") or {}
            inp = int(usage.get("input_tokens", 0))
            cc = int(usage.get("cache_creation_input_tokens", 0))
            cr = int(usage.get("cache_read_input_tokens", 0))
            out = int(usage.get("output_tokens", 0))
            fp = (inp, cc, cr, out)
            if fp == last_fp:
                continue
            last_fp = fp
            model = str(msg.get("model") or "")
            if not model or model == "<synthetic>":
                continue
            lines.extend(_fmt_content(content))

    return "\n".join(lines[-last_n_lines:])


# ---------------------------------------------------------------------------
# Haiku call via claude CLI
# ---------------------------------------------------------------------------

_AVAILABLE_TOOLS = "Read, Bash, Edit, Write, Grep, Glob, WebFetch, WebSearch, Agent"


def _call_haiku(
    context_text: str,
    haiku_model: str,
    timeout: int = 60,
) -> tuple[str, str, int, int]:
    """Call haiku via claude CLI and return (tool_name, raw_json, input_tokens, output_tokens).

    The prompt asks haiku to declare its NEXT tool call in JSON. We do NOT
    execute the tool - this is a decision benchmark, not an execution benchmark.
    """
    prompt = (
        "You are a Claude Code agent in the middle of a coding session. "
        "Based on the conversation context below, decide what the NEXT single "
        "tool call should be.\n\n"
        f"Available tools: {_AVAILABLE_TOOLS}\n\n"
        "Conversation context (most recent turns):\n"
        f"{context_text}\n\n"
        "Reply with ONLY a valid JSON object (no markdown fences): "
        '{"tool": "<tool_name>", "input": {<params>}}'
    )

    try:
        result = subprocess.run(
            [
                "claude",
                "--model",
                haiku_model,
                "-p",
                prompt,
                "--output-format",
                "json",
                "--no-session-persistence",
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        data = json.loads(result.stdout)
        raw = str(data.get("result", ""))
        usage = data.get("usage") or {}
        inp_tok = int(usage.get("cache_creation_input_tokens", 0)) + int(usage.get("input_tokens", 0))
        out_tok = int(usage.get("output_tokens", 0))
        return raw, result.stderr, inp_tok, out_tok
    except subprocess.TimeoutExpired:
        return "", "timeout", 0, 0
    except Exception as exc:
        return "", str(exc)[:200], 0, 0


def _parse_tool_response(raw: str) -> tuple[str, dict[str, Any]]:
    """Extract (tool_name, input_dict) from haiku's text response."""
    # Try direct parse first
    raw = raw.strip()
    for attempt in (raw, *_JSON_FENCE.findall(raw)):
        try:
            obj = json.loads(attempt)
            if isinstance(obj, dict) and "tool" in obj:
                return str(obj.get("tool", "")), dict(obj.get("input") or {})
        except Exception:
            pass
    return "", {}


# ---------------------------------------------------------------------------
# Input similarity
# ---------------------------------------------------------------------------

_WS = re.compile(r"\s+")


def _jaccard(a: str, b: str) -> float:
    sa = set(_WS.split(a.lower().strip()))
    sb = set(_WS.split(b.lower().strip()))
    if not sa and not sb:
        return 1.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


def _input_similarity(
    tool: str,
    s_input: dict[str, Any],
    h_input: dict[str, Any],
) -> float:
    t = tool.lower()
    if t == "bash":
        return _jaccard(str(s_input.get("command", "")), str(h_input.get("command", "")))
    if t == "edit":
        file_match = 1.0 if s_input.get("file_path") == h_input.get("file_path") else 0.3
        old_sim = _jaccard(str(s_input.get("old_string", "")), str(h_input.get("old_string", "")))
        return 0.4 * file_match + 0.6 * old_sim
    if t == "read":
        return 1.0 if s_input.get("file_path") == h_input.get("file_path") else 0.2
    if t in ("grep", "glob"):
        return _jaccard(str(s_input.get("pattern", "")), str(h_input.get("pattern", "")))
    # Generic key overlap
    sk, hk = set(s_input), set(h_input)
    if not sk and not hk:
        return 1.0
    return len(sk & hk) / len(sk | hk) if (sk | hk) else 1.0


# ---------------------------------------------------------------------------
# Per-turn result
# ---------------------------------------------------------------------------


@dataclass
class TurnReplayResult:
    turn_index: int
    session_id: str
    tool_sonnet: str
    tool_haiku: str
    tool_match: bool
    input_similarity: float
    output_tokens_sonnet: int
    output_tokens_haiku: int
    output_token_ratio: float
    parse_error: bool
    input_tokens_sent: int

    @property
    def quality_label(self) -> str:
        if self.parse_error:
            return "parse_error"
        if not self.tool_match:
            return "tool_mismatch"
        if self.input_similarity >= 0.7:
            return "match"
        if self.input_similarity >= 0.3:
            return "partial"
        return "diverge"

    def to_dict(self) -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "tool_sonnet": self.tool_sonnet,
            "tool_haiku": self.tool_haiku,
            "tool_match": self.tool_match,
            "input_similarity": round(self.input_similarity, 3),
            "output_tokens_sonnet": self.output_tokens_sonnet,
            "output_tokens_haiku": self.output_tokens_haiku,
            "output_token_ratio": round(self.output_token_ratio, 3),
            "quality_label": self.quality_label,
            "parse_error": self.parse_error,
            "input_tokens_sent": self.input_tokens_sent,
        }


# ---------------------------------------------------------------------------
# Session runner
# ---------------------------------------------------------------------------


@dataclass
class SessionReplayResult:
    session_id: str
    actual_model: str
    downtiered_turns_found: int
    turns_replayed: int
    tool_match_rate: float
    avg_input_similarity: float
    avg_output_token_ratio: float
    quality_label_counts: dict[str, int]
    parse_errors: int
    total_haiku_input_tokens: int
    total_haiku_output_tokens: int
    turn_results: list[TurnReplayResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "actual_model": self.actual_model,
            "downtiered_turns_found": self.downtiered_turns_found,
            "turns_replayed": self.turns_replayed,
            "tool_match_rate": round(self.tool_match_rate, 3),
            "avg_input_similarity": round(self.avg_input_similarity, 3),
            "avg_output_token_ratio": round(self.avg_output_token_ratio, 3),
            "quality_labels": self.quality_label_counts,
            "parse_errors": self.parse_errors,
            "total_haiku_input_tokens": self.total_haiku_input_tokens,
            "total_haiku_output_tokens": self.total_haiku_output_tokens,
            "turns": [t.to_dict() for t in self.turn_results],
        }


def _run_session(
    path: Path,
    router: ModelRouter,
    haiku_model: str,
    *,
    max_turns: int | None,
    context_lines: int,
    delay: float,
    verbose_cb: Any = None,
) -> SessionReplayResult | None:
    raw_events: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    raw_events.append(json.loads(raw))
                except Exception:
                    continue
    except Exception:
        return None

    last_fp: tuple[int, int, int, int] | None = None
    dominant_model = "claude-sonnet-4-6"
    turn_results: list[TurnReplayResult] = []
    turns_found = 0

    for ev_idx, ev in enumerate(raw_events):
        if ev.get("type") == "user":
            last_fp = None
            continue
        if ev.get("type") != "assistant":
            continue

        msg = ev.get("message") or {}
        usage = msg.get("usage") or {}
        inp = int(usage.get("input_tokens", 0))
        cc = int(usage.get("cache_creation_input_tokens", 0))
        cr = int(usage.get("cache_read_input_tokens", 0))
        out = int(usage.get("output_tokens", 0))
        fp = (inp, cc, cr, out)
        if fp == last_fp:
            continue
        last_fp = fp

        model = str(msg.get("model") or "")
        synthetic = model == "<synthetic>" or not model
        if synthetic or (inp == 0 and cc == 0 and cr == 0 and out == 0):
            continue
        if model and not synthetic:
            dominant_model = model

        content = msg.get("content") or []
        tool_uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if not tool_uses:
            continue

        actual_tier = _model_tier(model)
        tool_name = str(tool_uses[0].get("name", ""))
        tool_input = dict(tool_uses[0].get("input") or {})

        rec = router.score(tool_name, "", {})
        if _TIER_RANK[rec.tier] >= _TIER_RANK[actual_tier]:
            continue  # not downtiered

        turns_found += 1
        if max_turns is not None and len(turn_results) >= max_turns:
            continue

        # Build context text and call haiku
        ctx = _build_context_text(raw_events, ev_idx, last_n_lines=context_lines)
        if not ctx.strip():
            continue

        if delay > 0:
            time.sleep(delay)

        raw_resp, _stderr, h_inp_tok, h_out_tok = _call_haiku(ctx, haiku_model)
        h_tool, h_input = _parse_tool_response(raw_resp)
        parse_err = not h_tool

        tool_match = h_tool.lower() == tool_name.lower() and not parse_err
        sim = _input_similarity(tool_name, tool_input, h_input) if tool_match else 0.0
        ratio = (h_out_tok / out) if out > 0 and h_out_tok > 0 else 0.0

        tr = TurnReplayResult(
            turn_index=len(turn_results),
            session_id=path.stem,
            tool_sonnet=tool_name or "(none)",
            tool_haiku=h_tool or "(none)",
            tool_match=tool_match,
            input_similarity=sim,
            output_tokens_sonnet=out,
            output_tokens_haiku=h_out_tok,
            output_token_ratio=ratio,
            parse_error=parse_err,
            input_tokens_sent=h_inp_tok,
        )
        turn_results.append(tr)
        if verbose_cb:
            verbose_cb(tr)

    if not turn_results:
        return None

    matched = [t for t in turn_results if t.tool_match]
    match_rate = len(matched) / len(turn_results)
    avg_sim = sum(t.input_similarity for t in matched) / len(matched) if matched else 0.0
    ratios = [t.output_token_ratio for t in turn_results if t.output_token_ratio > 0]
    avg_ratio = sum(ratios) / len(ratios) if ratios else 0.0
    labels: dict[str, int] = {}
    for t in turn_results:
        labels[t.quality_label] = labels.get(t.quality_label, 0) + 1

    return SessionReplayResult(
        session_id=path.stem,
        actual_model=dominant_model,
        downtiered_turns_found=turns_found,
        turns_replayed=len(turn_results),
        tool_match_rate=match_rate,
        avg_input_similarity=avg_sim,
        avg_output_token_ratio=avg_ratio,
        quality_label_counts=labels,
        parse_errors=sum(1 for t in turn_results if t.parse_error),
        total_haiku_input_tokens=sum(t.input_tokens_sent for t in turn_results),
        total_haiku_output_tokens=sum(t.output_tokens_haiku for t in turn_results),
        turn_results=turn_results,
    )


# ---------------------------------------------------------------------------
# Main runner
# ---------------------------------------------------------------------------


def run_routing_replay_bench(
    corpus_dir: Path,
    *,
    max_sessions: int | None = None,
    max_turns_per_session: int | None = 5,
    context_lines: int = 30,
    haiku_model: str = "claude-haiku-4-5",
    rate_limit_delay: float = 0.5,
    verbose: bool = False,
) -> dict[str, Any]:
    """Run the routing replay benchmark using the claude CLI.

    No API key required - uses Claude Code's existing credentials.

    Parameters
    ----------
    corpus_dir:
        Directory with claude/*.jsonl session exports.
    max_sessions:
        Cap sessions. None = all.
    max_turns_per_session:
        Cap haiku calls per session. None = all downtiered turns.
    context_lines:
        How many recent context lines to include in each haiku prompt.
    haiku_model:
        Haiku model alias for --model flag.
    rate_limit_delay:
        Seconds between CLI calls.
    verbose:
        Print each turn result as it completes.
    """
    search_dir = corpus_dir / "claude" if (corpus_dir / "claude").is_dir() else corpus_dir

    candidates = sorted(search_dir.glob("*.jsonl"), key=lambda p: -p.stat().st_size)

    router = ModelRouter(
        cheap_model=_TIER_MODELS["cheap"],
        medium_model=_TIER_MODELS["medium"],
        expensive_model=_TIER_MODELS["expensive"],
    )

    session_results: list[SessionReplayResult] = []
    sessions_skipped = 0

    def _verbose_cb(t: TurnReplayResult) -> None:
        if verbose:
            print(
                f"  [{t.quality_label}] sonnet={t.tool_sonnet} haiku={t.tool_haiku}"
                + (f" sim={t.input_similarity:.2f}" if t.tool_match else "")
            )

    for path in candidates:
        if max_sessions is not None and len(session_results) >= max_sessions:
            break
        if verbose:
            print(f"Session: {path.stem[:40]}...")
        result = _run_session(
            path,
            router,
            haiku_model,
            max_turns=max_turns_per_session,
            context_lines=context_lines,
            delay=rate_limit_delay,
            verbose_cb=_verbose_cb,
        )
        if result is None:
            sessions_skipped += 1
        else:
            session_results.append(result)

    _empty = {
        "benchmark": "replay-routing",
        "methodology": (
            "True counterfactual via claude CLI - no API key required. "
            "Context formatted as text (thinking blocks omitted). "
            "Haiku declares its next tool call; we compare to sonnet's actual choice. "
            "tool_match = same tool name. input_similarity = Jaccard on key inputs. "
            "parse_error = haiku returned unparseable JSON."
        ),
        "haiku_model": haiku_model,
        "sessions_benchmarked": 0,
        "sessions_skipped": sessions_skipped,
        "total_turns_replayed": 0,
        "tool_match_rate": 0.0,
        "avg_input_similarity": 0.0,
        "avg_output_token_ratio": 0.0,
        "quality_label_counts": {},
        "total_haiku_cost_usd": 0.0,
        "sessions": [],
        "generated_at": datetime.now(UTC).isoformat(),
    }

    if not session_results:
        return _empty

    n_sess = len(session_results)
    total_replayed = sum(r.turns_replayed for r in session_results)
    avg_match = sum(r.tool_match_rate for r in session_results) / n_sess
    avg_sim = sum(r.avg_input_similarity for r in session_results) / n_sess
    avg_ratio = sum(r.avg_output_token_ratio for r in session_results) / n_sess
    total_inp = sum(r.total_haiku_input_tokens for r in session_results)
    total_out = sum(r.total_haiku_output_tokens for r in session_results)

    labels: dict[str, int] = {}
    for r in session_results:
        for k, v in r.quality_label_counts.items():
            labels[k] = labels.get(k, 0) + v

    # Haiku pricing: $0.80/M input, $4/M output
    cost = total_inp * 0.80 / 1_000_000 + total_out * 4.00 / 1_000_000

    return {
        "benchmark": "replay-routing",
        "methodology": (
            "True counterfactual via claude CLI - no API key required. "
            "Context formatted as text (thinking blocks omitted). "
            "Haiku declares its next tool call; we compare to sonnet's actual choice. "
            "tool_match = same tool name. input_similarity = Jaccard on key inputs "
            "(Bash: command tokens, Edit: file + old_string, Read: file path). "
            "parse_error = haiku returned unparseable JSON."
        ),
        "haiku_model": haiku_model,
        "sessions_benchmarked": n_sess,
        "sessions_skipped": sessions_skipped,
        "total_turns_replayed": total_replayed,
        "tool_match_rate": round(avg_match, 3),
        "avg_input_similarity": round(avg_sim, 3),
        "avg_output_token_ratio": round(avg_ratio, 3),
        "quality_label_counts": labels,
        "total_haiku_input_tokens": total_inp,
        "total_haiku_output_tokens": total_out,
        "total_haiku_cost_usd": round(cost, 4),
        "sessions": [r.to_dict() for r in session_results],
        "generated_at": datetime.now(UTC).isoformat(),
    }
