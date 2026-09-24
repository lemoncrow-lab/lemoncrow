"""Canonical model-facing renderers for structured LemonCrow tool results."""

from __future__ import annotations

import contextlib
import json
import os
import re
from pathlib import Path
from typing import Any

from lemoncrow_client.kit.search import render_grep_text
from lemoncrow_client.kit.sql import render_sql_text

from lemoncrow.gateway.tools.state import tool_call_rendered_text

# Bound outline and sparse-gutter verbosity without changing underlying results.
_READ_OUTLINE_MAX_LINES = 80
_SECTION_GUTTER_RE = re.compile(r"^(\d+)\t")
_GUTTER_ANCHOR_EVERY = 5


CODE_INTEL_RENDER_TOOLS: frozenset[str] = frozenset(
    {
        "node",
        "callers",
        "callees",
        "usages",
        "codemod",
        "index",
        "blame",
        "cache",
        "cache_status",
        "cache_invalidate",
    }
)


def _append_search_verdict_footer(text: str | None, result: dict[str, Any]) -> str | None:
    """Append verdict / fallback / breaker lines to a search|grep render.

    Single chokepoint so the model sees the honest-empty signal regardless of
    which underlying renderer produced the body text.
    """
    parts: list[str] = []
    fallback = result.get("text_fallback")
    if isinstance(fallback, list) and fallback:
        locs = "; ".join(f"{item.get('path')}:{item.get('line')}" for item in fallback[:8] if isinstance(item, dict))
        if locs:
            parts.append(f"[search:tfallback] {locs}")
    verdict = result.get("verdict")
    if isinstance(verdict, str) and verdict not in {"", "found"}:
        nxt = str(result.get("next") or "").strip()
        if verdict in {"missed", "absent"}:
            parts.append("no matches")  # minimal, vanilla-style -- no prefix, no nudge
        else:
            parts.append(f"[search:{verdict}]" + (f" {nxt}" if nxt else ""))
    breaker = result.get("breaker_note")
    if isinstance(breaker, str) and breaker:
        parts.append(f"[search:budget] {breaker}")
    if not parts:
        return text
    footer = "\n".join(parts)
    return f"{text}\n{footer}" if text else footer


def _render_read_md(result: dict[str, Any]) -> str | None:
    mode = str(result.get("mode") or "")
    projection = result.get("projection")
    notice = ""
    if isinstance(projection, dict):
        raw_notice = str(projection.get("notice") or "").strip()
        if raw_notice:
            notice = raw_notice
    if mode == "directory":
        entries = result.get("entries")
        if isinstance(entries, list):
            return "\n".join(entries)
        return None
    if mode == "summary":
        summary = str(result.get("summary") or "").strip()
        if not summary:
            return None
        return f"{notice}\n\n{summary}" if notice else summary
    if mode in {"range", "full"}:
        content = str(result.get("content") or "")
        if not content:
            return None
        return f"{notice}\n\n{content}" if notice else content
    if mode == "outline":
        path = str(result.get("path") or "?")
        language = str(result.get("language") or "")
        outline = result.get("outline")
        if isinstance(outline, dict):
            rendered = _render_read_outline_md(path, outline, language)
            return f"{notice}\n\n{rendered}" if notice else rendered
        return None
    return None


def _render_symbol_read_md(sym: dict[str, Any]) -> str | None:
    sym_path = str(sym.get("path") or "")
    sym_kind = str(sym.get("kind") or "symbol")
    start = int(sym.get("line") or 0)
    end = int(sym.get("end_line") or start)
    if not sym_path or not start:
        return None
    try:
        src_lines = Path(sym_path).read_text(encoding="utf-8", errors="ignore").splitlines()
        body = "\n".join(src_lines[start - 1 : end])
    except OSError:
        return None
    range_tag = f"L{start}-L{end}" if end > start else f"L{start}"
    return f"### {sym_path}:{range_tag} ({sym_kind})\n{body}"


