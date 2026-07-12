"""Renderers for :mod:`session_replay` — terminal timeline and standalone HTML.

Both consume a :class:`~lemoncrow.core.capabilities.session_replay.Replay` and show
the full transcript with the grep→read loops struck out and the collapsing
``code_search`` call inserted. No data is fetched or recomputed here.
"""

from __future__ import annotations

import html
import re
from typing import Any

from lemoncrow.core.capabilities.prompt_compilation.tokens import approx_tokens
from lemoncrow.core.capabilities.session_replay import (
    Episode,
    Replay,
    _tool_name,
    estimate_savings,
)

# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _arg_summary(turn: dict[str, Any]) -> str:
    """A compact one-line description of a tool call's input."""
    kind = turn.get("kind")
    name = _tool_name(turn) or turn.get("kind", "tool")
    args = turn.get("arguments") or {}
    if kind == "file_edit":
        return f"{name}({turn.get('path') or ''})"
    if kind == "shell_command":
        return str(turn.get("content") or "").splitlines()[0][:120] if turn.get("content") else name
    if isinstance(args, dict):
        # Prefer a human-meaningful scalar key.
        for key in ("pattern", "query", "content_regex", "file_path", "path", "command", "description"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                return f"{name}({val.strip()[:120]})"
        # List-valued inputs (e.g. lc read files=[...], edit edits=[...]).
        for key in ("files", "edits", "symbol", "paths"):
            val = args.get(key)
            summ = _summarize_value(val)
            if summ:
                return f"{name}({summ})"
        # Generic fallback: first non-empty value of any key.
        for val in args.values():
            summ = _summarize_value(val)
            if summ:
                return f"{name}({summ})"
    return f"{name}(…)"


def _summarize_value(val: Any) -> str:
    """Compact one-line string for a scalar or list arg value."""
    if isinstance(val, str):
        return val.strip()[:120]
    if isinstance(val, (int, float, bool)):
        return str(val)
    if isinstance(val, dict):
        path = val.get("path") or val.get("file_path")
        return str(path)[:120] if path else ""
    if isinstance(val, list) and val:
        items = [_item_label(v) for v in val]
        items = [i for i in items if i]
        if not items:
            return ""
        shown = ", ".join(items[:3])
        if len(items) > 3:
            shown += f", +{len(items) - 3} more"
        return shown[:120]
    return ""


def _item_label(v: Any) -> str:
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, dict):
        return str(v.get("path") or v.get("file_path") or v.get("new_string") or "").strip()[:60]
    return ""


def _ann_tokens(a: dict[str, Any]) -> tuple[int, int, int]:
    """(before, after, saved) token counts for a compaction annotation.

    Prefers stored ``*_tokens`` keys; falls back to a ~4-chars/token estimate
    from the ``*_chars`` counts so older annotations still read in tokens."""
    if "before_tokens" in a or "after_tokens" in a:
        b = int(a.get("before_tokens", 0) or 0)
        af = int(a.get("after_tokens", 0) or 0)
        saved = int(a.get("tokens_omitted", max(0, b - af)) or 0)
        return b, af, saved
    b = int(a.get("before_chars", 0) or 0) // 4
    af = int(a.get("after_chars", 0) or 0) // 4
    return b, af, max(0, b - af)


def _episodes_after(replay: Replay) -> dict[int, Episode]:
    return {e.after_index: e for e in replay.episodes}


def _turns_saved(replay: Replay) -> int:
    return replay.summary.calls_saved if replay.summary else 0


# --------------------------------------------------------------------------- #
# Terminal renderer
# --------------------------------------------------------------------------- #

_RESET = "\x1b[0m"
_DIM = "\x1b[2m"
_STRIKE = "\x1b[9m"
_GREEN = "\x1b[32m"
_GREY = "\x1b[90m"  # bright-black: "dead"/removed
_ORANGE = "\x1b[33m"  # replaced marker
_BOLD = "\x1b[1m"


# Distinct terminal colors per message kind.
_KIND_COLOR: dict[str, str] = {
    "user_message": "\x1b[94m",  # bright blue
    "agent_message": "",  # default text
    "thinking": "\x1b[90m",  # grey
    "tool_call": "\x1b[35m",  # magenta
    "file_edit": "\x1b[33m",  # amber
    "shell_command": "\x1b[36m",  # cyan
    "subagent_event": "\x1b[94m",
    "todo_write": "\x1b[90m",
}


def _dur(seconds: float) -> str:
    """Human duration via the canonical formatter (single source of truth)."""
    try:
        from lemoncrow.core.capabilities.savings_summary import fmt_duration

        return str(fmt_duration(float(seconds)))
    except Exception:  # noqa: BLE001
        s = max(0.0, float(seconds))
        return f"{s:.0f}s" if s < 90 else f"{s / 60:.1f}m"


def _money(value: float) -> str:
    """$ amount: 2 decimals at/above $1, 4 below (sub-cent costs stay visible)."""
    amount = float(value)
    return f"${amount:.2f}" if amount >= 1 else f"${amount:.4f}"