def _render_read_outline_md(path: str, outline: dict[str, Any], language: str) -> str:
    # Treesitter/generic: has pre-formatted `text` field
    text = str(outline.get("text") or "").strip()
    if text:
        t_lines = text.splitlines()
        if len(t_lines) > _READ_OUTLINE_MAX_LINES:
            dropped = len(t_lines) - _READ_OUTLINE_MAX_LINES
            t_lines = [
                *t_lines[:_READ_OUTLINE_MAX_LINES],
                f"... +{dropped} more outline lines (use :Lx-Ly or :full)",
            ]
        return "\n".join(t_lines)
    # AST outline: has `symbols`, `imports`, `hint` fields
    lines: list[str] = []
    imports_list = outline.get("imports")
    if isinstance(imports_list, list) and imports_list:
        # Collapse to distinct top-level roots instead of one line per import.
        # On import-heavy files the full list is dozens of lines the model
        # rarely needs; the root set conveys the dependency surface far cheaper.
        roots = sorted({str(imp).split()[0].split(".")[0] for imp in imports_list if str(imp).strip()})
        lines.append(f"imports ({len(imports_list)}): {', '.join(roots)}")
    symbols_list = outline.get("symbols")
    if isinstance(symbols_list, list) and symbols_list:
        lines.append("symbols:")
        for sym in symbols_list[:_READ_OUTLINE_MAX_LINES]:
            if not isinstance(sym, dict):
                continue
            name = str(sym.get("name") or "?")
            kind = str(sym.get("kind") or "?")
            start = int(sym.get("start_line") or 0)
            end = int(sym.get("end_line") or 0)
            loc = f"{start}-{end}" if end > start else str(start)
            # `[function]` is the majority kind -- tagging only the exceptions
            # saves ~2 tokens per line on function-heavy outlines.
            tag = "" if kind == "function" else f" [{kind}]"
            lines.append(f"- {loc}: {name}{tag}")
        if len(symbols_list) > _READ_OUTLINE_MAX_LINES:
            lines.append(f"... +{len(symbols_list) - _READ_OUTLINE_MAX_LINES} more symbols (use :Lx-Ly or :full)")
    return "\n".join(lines) if lines else "(no outline)"


def _sparse_gutter(content: str) -> str:
    """Drop line-number prefixes that are derivable from a nearby anchor.

    Search sections are read-oriented: within a contiguous run the first line
    and every ``_GUTTER_ANCHOR_EVERY``-th line keep their number; the lines
    between (numbered exactly previous+1) carry no information and drop their
    prefix. Runs re-anchor after skeleton elisions and truncation markers (any
    non-gutter line resets the counter). Measured on real payloads the full
    gutter is ~10% of the rendered code_search text. The `read` tool keeps its
    full gutter — that surface is edit-oriented.
    """
    out: list[str] = []
    previous: int | None = None
    run_pos = 0
    for line in content.split("\n"):
        match = _SECTION_GUTTER_RE.match(line)
        if match is None:
            out.append(line)
            previous = None
            continue
        number = int(match.group(1))
        consecutive = previous is not None and number == previous + 1
        run_pos = run_pos + 1 if consecutive else 0
        keep = not consecutive or run_pos % _GUTTER_ANCHOR_EVERY == 0
        out.append(line if keep else line[match.end() :])
        previous = number
    return "\n".join(out)


def _compress_candidate_files(paths: list[str]) -> str:
    """Comma-joined candidate_files line, grouping consecutive same-directory
    entries into ``dir/{a,b,c}`` so a shared prefix isn't repeated per file.

    Only ADJACENT same-directory runs group (never reordered into a group):
    candidate_files is the ranked recall surface (MRR-scored by position), so
    grouping non-adjacent entries would shift ranks when a consumer re-splits
    and expands. A caller that splits on top-level commas (treating a
    ``{...}`` span as opaque) and expands each ``dir/{a,b,c}`` segment
    recovers the identical ordered list -- see
    ``benchmarks/codebench/eval_external_provider_mrr.py``'s
    ``_split_candidate_files_line``/``_expand_candidate_segment``.
    """
    segments: list[str] = []
    i, n = 0, len(paths)
    while i < n:
        path = paths[i]
        slash = path.rfind("/")
        if slash == -1:
            segments.append(path)
            i += 1
            continue
        directory = path[: slash + 1]
        run = [path[slash + 1 :]]
        j = i + 1
        while j < n:
            next_path = paths[j]
            next_slash = next_path.rfind("/")
            if next_slash == -1 or next_path[: next_slash + 1] != directory:
                break
            run.append(next_path[next_slash + 1 :])
            j += 1
        segments.append(directory + "{" + ",".join(run) + "}" if len(run) >= 2 else path)
        i = j
    return ", ".join(segments)


def _render_code_search_md(payload: dict[str, Any]) -> str | None:
    """Compact text view of a lean code_search payload.

    Drops the JSON key-noise: sections render as ``## path`` / ``### sym Lx-Ly``
    over sparsely line-numbered source (first line of each contiguous run keeps
    its number; see ``_sparse_gutter``) or their outline pointer, related_symbols
    as one ``path:Lx-Ly kind name`` line each (same-symbol repeats merge their
    ranges), candidate_files as a single comma-joined line. Internal bookkeeping
    (tokens_saved / calls_saved) never reaches the model.
    """
    files = payload.get("files")
    if not isinstance(files, list):
        return None
    parts: list[str] = []
    if not payload.get("exact_match"):
        parts.append("no exact match -- ranked candidates")
    for entry in files:
        if not isinstance(entry, dict):
            continue
        identity = " ".join(str(value) for value in (entry.get("repo_name"), entry.get("project_id")) if value)
        suffix = f" [{identity}]" if identity else ""
        lines = [f"## {entry.get('path') or '?'}{suffix}"]
        for sec in entry.get("sections") or []:
            if not isinstance(sec, dict):
                continue
            outline = sec.get("outline")
            if outline and "content" not in sec:
                lines.append(str(outline))
                continue
            sym = str(sec.get("qualified_name") or "")
            start, end = sec.get("line"), sec.get("end_line")
            span = f"L{start}-L{end}" if start is not None and end is not None else ""
            header = " ".join(p for p in ("###", sym, span) if p)
            lines.append(header)
            content = str(sec.get("content") or "").rstrip("\n")
            if content:
                lines.append(_sparse_gutter(content))
        parts.append("\n".join(lines))
    related = payload.get("related_symbols")
    if isinstance(related, list) and related:
        merged: dict[tuple[str, str, str, str, str], list[str]] = {}
        for sym_entry in related:
            if not isinstance(sym_entry, dict):
                continue
            path = str(sym_entry.get("path") or "?")
            kind = str(sym_entry.get("kind") or "")
            name = str(sym_entry.get("qualified_name") or "?")
            repo_name = str(sym_entry.get("repo_name") or "")
            project_id = str(sym_entry.get("project_id") or "")
            start, end = sym_entry.get("line"), sym_entry.get("end_line")
            if start is None:
                span = ""
            elif end is None or end == start:
                span = f"L{start}"
            else:
                span = f"L{start}-L{end}"
            merged.setdefault((path, kind, name, repo_name, project_id), []).append(span)
        rel_lines = ["related_symbols:"]
        for (path, kind, name, repo_name, project_id), spans in merged.items():
            span_txt = ",".join(s for s in spans if s)
            loc = f"{path}:{span_txt}" if span_txt else path
            # "function" is the majority kind -- tag only the exceptions
            # (same rule as read outlines).
            if kind == "function":
                kind = ""
            identity = " ".join(p for p in (repo_name, project_id) if p)
            suffix = f"[{identity}]" if identity else ""
            rel_lines.append(" ".join(p for p in (loc, kind, name, suffix) if p))
        parts.append("\n".join(rel_lines))
    cands = payload.get("candidate_files")
    if isinstance(cands, list) and cands:
        line = "candidate_files: " + _compress_candidate_files([str(c) for c in cands])
        _more = payload.get("candidate_files_more")
        if isinstance(_more, int) and _more > 0:
            line += f" (+{_more} more; pass limit={len(cands) + _more} to see them)"
        parts.append(line)
        candidate_projects = payload.get("candidate_projects")
        if isinstance(candidate_projects, dict):
            routed = [
                f"{path}={project_id}"
                for path, project_id in candidate_projects.items()
                if isinstance(path, str) and isinstance(project_id, str)
            ]
            if routed:
                parts.append("candidate_projects: " + ", ".join(routed))
    if payload.get("truncated"):
        parts.append("(truncated; narrow with paths=)")
    return "\n\n".join(parts) if parts else None