def render_text(replay: Replay, *, color: bool = True) -> str:
    def c(code: str, text: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    lines: list[str] = []
    s = replay.summary
    lines.append(c(_BOLD, f"Session {replay.session_id}  ({replay.host} · {replay.model or 'unknown model'})"))
    if replay.task:
        lines.append(f"  task: {replay.task.splitlines()[0][:100]}")
    if s:
        parts = []
        if s.search_calls_saved:
            parts.append(f"-{s.search_calls_saved} via {s.episode_count} search{'es' if s.episode_count != 1 else ''}")
        if s.batch_calls_saved:
            parts.append(f"-{s.batch_calls_saved} via {s.batch_count} batch{'es' if s.batch_count != 1 else ''}")
        detail = f"  ({', '.join(parts)})" if parts else ""
        sav = estimate_savings(replay)
        lines.append(
            "  tool calls "
            + c(_BOLD, f"{s.total_tool_calls} → {s.kept_tool_calls}")
            + f" · {sav['calls_saved']} collapsed · {s.episode_count} search loops · {s.batch_count} batches"
            + c(_GREEN, detail)
        )
        head = "  " + c(_BOLD, f"cost {_money(sav['total_cost_usd'])}")
        if sav["saved_is_measured"]:
            # Ran with LemonCrow and has its own recorded savings -> show them.
            head += "     " + c(_GREEN + _BOLD, f"saved {_money(sav['saved_usd'])} ({sav['saved_pct']}%) measured")
            head += "     " + c(_GREEN + _BOLD, f"time saved {_dur(sav['time_saved_seconds'])}") + c(_DIM, " est")
        elif sav["ran_with_lemoncrow"]:
            # Ran with LemonCrow, no per-node savings. Only a subagent can point
            # at a parent; a top-level session just has nothing recorded.
            note = (
                " — savings counted on the parent session"
                if replay.is_subagent
                else " — savings not recorded for this session"
            )
            head += "     " + c(_GREEN + _BOLD, "ran with LemonCrow") + c(_DIM, note)
        else:
            # Vanilla session -> estimated LemonCrow cost.
            head += (
                "     "
                + c(_GREEN + _BOLD, f"LemonCrow cost {_money(sav['lemoncrow_cost_usd'])} (-{sav['saved_pct']}%)")
                + c(_DIM, " est")
            )
            head += "     " + c(_GREEN + _BOLD, f"time saved {_dur(sav['time_saved_seconds'])}")
        lines.append(head)
        if not sav["saved_is_measured"] and not sav["ran_with_lemoncrow"]:
            lines.append(
                "  " + c(_DIM, "LemonCrow cost & saving are estimates — run `lc benchmark` for the measured A/B")
            )
    if not replay.turns:
        lines.append("  " + c(_ORANGE, "⚠ no turns parsed from this transcript — nothing to replay"))
    lines.append("  " + c(_DIM, "reconstructed from history — no model re-run, $0"))
    lines.append("")

    collapsed = set(replay.collapsed_indices)
    batched = set(replay.batched_indices)
    ep_after = _episodes_after(replay)
    batch_after = {b.after_index: b for b in replay.batches}

    for idx, turn in enumerate(replay.turns):
        body = _text_turn_body(turn)
        if body:
            if idx in collapsed:
                lines.append(c(_GREY + _STRIKE, "  ✗ ") + c(_GREY + _STRIKE, body) + c(_ORANGE, "  ← replaced"))
            elif idx in batched:
                lines.append(c(_DIM, f"  ⊕ {body}"))
            else:
                lines.append(c(_KIND_COLOR.get(str(turn.get("kind", "")), ""), body))
                lemoncrow = turn.get("lemoncrow")
                if isinstance(lemoncrow, dict):
                    for line in _text_lemoncrow(lemoncrow):
                        lines.append(c(_GREEN, line))
        if idx in ep_after:
            lines.extend(_text_collapse(ep_after[idx], color=color))
        if idx in batch_after:
            lines.extend(_text_batch(batch_after[idx], color=color))

    if replay.subagent_replays:
        lines.append("")
        lines.append(c(_BOLD, f"  Subagents ({len(replay.subagent_replays)}) — expandable in the HTML replay:"))
        for sr in replay.subagent_replays:
            st = sr.summary
            lines.append(
                c(_DIM, f"    ↳ {sr.session_id[:12]}: {st.total_turns if st else 0} turns, ")
                + c(_DIM, f"{st.kept_tool_calls if st else 0} tool calls")
            )
    return "\n".join(lines)


def _text_batch(batch: Any, *, color: bool) -> list[str]:
    def c(code: str, text: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    a = batch.live_result if isinstance(batch.live_result, dict) else {}
    call = a.get("call") or f"{batch.kind}([{len(batch.turn_indices)}])"
    return [
        c(_GREEN, f"  ┌─ ⊕ LemonCrow: {call} → 1 call"),
        c(_GREEN, f"  └─ batches {len(batch.turn_indices)} {batch.kind}s → 1, saving {batch.calls_saved}"),
        "",
    ]


def _text_lemoncrow(a: dict[str, Any]) -> list[str]:
    tool = a.get("tool", "tool")
    mode = a.get("mode")
    if mode not in ("real", "preview", "simulated"):
        return []
    if tool == "read":
        return [f"     ↳ lc read: {a.get('note', '')}"]
    if tool == "bash":
        if a.get("mode") == "simulated":
            b, af, saved = _ann_tokens(a)
            return [f"     ↳ lc bash [output compacted, not re-run]: {b:,} → {af:,} tokens (-{saved:,})"]
        extra = f" → {a['rewrite']}" if a.get("rewrite") else ""
        return [f"     ↳ lc bash [preview, not run]: {a.get('category') or 'classified'}{extra}"]
    if tool == "edit":
        return [
            f"     ↳ lc edit [preview, not written]: {a.get('path') or ''} ({a.get('changed_lines', 0)} lines)"
        ]
    if tool == "web_fetch":
        return [f"     ↳ lc web_fetch: {str(a.get('content') or '').splitlines()[0][:80]}"]
    if tool == "code_search":
        return _text_hits(a)
    return []


def _text_hits(a: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for h in (a.get("hits") or [])[:3]:
        rng = f"L{h.get('line')}-L{h.get('end_line')}" if h.get("line") else ""
        out.append(f"     ↳ lc code_search → {h.get('path')}:{rng}  {h.get('name') or ''} ({h.get('kind') or ''})")
    if a.get("matched_endpoint"):
        out.append(f"     ✓ matches where the loop landed: {a.get('endpoint')}")
    return out


def _text_turn_body(turn: dict[str, Any]) -> str:
    kind = str(turn.get("kind", ""))
    if kind == "user_message":
        return f"▸ user: {str(turn.get('content') or '').splitlines()[0][:100]}"
    if kind == "agent_message":
        text = str(turn.get("content") or "").strip().replace("\n", " ")
        return f"  ● {text[:110]}" if text else ""
    if kind == "thinking":
        text = str(turn.get("content") or "").strip().replace("\n", " ")
        return f"  · (thinking) {text[:90]}" if text else ""
    if kind in ("tool_call", "file_edit", "shell_command"):
        return f"  ⚙ {_arg_summary(turn)}"
    if kind == "todo_write":
        return f"  ☐ {turn.get('summary') or 'todo'}"
    if kind == "subagent_event":
        return f"  ↳ subagent: {str(turn.get('summary') or '')[:90]}"
    return ""


def _text_collapse(ep: Episode, *, color: bool) -> list[str]:
    def c(code: str, text: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    saved = ep.calls_saved
    detail = f"{ep.grep_count} grep{'s' if ep.grep_count != 1 else ''}"
    if ep.read_count:
        detail += f" + {ep.read_count} whole-file read{'s' if ep.read_count != 1 else ''}"
    out = [c(_GREEN, f'  ┌─ ⟳ LemonCrow: code_search("{ep.query}") → 1 call')]
    lemoncrow = ep.live_result if isinstance(ep.live_result, dict) else None
    if lemoncrow and lemoncrow.get("mode") == "real":
        for h in (lemoncrow.get("hits") or [])[:3]:
            rng = f"L{h.get('line')}-L{h.get('end_line')}" if h.get("line") else ""
            out.append(c(_GREEN, f"  │   → {h.get('path')}:{rng}  {h.get('name') or ''} ({h.get('kind') or ''})"))
        if lemoncrow.get("matched_endpoint"):
            out.append(c(_GREEN, f"  │   ✓ same file the loop landed on: {lemoncrow.get('endpoint')}"))
    out.append(c(_GREEN, f"  └─ collapses {len(ep.turn_indices)} calls ({detail}) → 1, saving {saved}"))
    out.append("")
    return out


# --------------------------------------------------------------------------- #
# HTML renderer (standalone document)
# --------------------------------------------------------------------------- #


def _esc(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def _html_turn(turn: dict[str, Any], mark: str | None, tool_results: dict[str, str]) -> str:
    kind = str(turn.get("kind", ""))
    cls = ("turn " + mark if mark in ("cut", "merged") else "turn") + f" k-{kind}"
    inner: list[str] = []

    if kind == "user_message":
        inner.append('<div class="role">user</div>')
        inner.append(f'<div class="say user">{_esc(turn.get("content"))}</div>')
    elif kind == "agent_message":
        inner.append('<div class="role">assistant</div>')
        inner.append(_telegraphic_html(str(turn.get("content") or "")))
    elif kind == "thinking":
        inner.append('<div class="role">thinking</div>')
        inner.append(f'<div class="say think">{_esc(turn.get("content"))}</div>')
    elif kind in ("tool_call", "file_edit", "shell_command"):
        tag = ""
        if mark == "cut":
            tag = '<span class="cut-tag">← replaced by code_search</span>'
        elif mark == "merged":
            tag = '<span class="merge-tag">batched</span>'
        inner.append(f'<div class="role">tool{tag}</div>')
        inner.append(_html_tool_call(turn, tool_results))
    elif kind == "todo_write":
        inner.append(f'<div class="say meta">☐ {_esc(turn.get("summary"))}</div>')
    elif kind == "subagent_event":
        name = _esc(turn.get("subagent_name") or turn.get("tool_name") or "subagent")
        summ = _esc(turn.get("summary") or "subagent")
        prompt = _esc(turn.get("content") or turn.get("subagent_description") or "")
        inner.append(f'<div class="role">subagent · {name}</div>')
        inner.append(
            f'<details class="sub-inline"><summary>{summ}</summary><pre class="an-out">{prompt}</pre></details>'
        )
    else:
        return ""

    return f'<div class="{cls}"><div class="body">{"".join(inner)}</div></div>'


def _html_tool_call(turn: dict[str, Any], tool_results: dict[str, str]) -> str:
    name = _esc(_tool_name(turn) or turn.get("kind"))
    summary = _esc(_arg_summary(turn))
    kind = turn.get("kind")
    parts = [f'<div class="call"><span class="tool">{name}</span> <span class="arg">{summary}</span>']

    if kind == "file_edit" and turn.get("diff"):
        parts.append(f'<pre class="diff">{_esc(turn.get("diff"))}</pre>')
    elif kind == "shell_command" and turn.get("content"):
        parts.append(f'<pre class="cmd">{_esc(turn.get("content"))}</pre>')

    tuid = str(turn.get("tool_use_id") or "")
    result = tool_results.get(tuid, "")
    if result:
        trimmed = result if len(result) <= 4000 else result[:4000] + "\n… (truncated)"
        parts.append(
            '<details class="out"><summary>output · '
            + f"{approx_tokens(result):,} tokens</summary><pre>{_esc(trimmed)}</pre></details>"
        )
    parts.append("</div>")
    lemoncrow = turn.get("lemoncrow")
    if isinstance(lemoncrow, dict):
        parts.append(_html_lemoncrow(lemoncrow))
    return "".join(parts)


# Function words the telegraphic register drops: articles, copulas, connectors,
# hedges, filler, pleasantries. Heuristic only — illustrates the compression, it
# is NOT what the model deterministically produces.
_FILLER_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "'s",
        "'re",
        "so",
        "thus",
        "therefore",
        "hence",
        "however",
        "moreover",
        "furthermore",
        "then",
        "but",
        "that",
        "which",
        "as",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "just",
        "really",
        "actually",
        "quite",
        "very",
        "rather",
        "somewhat",
        "likely",
        "roughly",
        "probably",
        "perhaps",
        "maybe",
        "basically",
        "essentially",
        "simply",
        "please",
        "sure",
        "okay",
        "ok",
        "well",
        "now",
        "let",
        "let's",
        "me",
        "i'll",
        "i'm",
        "we'll",
        "going",
        "go",
        "here",
        "there",
    }
)


def _telegraphic_html(text: str) -> str:
    """Grey out the filler/connector words the telegraphic register would drop.

    Heuristic illustration only — labelled as such. Content words stay; function
    words are struck so the compression is visible on the real recorded prose.
    """
    if not text.strip():
        return '<div class="say"></div>'
    chunks = re.findall(r"[A-Za-z][A-Za-z']*|[^A-Za-z]+", text)
    total = kept = 0
    out: list[str] = []
    for chunk in chunks:
        if chunk[:1].isalpha():
            total += 1
            if chunk.lower() in _FILLER_WORDS:
                out.append(f'<span class="filler">{_esc(chunk)}</span>')
            else:
                kept += 1
                out.append(_esc(chunk))
        else:
            out.append(_esc(chunk))
    dropped = total - kept
    body = f'<div class="say tele">{"".join(out)}</div>'
    if dropped <= 0 or total < 4:
        return f'<div class="say">{_esc(text)}</div>'
    note = (
        f'<div class="tele-note">telegraphic register would drop ~{dropped} filler/connector words '
        f"({total} &rarr; {kept}) · heuristic simulation only</div>"
    )
    return body + note


def _html_lemoncrow(a: dict[str, Any]) -> str:
    tool = _esc(a.get("tool", "tool"))
    mode = a.get("mode")
    if mode not in ("real", "preview", "simulated"):
        return ""
    tag = {"real": "REAL", "preview": "PREVIEW", "simulated": "COMPACTED"}.get(str(mode), "")
    tag_cls = "real" if mode in ("real", "simulated") else "preview"
    inner: list[str] = []
    if a.get("tool") == "code_search":
        inner.append(_html_hits(a))
    elif a.get("tool") == "read":
        inner.append(f'<div class="an-note">{_esc(a.get("note"))}</div>')
        if a.get("outline"):
            inner.append('<pre class="an-out">' + _esc("\n".join(a["outline"])) + "</pre>")
    elif a.get("tool") == "bash":
        if a.get("mode") == "simulated":
            b, af, _ = _ann_tokens(a)
            inner.append(
                f'<div class="an-note">output compacted {b:,} &rarr; '
                f"{af:,} tokens ({a.get('lines_omitted', 0)} lines omitted) &middot; not re-run</div>"
            )
            if a.get("output"):
                inner.append(f'<pre class="an-out">{_esc(a["output"])}</pre>')
        else:
            extra = f" &rarr; <code>{_esc(a['rewrite'])}</code>" if a.get("rewrite") else ""
            inner.append(
                f'<div class="an-note">{_esc(a.get("category") or "classified")}{extra} &middot; {_esc(a.get("note"))}</div>'
            )
    elif a.get("tool") == "edit":
        inner.append(f'<div class="an-note">{_esc(a.get("note"))}</div>')
        if a.get("diff"):
            inner.append(f'<pre class="an-out">{_esc(a["diff"])}</pre>')
    elif a.get("tool") == "web_fetch":
        inner.append(f'<pre class="an-out">{_esc(a.get("content"))}</pre>')
    return f'<div class="an"><div class="an-h">↳ lc {tool} <span class="an-tag {tag_cls}">{tag}</span></div>{"".join(inner)}</div>'


def _html_hits(a: dict[str, Any]) -> str:
    rows: list[str] = []
    for h in (a.get("hits") or [])[:5]:
        rng = f"L{h.get('line')}-L{h.get('end_line')}" if h.get("line") else ""
        rows.append(
            f'<div class="hit"><code>{_esc(h.get("path"))}:{_esc(rng)}</code> '
            f'<span class="sym">{_esc(h.get("name"))}</span> <span class="kind">{_esc(h.get("kind"))}</span></div>'
        )
    if not rows:
        rows.append('<div class="an-note">no index hit</div>')
    if a.get("matched_endpoint"):
        rows.append(f'<div class="match">✓ same file the loop landed on: <code>{_esc(a.get("endpoint"))}</code></div>')
    return "".join(rows)


def _html_collapse(ep: Episode) -> str:
    detail = f"{ep.grep_count} grep{'s' if ep.grep_count != 1 else ''}"
    if ep.read_count:
        detail += f" + {ep.read_count} whole-file read{'s' if ep.read_count != 1 else ''}"
    hits_html = ""
    lemoncrow = ep.live_result if isinstance(ep.live_result, dict) else None
    if lemoncrow and lemoncrow.get("mode") == "real":
        hits_html = f'<div class="hits">{_html_hits(lemoncrow)}</div>'
    elif lemoncrow and lemoncrow.get("mode") in ("error", "unavailable"):
        hits_html = (
            '<div class="an-note">(real code_search unavailable — run with --repo pointing at the indexed repo)</div>'
        )
    return (
        '<div class="collapse"><div class="atl-card">'
        '<div class="h">⟳ LemonCrow · code_search <span class="an-tag real">REAL</span></div>'
        f'<div class="search">code_search("{_esc(ep.query)}") → <b>1 call</b></div>'
        f"{hits_html}"
        f'<div class="collapses">Collapses <b>{len(ep.turn_indices)}</b> calls ({detail}) into one, '
        f"reaching the next real step <b>{ep.calls_saved}</b> turn(s) sooner.</div>"
        "</div></div>"
    )


def _html_session(replay: Replay) -> str:
    s = replay.summary
    collapsed = set(replay.collapsed_indices)
    batched = set(replay.batched_indices)
    ep_after = _episodes_after(replay)
    batch_after = {b.after_index: b for b in replay.batches}
    rows: list[str] = []
    for idx, turn in enumerate(replay.turns):
        mark = "cut" if idx in collapsed else "merged" if idx in batched else None
        cell = _html_turn(turn, mark, replay.tool_results)
        if cell:
            rows.append(cell)
        if idx in ep_after:
            rows.append(_html_collapse(ep_after[idx]))
        if idx in batch_after:
            rows.append(_html_batch(batch_after[idx]))

    tiles = ""
    if s:
        sav = estimate_savings(replay)
        cost_tile = f'<div class="tile hero"><div class="k">Cost</div><div class="v">{_money(sav["total_cost_usd"])}</div><div class="d before">this session</div></div>'
        time_tile = f'<div class="tile hero good"><div class="k">Time saved</div><div class="v">{_dur(sav["time_saved_seconds"])}</div><div class="d">est</div></div>'
        # Three states: measured savings / ran-with-LemonCrow (savings on parent) /
        # vanilla estimate.
        if sav["saved_is_measured"]:
            mid_tile = (
                f'<div class="tile hero good"><div class="k">Saved</div><div class="v">{_money(sav["saved_usd"])}</div>'
                f'<div class="d">&minus;{sav["saved_pct"]}% &middot; measured</div></div>'
            )
            hero = f'<div class="tiles hero-row">{cost_tile}{mid_tile}{time_tile}</div>'
        elif sav["ran_with_lemoncrow"]:
            note = (
                "savings counted on the parent session"
                if replay.is_subagent
                else "savings not recorded for this session"
            )
            mid_tile = (
                '<div class="tile hero good"><div class="k">LemonCrow</div>'
                '<div class="v" style="font-size:16px">ran with LemonCrow</div>'
                f'<div class="d before">{note}</div></div>'
            )
            hero = f'<div class="tiles hero-row two">{cost_tile}{mid_tile}</div>'
        else:
            mid_tile = (
                f'<div class="tile hero good"><div class="k">LemonCrow cost</div><div class="v">{_money(sav["lemoncrow_cost_usd"])}</div>'
                f'<div class="d">&minus;{sav["saved_pct"]}% &middot; est</div></div>'
            )
            time_est = f'<div class="tile hero good"><div class="k">Time saved</div><div class="v">{_dur(sav["time_saved_seconds"])}</div><div class="d">estimate</div></div>'
            hero = f'<div class="tiles hero-row">{cost_tile}{mid_tile}{time_est}</div>'
        tiles = (
            hero + '<div class="tiles">'
            f'<div class="tile"><div class="k">Tool calls</div><div class="v">{s.total_tool_calls} &rarr; {s.kept_tool_calls}</div><div class="d">&minus;{s.calls_saved} collapsed</div></div>'
            f'<div class="tile"><div class="k">Grep/read loops</div><div class="v">{s.episode_count}</div><div class="d before">&rarr; 1 code_search each</div></div>'
            f'<div class="tile"><div class="k">Read/edit batches</div><div class="v">{s.batch_count}</div><div class="d before">&rarr; 1 batched call each</div></div>'
            "</div>"
        )

    task = f'<div class="task"><b>Task:</b> {_esc(replay.task.splitlines()[0][:200])}</div>' if replay.task else ""
    subs_html = ""
    if replay.subagent_replays:
        items = []
        for sr in replay.subagent_replays:
            st = sr.summary
            head = (
                f"▸ subagent {sr.session_id[:12]} — {st.total_turns if st else 0} turns, "
                f"{st.kept_tool_calls if st else 0} tool calls"
            )
            items.append(f'<details class="subagent"><summary>{_esc(head)}</summary>{_html_session(sr)}</details>')
        subs_html = (
            f'<div class="subs"><div class="subs-h">Subagents ({len(replay.subagent_replays)}) '
            "— click to replay each</div>" + "".join(items) + "</div>"
        )
    sub_badge = (
        f'<span class="badge">{len(replay.subagent_replays)} subagents</span>' if replay.subagent_replays else ""
    )
    return (
        '<section class="session-block">'
        '<div class="session"><div class="row">'
        f'<span class="badge host">{_esc(replay.host)}</span>'
        f'<span class="sid">session {_esc(replay.session_id)}</span>'
        f'<span class="badge">{_esc(replay.model or "unknown model")}</span>'
        f'<span class="badge">{s.total_turns if s else 0} turns</span>'
        f"{sub_badge}"
        f"</div>{task}</div>"
        f"{tiles}"
        '<div class="legend">'
        '<span><i class="swatch cut"></i> eliminated by LemonCrow</span>'
        '<span><i class="swatch atl"></i> inserted one-shot search</span></div>'
        f'<div class="timeline">{"".join(rows)}</div>'
        f"{subs_html}"
        "</section>"
    )


def _html_batch(batch: Any) -> str:
    a = batch.live_result if isinstance(batch.live_result, dict) else {}
    call = _esc(a.get("call") or f"{batch.kind}([{len(batch.turn_indices)}])")
    files = a.get("files") or []
    files_html = "".join(f'<div class="hit"><code>{_esc(p)}</code></div>' for p in files[:8])
    return (
        '<div class="collapse"><div class="atl-card batch">'
        f'<div class="h">⊕ LemonCrow · {_esc(batch.kind)} batch <span class="an-tag real">1 CALL</span></div>'
        f'<div class="search">{call}</div>'
        f'<div class="hits">{files_html}</div>'
        f'<div class="collapses">Batches <b>{len(batch.turn_indices)}</b> {_esc(batch.kind)} calls into one, '
        f"saving <b>{batch.calls_saved}</b>.</div>"
        "</div></div>"
    )


def render_html(replays: list[Replay], *, title: str = "LemonCrow Session Replay") -> str:
    if not replays:
        body = '<p class="empty">No sessions found to replay.</p>'
    elif len(replays) == 1:
        body = _html_session(replays[0])
    else:
        body = _html_tabbed(replays)
    return _HTML_SHELL.replace("{{TITLE}}", _esc(title)).replace("{{BODY}}", body)


def _html_tabbed(replays: list[Replay]) -> str:
    """Render each session in its own tab when more than one is loaded."""
    tabs, panels = [], []
    for i, r in enumerate(replays):
        s = r.summary
        turns = s.total_turns if s else 0
        label = f"{_esc(r.host)} · {_esc(r.session_id[:8])}"
        active = " active" if i == 0 else ""
        tabs.append(
            f'<button class="tab-btn{active}" data-tab="{i}" onclick="selTab({i})">'
            f'{label} <span class="tab-n">{turns}t</span></button>'
        )
        panels.append(f'<div class="tab-panel{active}" data-panel="{i}">{_html_session(r)}</div>')
    return (
        f'<div class="tabs" role="tablist">{"".join(tabs)}</div>'
        f"{''.join(panels)}"
        "<script>function selTab(i){"
        "document.querySelectorAll('.tab-btn').forEach(b=>b.classList.toggle('active',b.dataset.tab==i));"
        "document.querySelectorAll('.tab-panel').forEach(p=>p.classList.toggle('active',p.dataset.panel==i));"
        "}</script>"
    )


_HTML_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
:root{--bg:#f4f7f5;--surface:#fff;--surface-2:#eef3f0;--text:#16201b;--muted:#5c6b63;--faint:#8a988f;--border:#dde6e0;--rail:#cdd8d1;--accent:#1a7f3c;--accent-soft:#e3f3e8;--waste:#b23b2e;--waste-soft:#f6e5e2;--font-sans:system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;--font-mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace}
:root[data-theme=dark]{--bg:#0b110e;--surface:#111a15;--surface-2:#0e1712;--text:#dce6df;--muted:#93a399;--faint:#66756c;--border:#20302a;--rail:#2a3b33;--accent:#3fb950;--accent-soft:#12251a;--waste:#e5705f;--waste-soft:#24140f}
:root[data-theme=light]{--bg:#f4f7f5;--surface:#fff;--surface-2:#eef3f0;--text:#16201b;--muted:#5c6b63;--faint:#8a988f;--border:#dde6e0;--rail:#cdd8d1;--accent:#1a7f3c;--accent-soft:#e3f3e8;--waste:#b23b2e;--waste-soft:#f6e5e2}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font-family:var(--font-sans);line-height:1.5;-webkit-font-smoothing:antialiased}
.wrap{max-width:900px;margin:0 auto;padding:36px 22px 80px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:22px 0 4px;border-bottom:1px solid var(--border);padding-bottom:0}
.tab-btn{font-family:var(--font-mono);font-size:12px;font-weight:600;color:var(--muted);background:transparent;border:1px solid transparent;border-bottom:none;border-radius:8px 8px 0 0;padding:8px 13px;cursor:pointer;margin-bottom:-1px}
.tab-btn:hover{color:var(--text);background:var(--surface-2)}
.tab-btn.active{color:var(--accent);background:var(--surface);border-color:var(--border);border-bottom:1px solid var(--surface)}
.tab-btn .tab-n{color:var(--faint);font-weight:500}
.tab-panel{display:none}
.tab-panel.active{display:block}
h1{font-size:26px;margin:0 0 4px;letter-spacing:-.01em}
.lede{color:var(--muted);font-size:13.5px;margin:0 0 26px;max-width:70ch}
.lede code{font-family:var(--font-mono);font-size:.92em;background:var(--surface-2);padding:1px 5px;border-radius:4px}
.session{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:16px 18px;margin:26px 0 16px}
.row{display:flex;gap:9px;flex-wrap:wrap;align-items:center}
.badge{font-family:var(--font-mono);font-size:11.5px;font-weight:600;padding:3px 8px;border-radius:6px;border:1px solid var(--border);background:var(--surface-2);color:var(--muted)}
.badge.host{color:var(--accent);border-color:var(--accent);background:var(--accent-soft)}
.sid{font-family:var(--font-mono);font-size:12.5px}
.task{margin-top:9px;font-size:14.5px}
.tiles{display:grid;grid-template-columns:repeat(3,1fr);gap:11px;margin:14px 0 22px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:13px 14px}
.tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--faint);font-family:var(--font-mono)}
.tile .v{font-size:20px;font-weight:650;margin-top:5px;font-variant-numeric:tabular-nums}
.tile .d{font-size:12px;margin-top:3px;color:var(--accent);font-weight:600}
.tile .d.before{color:var(--muted);font-weight:500}
.hero-row{grid-template-columns:repeat(3,1fr)!important}
.hero-row.two{grid-template-columns:repeat(2,1fr)!important}
.tile.hero{border-width:1px}
.tile.hero .v{font-size:26px}
.tile.hero.good{background:var(--accent-soft);border-color:var(--accent)}
.tile.hero.good .v{color:var(--accent)}
@media(max-width:640px){.tiles{grid-template-columns:1fr 1fr}}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin-bottom:10px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.swatch{width:11px;height:11px;border-radius:3px;display:inline-block}
.swatch.cut{background:var(--waste)}.swatch.atl{background:var(--accent)}
.turn{padding:8px 0}
.turn .body{border-left:2px solid var(--rail);padding:1px 0 4px 15px}
.role{font-size:11px;font-family:var(--font-mono);text-transform:uppercase;letter-spacing:.05em;color:var(--faint);margin-bottom:3px}
/* per message-kind accents (border + role label) */
.k-user_message .body{border-left-color:#3b82f6}.k-user_message .role{color:#3b82f6}
.k-agent_message .body{border-left-color:#6366f1}.k-agent_message .role{color:#6366f1}
.k-thinking .body{border-left-color:#8a8f98}.k-thinking .role{color:#8a8f98}
.k-tool_call .body{border-left-color:#8b5cf6}.k-tool_call .role{color:#8b5cf6}
.k-file_edit .body{border-left-color:#d69e2e}.k-file_edit .role{color:#d69e2e}
.k-shell_command .body{border-left-color:#0d9488}.k-shell_command .role{color:#0d9488}
.k-subagent_event .body{border-left-color:#3b82f6}.k-subagent_event .role{color:#3b82f6}
.k-todo_write .body{border-left-color:#8a8f98}
.say{font-size:14px;white-space:pre-wrap;overflow-wrap:anywhere}
.say.user{font-weight:550}
.say.think{color:var(--muted);font-style:italic}
.say.tele .filler{color:var(--faint);opacity:.45;text-decoration:line-through;text-decoration-color:var(--faint)}
.tele-note{font-size:11px;color:var(--faint);font-style:italic;margin-top:3px;font-family:var(--font-mono)}
.sub-inline summary{cursor:pointer;font-size:13.5px}
.sub-inline pre{margin:5px 0 0;white-space:pre-wrap;font-size:12px;background:var(--surface-2);padding:7px;border-radius:6px;max-height:240px;overflow:auto}
.subs{margin-top:22px;border-top:2px solid var(--border);padding-top:14px}
.subs-h{font-family:var(--font-mono);font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--faint);margin-bottom:8px}
details.subagent{border:1px solid var(--border);border-radius:10px;margin:8px 0;background:var(--surface)}
details.subagent>summary{cursor:pointer;padding:10px 12px;font-family:var(--font-mono);font-size:12.5px;color:var(--accent)}
details.subagent[open]>summary{border-bottom:1px solid var(--border)}
details.subagent .session-block{padding:0 12px 8px}
details.subagent .wrap{padding:0}
.say.meta{color:var(--muted);font-size:13px}
.call{font-family:var(--font-mono);font-size:12.5px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:7px 10px;margin:5px 0;overflow-x:auto}
.call .tool{color:var(--accent);font-weight:600}
.call .arg{color:var(--text)}
.call pre{margin:6px 0 0;padding-top:6px;border-top:1px dashed var(--border);white-space:pre-wrap;overflow-x:auto;font-size:12px;color:var(--muted)}
.out{margin-top:6px}
.out summary{cursor:pointer;color:var(--muted);font-size:12px}
.out pre{margin:6px 0 0;white-space:pre-wrap;overflow-x:auto;font-size:12px;background:var(--surface-2);padding:8px;border-radius:6px;max-height:340px;overflow-y:auto}
/* replaced (grep/read loop LemonCrow collapses): dead-grey text, orange 'replaced' cue */
.turn.cut .body{border-left-color:#c2611d;border-left-style:dashed}
.turn.cut .say,.turn.cut .role{opacity:.6;color:var(--faint)}
.turn.cut .call{text-decoration:line-through;text-decoration-color:var(--faint);color:var(--faint);opacity:.72;background:transparent;border:1px dashed #c2611d}
.cut-tag{font-family:var(--font-mono);font-size:10.5px;color:#c2611d;font-weight:600;margin-left:7px;text-decoration:none;display:inline-block;opacity:1}
:root[data-theme=dark] .turn.cut .body{border-left-color:#d98a4a}
:root[data-theme=dark] .turn.cut .call{border-color:#d98a4a}
:root[data-theme=dark] .cut-tag{color:#d98a4a}
:root[data-theme=light] .turn.cut .body{border-left-color:#c2611d}
:root[data-theme=light] .turn.cut .call{border-color:#c2611d}
:root[data-theme=light] .cut-tag{color:#c2611d}
/* batched (merged into one LemonCrow call): green, not removed */
.turn.merged .body{border-left-color:var(--accent);border-left-style:dashed}
.turn.merged .call{opacity:.66}
.merge-tag{font-family:var(--font-mono);font-size:10.5px;color:var(--accent);font-weight:600;margin-left:7px}
.atl-card.batch{background:transparent;border-style:dashed}
.collapse{margin:5px 0 6px}
.atl-card{background:var(--accent-soft);border:1px solid var(--accent);border-radius:10px;padding:11px 13px;margin-left:15px}
.atl-card .h{font-weight:650;font-size:13px;color:var(--accent);font-family:var(--font-mono)}
.atl-card .search{font-family:var(--font-mono);font-size:12.5px;margin:7px 0 3px}
.atl-card .collapses{margin-top:8px;font-size:12.5px;border-top:1px solid var(--accent);padding-top:7px}
.atl-card .collapses b{color:var(--accent);font-variant-numeric:tabular-nums}
.hits{margin:8px 0 2px}
.hit{font-family:var(--font-mono);font-size:12px;margin:2px 0}
.hit code{color:var(--text)}.hit .sym{color:var(--accent);font-weight:600}.hit .kind{color:var(--muted)}
.match{font-family:var(--font-mono);font-size:12px;color:var(--accent);font-weight:600;margin-top:5px}
.an{margin:5px 0 2px 4px;border-left:2px solid var(--accent);padding:4px 0 4px 10px}
.an-h{font-family:var(--font-mono);font-size:12px;color:var(--accent);font-weight:600}
.an-tag{font-size:9.5px;padding:1px 5px;border-radius:4px;margin-left:6px;font-weight:700;letter-spacing:.04em}
.an-tag.real{background:var(--accent);color:var(--bg)}
.an-tag.preview{background:var(--surface-2);color:var(--muted);border:1px solid var(--border)}
.an-note{font-size:12px;color:var(--muted);margin-top:3px}
.an-out{font-family:var(--font-mono);font-size:11.5px;background:var(--surface-2);padding:7px;border-radius:6px;margin-top:5px;white-space:pre-wrap;overflow-x:auto;max-height:260px;overflow-y:auto}
.empty{color:var(--muted)}
.toggle{position:fixed;top:12px;right:12px;font-family:var(--font-mono);font-size:12px;background:var(--surface);border:1px solid var(--border);color:var(--muted);border-radius:7px;padding:5px 9px;cursor:pointer}
.toggle:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
@media(prefers-reduced-motion:reduce){*{transition:none!important}}
</style>
</head>
<body>
<button class="toggle" onclick="var r=document.documentElement;r.dataset.theme=(r.dataset.theme==='dark'?'light':'dark')">theme</button>
<div class="wrap">
<h1>LemonCrow Session Replay</h1>
<p class="lede">Reconstructed from a recorded session — <b>no model was re-run, $0</b>. The full transcript is replayed; grep→read loops the agent walked are struck through, with the single <code>code_search</code> that would have collapsed each one inserted inline.</p>
{{BODY}}
</div>
</body>
</html>
"""