def _render_search_md(result: dict[str, Any]) -> str | None:
    mode = str(result.get("mode") or "chunks")
    if mode == "map":
        outline = str(result.get("outline") or "").strip()
        ranked_raw = result.get("ranked_files")
        ranked = ranked_raw if isinstance(ranked_raw, list) else []
        if not outline and not ranked:
            return None
        # The repo-map `outline` is already plain text; emitting it directly
        # (instead of json.dumps of the whole payload) drops the JSON wrapper and
        # the \n-escaping of every outline line.
        map_lines = ["### repo_map"]
        if outline:
            map_lines.append(outline)
        if ranked:
            map_lines.append("files:")
            for entry in ranked:
                if isinstance(entry, dict):
                    map_lines.append(f"- {entry.get('path') or entry.get('file') or '?'}")
                else:
                    map_lines.append(f"- {entry}")
        return "\n".join(map_lines)
    matches = result.get("matches")
    if not isinstance(matches, list) or not matches:
        return "### search\n- no matches"
    lines: list[str] = ["### search"]
    for match in matches:
        if not isinstance(match, dict):
            continue
        path = str(match.get("path") or "?")
        lines.append(path)
        content = str(match.get("content") or "").strip()
        if content:
            lines.append(content)
        else:
            snippets = match.get("snippets")
            if isinstance(snippets, list):
                for snip in snippets[:3]:
                    if isinstance(snip, dict):
                        snip_content = str(snip.get("content") or "").strip()
                        if snip_content:
                            lines.append(snip_content)
    return "\n".join(lines)


def _render_memory_md(result: dict[str, Any]) -> str | None:
    """Compact recall rendering: one header line per passage (source/tags) plus
    its text body, instead of a JSON list that repeats the field keys on every
    entry and escapes every newline in the passage text. Only recall (which has
    a ``passages`` list) is rendered; store_fact/vote_fact fall back to JSON.
    """
    passages = result.get("passages")
    if not isinstance(passages, list):
        return None
    if not passages:
        return "### memory\n- no passages"
    lines = ["### memory"]
    for passage in passages:
        if not isinstance(passage, dict):
            continue
        ref = (str(passage.get("source_ref") or passage.get("id") or "?").strip()) or "?"
        tags = passage.get("tags")
        tag_str = f" [{', '.join(str(tag) for tag in tags)}]" if isinstance(tags, list) and tags else ""
        lines.append(f"- {ref}{tag_str}")
        text = str(passage.get("text") or passage.get("fact") or "").strip()
        if text:
            lines.append(text)
    return "\n".join(lines)


def _render_verify_md(result: dict[str, Any]) -> str | None:
    """Compact rubric-gate rendering with optional per-check detail."""
    status = result.get("status")
    rubric_id = result.get("rubric_id")
    outcomes = result.get("outcomes")
    if status is None and rubric_id is None and not isinstance(outcomes, list):
        return None
    lines = [f"### verify rubric={rubric_id or '?'} status={status or '?'}"]
    if not isinstance(outcomes, list):
        return "\n".join(lines)
    for outcome in outcomes:
        if not isinstance(outcome, dict):
            continue
        status = str(outcome.get("status") or "?")
        check_name = str(outcome.get("name") or "?")
        detail = str(outcome.get("detail") or "").strip()
        lines.append(f"- {status} {check_name}" + (f": {detail}" if detail else ""))
    escalations = result.get("escalations")
    if isinstance(escalations, list):
        for escalation in escalations:
            lines.append(f"- escalation: {escalation}")
    return "\n".join(lines)


def _render_context_tool_md(result: dict[str, Any]) -> str | None:
    """Compact the three public context modes by payload shape.

    Procedures already arrive as one fully assembled model-facing ``context``
    string. The surrounding recall/token/bootstrap objects are diagnostics or
    structured provenance and would merely repeat content the model already sees.
    Symbol context reuses the shared code-context renderer; scoped pull context
    keeps packed snippets plus a tiny budget-tail marker.
    """
    context = result.get("context")
    if isinstance(context, str):
        body = context.strip()
        note = ""
        bootstrap = result.get("bootstrap")
        if isinstance(bootstrap, dict):
            status = str(bootstrap.get("status") or "").strip().lower()
            if status and status != "warm":
                note = f"[bootstrap {status}]"
        if body and note:
            return f"{body}\n\n{note}"
        if body:
            return body
        if note:
            return note

    if any(key in result for key in ("entry_points", "related_symbols", "code_blocks", "symbols", "import_neighbors")):
        from lemoncrow.pro.capabilities.code_context.renderer import render_code_payload

        return render_code_payload("context", result)

    chunks = result.get("chunks")
    if isinstance(chunks, list):
        lines: list[str] = []
        rationale = str(result.get("rationale") or "").strip()
        if rationale:
            lines.append(rationale)
        for chunk in chunks:
            if not isinstance(chunk, dict):
                continue
            path = str(chunk.get("path") or "?")
            symbol = str(chunk.get("symbol") or "").strip()
            kind = str(chunk.get("kind") or "").strip().lower()
            suffix = f" · {symbol}" if symbol else ""
            if kind and kind not in {"?", "function"}:
                suffix += f" · {kind}"
            lines.append(f"→ {path}{suffix}")
            signature = str(chunk.get("signature") or "").strip()
            snippet = str(chunk.get("snippet") or "").strip()
            if signature:
                lines.append(signature)
            if snippet and snippet != signature:
                lines.append(snippet)
        dropped = result.get("dropped_for_budget")
        if isinstance(dropped, int) and dropped > 0:
            lines.append(f"+{dropped} dropped for budget")
        return "\n".join(lines) if lines else "no scoped context"
    return None


def _render_rescue_md(result: dict[str, Any]) -> str | None:
    """Render recovery instructions plus only actionable historical evidence."""
    rescue = str(result.get("rescue") or "").strip()
    analysis = result.get("analysis")
    if not rescue and not isinstance(analysis, dict):
        return None

    lines: list[str] = []
    if rescue:
        lines.extend(["rescue", rescue])

    if isinstance(analysis, dict):
        incident = analysis.get("incident")
        fixes: list[str] = []
        if isinstance(incident, dict):
            root_cause = str(incident.get("root_cause_hypothesis") or "").strip()
            if root_cause:
                lines.extend(["history", f"root cause · {root_cause}"])
            meta: list[str] = []
            count = incident.get("count")
            confidence = incident.get("confidence")
            match_score = analysis.get("match_score")
            if isinstance(count, int) and count > 0:
                meta.append(f"seen {count}x")
            if isinstance(confidence, (int, float)):
                meta.append(f"confidence {confidence}")
            if isinstance(match_score, (int, float)):
                meta.append(f"match {match_score}")
            if meta:
                lines.append(" · ".join(meta))
            raw_fixes = incident.get("suggested_fixes")
            if isinstance(raw_fixes, list):
                fixes = [str(item).strip() for item in raw_fixes if str(item).strip()]
        else:
            reason = str(analysis.get("reason") or "").strip()
            if reason:
                lines.extend(["history", reason])
            raw_fixes = analysis.get("suggested_fixes")
            if isinstance(raw_fixes, list):
                fixes = [str(item).strip() for item in raw_fixes if str(item).strip()]

        if fixes:
            lines.append("next")
            lines.extend(f"- {item}" for item in fixes[:8])

    return "\n".join(lines) if lines else None


def _render_compact_md(result: dict[str, Any]) -> str | None:
    """Return the compacted state itself, not a JSON wrapper around it."""
    prompt_block = str(result.get("prompt_block") or "").strip()
    if not prompt_block:
        return None
    before = result.get("tokens_before")
    after = result.get("tokens_after_estimate")
    freed = result.get("tokens_freed")
    summary = ""
    if isinstance(before, int) and isinstance(after, int):
        summary = f"compact {before}→{after} tokens"
        if isinstance(freed, int):
            summary += f" · freed {freed}"
    elif isinstance(freed, int):
        summary = f"compact · freed {freed} tokens"
    return f"{prompt_block}\n\n{summary}" if summary else prompt_block


def _resolved_bash_idle_grace_s(idle_grace_s: float | None) -> float:
    if idle_grace_s is not None:
        return idle_grace_s
    try:
        value = float(os.environ.get("LEMONCROW_BASH_IDLE_GRACE", "75"))
    except ValueError:
        return 75.0
    return value if value > 0 else 75.0


def render_bash_text(result: dict[str, Any], *, idle_grace_s: float | None = None) -> str:
    idle_grace_s = _resolved_bash_idle_grace_s(idle_grace_s)
    """Render shell output as compact text while preserving structured internals."""
    exit_code = result.get("exit_code")
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    blocked = bool(result.get("blocked"))
    blocked_reason = str(result.get("blocked_reason") or "")
    truncated = bool(result.get("truncated"))
    lines_omitted = result.get("lines_omitted")
    status = str(result.get("status") or "")
    session_id = str(result.get("session_id") or "")
    explicit_background = bool(result.get("explicit_background"))

    parts: list[str] = []
    if "updated" in result:
        # action="update" response -- a distinct shape from the plain
        # running/status payloads below, so render it up front and return.
        remaining_ms = result.get("timeout_remaining_ms")
        if result.get("updated"):
            remaining_txt = f"{int(remaining_ms) // 1000}s" if isinstance(remaining_ms, int) else "?"
            parts.append(f"kill deadline updated, {remaining_txt} left id={session_id}")
        else:
            parts.append(f"update failed: session already {status} id={session_id}")
        return "\n".join(parts).strip()
    if status == "running":
        over_budget = bool(result.get("over_budget"))
        idle_return = bool(result.get("idle_return"))
        if explicit_background:
            parts.append(f"background running id={session_id}; bash(id={session_id}) waits for it")
        elif result.get("interactive"):
            parts.append(f"interactive session id={session_id}")
        elif idle_return:
            # Cut early because progress stalled -- NOT because the budget ran
            # out. Do not steer the model to just re-wait (that re-blocks on a
            # stuck command); tell it the handle is a decision point.
            parts.append(
                f"no progress ~{int(idle_grace_s)}s — likely stuck. id={session_id} still running "
                f"(not killed): bash(id={session_id}, action=kill), or move on"
            )
        elif result.get("orchestration_return"):
            parts.append(
                f"still running id={session_id}; returned early to keep the tool call reliable — "
                f"bash(id={session_id}) waits for this same run; don't rerun it"
            )
        elif over_budget:
            parts.append(f"still running id={session_id}; bash(id={session_id}) waits for it — don't sleep-poll")
        else:
            parts.append(f"running id={session_id}")
    elif status and status != "completed":
        # Terminal states (cancelled/timed_out): the session is reaped, its id
        # can never be polled again -- don't ship a dead handle. A clean
        # "completed" is implied by output + exit_code and costs a line.
        # "blocked" with a reason skips the bare state word too -- every
        # blocked_reason already says "blocked".
        if not (status == "blocked" and blocked_reason):
            parts.append(status)
    # Log paths are recovery pointers: folded into the lossy-view marker
    # (tail slice / truncation) instead of standalone log_file= lines. A spill
    # hint already names a full-output path, so logs are skipped there.
    log_file = str(result.get("log_file") or "")
    log_file_stderr = str(result.get("log_file_stderr") or "")
    tail_lines = result.get("tail_lines")
    spill_hint = str(result.get("spill_hint") or "")
    if log_file and log_file_stderr:
        # The two stream logs differ only in suffix -- brace the divergence
        # ({stdout.txt, stderr.txt}) instead of repeating the directory + id.
        i = len(os.path.commonprefix([log_file, log_file_stderr]))
        i = max(log_file.rfind(c, 0, i) + 1 for c in "./")
        if i:
            log_paths = f"{log_file[:i]}{{{log_file[i:]}, {log_file_stderr[i:]}}}"
        else:
            log_paths = f"{log_file} {log_file_stderr}"
    else:
        log_paths = log_file or log_file_stderr
    log_ptr = f"; full: {log_paths}" if log_paths and not spill_hint else ""
    if status == "running" and log_paths:
        parts.append(f"[logs: {log_paths}]")
    if isinstance(tail_lines, int) and tail_lines > 0:
        parts.append(f"[tail: last {tail_lines} lines{log_ptr}]")
    if blocked:
        if status != "blocked":
            header = "blocked"
            if exit_code is not None:
                header = f"{header} (exit_code={exit_code})"
            parts.append(header)
        if blocked_reason:
            parts.append(blocked_reason)
            # Streams that merely echo the reason are noise.
            if stdout.strip() == blocked_reason:
                stdout = ""
            if stderr.strip() == blocked_reason:
                stderr = ""
    elif exit_code not in (None, 0):
        parts.append(f"exit_code={exit_code}")

    if stdout:
        parts.append(stdout)
    if stderr:
        if stdout:
            parts.append("")
        if exit_code in (None, 0) and not blocked:
            parts.append("stderr:")
        parts.append(stderr)
    _chars_omitted = result.get("chars_omitted")
    _trivial_trim = isinstance(_chars_omitted, int) and 0 <= _chars_omitted < 300
    if truncated and not (_trivial_trim and not spill_hint):
        if stdout or stderr:
            parts.append("")
        # Character-only and structured-data sampling can be lossy without a
        # meaningful line count. Surface recovery whenever the result says it
        # was compacted, not only when lines_omitted happens to be positive.
        # Trivial trims (a few progress/blank lines) get no notice at all --
        # the footer would outweigh what it accounts for.
        if spill_hint:
            parts.append(spill_hint)
        elif isinstance(lines_omitted, int) and lines_omitted > 0:
            parts.append(f"[output truncated: {lines_omitted} lines omitted{log_ptr}]")
        else:
            parts.append(f"[output compacted{log_ptr}]")
    rendered = "\n".join(parts).strip()
    if rendered:
        return rendered
    if exit_code is not None:
        return f"exit_code={exit_code}"
    return ""


def render_tool_result_text(
    name: str,
    result: Any,
    *,
    bash_idle_grace_s: float | None = None,
) -> str | None:
    """Best-effort compact text rendering of a tool result for model context.

    Shared by the MCP dispatch path and the in-process CLI runtime so both
    hosts send the model identical, minimal text instead of raw dict dumps.
    Returns ``None`` when no renderer applies or it produced nothing — callers
    fall back to the raw string / compact JSON form.
    """
    # "symbols" is a render-name (not a tool): the `symbols` tool was removed,
    # but direct `_op_search` callers (tests, power use) still pass it to fetch
    # the engine's thread-local rendered text. It can't be "search" -- that
    # would flip the live `search` tool from raw JSON to markdown output.
    if name in {"symbols", "tool"} | CODE_INTEL_RENDER_TOOLS:
        return getattr(tool_call_rendered_text, "value", None) or None
    if not isinstance(result, dict):
        return None
    payload = result
    text: str | None = None
    if name == "read":
        with contextlib.suppress(Exception):
            files = payload.get("files")
            if isinstance(files, list):
                parts: list[str] = []
                cwd = str(Path.cwd())
                dict_entries = [entry for entry in files if isinstance(entry, dict)]
                # A single-entry read needs no `## path` header -- the caller
                # just named the file (and range); headers only earn their
                # tokens when several entries must be told apart.
                lone_entry = len(dict_entries) == 1
                for entry in dict_entries:
                    entry_path = str(entry.get("path") or "?")
                    if entry_path.startswith(cwd + os.sep):
                        entry_path = entry_path[len(cwd) + 1 :]
                    entry_text = _render_read_md(entry)
                    if entry_text is None:
                        entry_text = json.dumps(entry, ensure_ascii=False, separators=(",", ":"))
                    if lone_entry:
                        parts.append(entry_text)
                    elif entry.get("mode") == "range":
                        raw_range = str(entry.get("range") or "")
                        range_tag = ":L" + raw_range.replace("-", "-L") if raw_range else ""
                        parts.append(f"## {entry_path}{range_tag}\n{entry_text}")
                    else:
                        parts.append(f"## {entry_path}\n{entry_text}")
                text = "\n\n".join(parts) if parts else None
            else:
                text = _render_read_md(payload)
                if text and payload.get("mode") == "range":
                    raw_range = str(payload.get("range") or "")
                    if raw_range:
                        text = "## L" + raw_range.replace("-", "-L") + "\n" + text
                if text is None:
                    symbols_list = payload.get("symbols")
                    if isinstance(symbols_list, list):
                        sym_parts = [_render_symbol_read_md(s) for s in symbols_list if isinstance(s, dict)]
                        text = "\n\n".join(p for p in sym_parts if p) or None
                    elif "kind" in payload and "line" in payload and "path" in payload:
                        text = _render_symbol_read_md(payload)
    elif name == "grep":
        with contextlib.suppress(Exception):
            text = render_grep_text(payload)
    elif name == "code_search":
        with contextlib.suppress(Exception):
            text = _render_code_search_md(payload)
    elif name == "graph":
        with contextlib.suppress(Exception):
            from lemoncrow.pro.capabilities.code_context.renderer import render_graph_payload

            text = render_graph_payload(payload)
    elif name == "context":
        with contextlib.suppress(Exception):
            text = _render_context_tool_md(payload)
    elif name == "compact":
        with contextlib.suppress(Exception):
            text = _render_compact_md(payload)
    elif name == "rescue":
        with contextlib.suppress(Exception):
            text = _render_rescue_md(payload)
    elif name == "search":
        with contextlib.suppress(Exception):
            # mode="symbol" routes through _op_search, which stashes the compact
            # code-intel locator on the thread-local; prefer it when present.
            rendered = getattr(tool_call_rendered_text, "value", None)
            text = rendered if isinstance(rendered, str) and rendered.strip() else _render_search_md(payload)
    elif name == "bash":
        with contextlib.suppress(Exception):
            text = render_bash_text(payload, idle_grace_s=bash_idle_grace_s)
    elif name == "web_fetch":
        with contextlib.suppress(Exception):
            text = str(payload.get("content") or "")
    elif name == "mcp":
        with contextlib.suppress(Exception):
            text = str(payload.get("content") or "") or None
    elif name == "verify":
        with contextlib.suppress(Exception):
            text = _render_verify_md(payload)
    elif name == "sql":
        with contextlib.suppress(Exception):
            text = render_sql_text(payload)
    elif name == "explore":
        with contextlib.suppress(Exception):
            from lemoncrow.pro.capabilities.code_context.renderer import _render_explore

            text = _render_explore(payload) or None
    elif name == "memory":
        with contextlib.suppress(Exception):
            text = _render_memory_md(payload)
    elif name == "edit":
        # Clean success renders a MINIMAL one-liner -- "applied path:line" -- so the
        # model stays oriented without re-reading, and without a JSON dump of the
        # internal `calls_saved` key. No applied ranges -> "ok". Actionable results
        # (failures, rollbacks, diagnostics, reviews, fuzzy matches) keep their
        # structured body and render as JSON via the dispatcher fallback.
        # `vcs_status` rides along as a suffix on the one-liner rather than
        # tripping the JSON fallback -- it's always present on a dirty tree
        # (the just-edited file always shows), so treating it like an
        # actionable key would blow the minimal render up on every call.
        keys = set(payload)
        # `resolved_against` rides as a one-liner suffix like vcs_status rather
        # than tripping the JSON fallback -- a redirected edit is still a clean
        # success and should not render as a structured dump.
        base_keys = keys - {"vcs_status", "resolved_against"}
        if base_keys <= {"calls_saved"}:
            text = "ok"
        elif base_keys <= {"applied", "calls_saved"}:
            applied = payload.get("applied") or []
            if applied and all(isinstance(a, str) for a in applied):
                text = "applied " + ", ".join(applied)
            elif not applied:
                text = "ok"
        if text:
            resolved_against = payload.get("resolved_against")
            if isinstance(resolved_against, str) and resolved_against:
                text = f"{text} | resolved against worktree {resolved_against} (from last bash cwd)"
            vcs_raw = payload.get("vcs_status")
            vcs = vcs_raw if isinstance(vcs_raw, dict) else {}
            vcs_lines = vcs.get("lines")
            if vcs_lines:
                text = f"{text} | {vcs.get('source')}: " + "; ".join(vcs_lines)
    if name in {"search", "grep"} and isinstance(result, dict):
        text = _append_search_verdict_footer(text, result)
    return text or None


__all__ = [
    "CODE_INTEL_RENDER_TOOLS",
    "_GUTTER_ANCHOR_EVERY",
    "_READ_OUTLINE_MAX_LINES",
    "_SECTION_GUTTER_RE",
    "_append_search_verdict_footer",
    "_compress_candidate_files",
    "_render_code_search_md",
    "_render_compact_md",
    "_render_context_tool_md",
    "_render_memory_md",
    "_render_read_md",
    "_render_read_outline_md",
    "_render_rescue_md",
    "_render_search_md",
    "_render_symbol_read_md",
    "_render_verify_md",
    "_sparse_gutter",
    "render_bash_text",
    "render_tool_result_text",
]
