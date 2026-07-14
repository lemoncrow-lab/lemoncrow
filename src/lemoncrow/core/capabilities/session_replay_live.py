"""Live enrichment for session replay: call the REAL LemonCrow tools.

For a reconstructed :class:`~lemoncrow.core.capabilities.session_replay.Replay`,
this invokes the actual LemonCrow tool that would have replaced each recorded
native call, and attaches the real output so the replay shows the genuine
result — not a hand-written label.

Safety contract (never mutate, never execute):

============  ==========================  =============================
Native call   LemonCrow tool                Mode
============  ==========================  =============================
Grep / Glob   code_search                 REAL (read-only index query)
Read          read (outline/budgeted)     REAL (read-only)
WebFetch      web_fetch                    REAL (network, SSRF-guarded)
Bash          bash (classify only)        PREVIEW — command is NOT run
Edit/Write    edit                         PREVIEW — diff, NOT written
============  ==========================  =============================

Every call is wrapped so a failure (missing repo/index, network error) degrades
to the structural view instead of breaking the replay.
"""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.session_replay import (
    Replay,
    _grep_query,
    _is_grep,
    _read_path,
    _tool_name,
)


def enrich_replay(replay: Replay, repo_root: Path, *, allow_network: bool = True) -> Replay:
    """Attach real LemonCrow tool output to a replay in place, then return it."""
    engine = _build_engine(repo_root)
    collapsed = set(replay.collapsed_indices)
    batched = set(replay.batched_indices)

    # Episodes: run the ONE real code_search that replaces each grep/read loop.
    for ep in replay.episodes:
        ep.live_result = _real_code_search(engine, ep.query, endpoint=_episode_endpoint(replay, ep))

    # Batches: N adjacent reads/edits -> one LemonCrow read(files=[...])/edit(edits=[...]).
    for batch in replay.batches:
        batch.live_result = _batch_lemoncrow(replay, batch)

    # Standalone tool turns (not collapsed or batched): real/preview/simulated output.
    for idx, turn in enumerate(replay.turns):
        if idx in collapsed or idx in batched:
            continue
        if turn.get("kind") not in ("tool_call", "file_edit", "shell_command"):
            continue
        turn["lemoncrow"] = _enrich_turn(turn, engine, repo_root, replay.tool_results, allow_network=allow_network)

    # Recurse into subagent (sidechain) replays so they enrich too.
    for sub in replay.subagent_replays:
        enrich_replay(sub, repo_root, allow_network=allow_network)
    return replay


def _batch_lemoncrow(replay: Replay, batch: Any) -> dict[str, Any]:
    files: list[str] = []
    for i in batch.turn_indices:
        turn = replay.turns[i]
        path = turn.get("path") or _read_path(turn)
        files.append(str(path) if path else "?")
    call = f"{batch.kind}(files=[{len(files)}])" if batch.kind == "read" else f"edit(edits=[{len(files)}])"
    return {
        "tool": batch.kind,
        "mode": "batch",
        "call": call,
        "files": files,
        "count": len(files),
        "note": (f"LemonCrow {batch.kind} takes a batch, so these {len(files)} calls become one."),
    }


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


def _build_engine(repo_root: Path) -> Any | None:
    # Lexical-only, no network/embeddings — fast and deterministic.
    os.environ.setdefault("LEMONCROW_EXPLORE_SEMANTIC", "0")
    os.environ.setdefault("LEMONCROW_ZOEKT_MODE", "off")
    try:
        from lemoncrow.pro.capabilities.code_context.engine import CodeContextEngine

        return CodeContextEngine(repo_root, autosync_enabled=False)
    except Exception:  # noqa: BLE001 - engine is best-effort
        return None


def _real_code_search(engine: Any | None, query: str, *, endpoint: str | None) -> dict[str, Any]:
    if engine is None or not query or query == "(unknown)":
        return {"tool": "code_search", "mode": "unavailable", "query": query}
    try:
        res = engine.tool_explore(query, max_files=4, auto_index=True)
    except Exception as exc:  # noqa: BLE001
        return {"tool": "code_search", "mode": "error", "query": query, "error": str(exc)[:200]}

    hits: list[dict[str, Any]] = []
    for pt in (res.get("entry_points") or [])[:5]:
        if isinstance(pt, dict):
            hits.append(
                {
                    "path": pt.get("path"),
                    "line": pt.get("line"),
                    "end_line": pt.get("end_line"),
                    "name": pt.get("name") or pt.get("qualified_name"),
                    "kind": pt.get("kind"),
                    "score": round(float(pt.get("score", 0) or 0), 1),
                }
            )
    if not hits:
        for f in (res.get("files") or [])[:3]:
            for sym in (f.get("symbols") or [])[:1]:
                hits.append(
                    {
                        "path": f.get("path"),
                        "line": sym.get("line"),
                        "end_line": sym.get("end_line"),
                        "name": sym.get("name") or sym.get("qualified_name"),
                        "kind": sym.get("kind"),
                        "score": None,
                    }
                )
    matched = bool(endpoint) and any(_paths_match(h.get("path"), endpoint) for h in hits)
    return {
        "tool": "code_search",
        "mode": "real",
        "query": query,
        "exact_match": bool(res.get("exact_match")),
        "hits": hits,
        "endpoint": endpoint,
        "matched_endpoint": matched,
    }


def _episode_endpoint(replay: Replay, ep: Any) -> str | None:
    """The file the agent reached right after the loop (edit or targeted read)."""
    for idx in range(ep.after_index + 1, len(replay.turns)):
        turn = replay.turns[idx]
        if turn.get("kind") == "file_edit" and turn.get("path"):
            return str(turn["path"])
        if turn.get("kind") == "tool_call":
            path = _read_path(turn)
            if path:
                return path
            break
    return None


def _paths_match(a: Any, b: Any) -> bool:
    if not a or not b:
        return False
    sa, sb = str(a), str(b)
    return sa.endswith(sb) or sb.endswith(sa) or Path(sa).name == Path(sb).name


# --------------------------------------------------------------------------- #
# Per-turn enrichment
# --------------------------------------------------------------------------- #


def _enrich_turn(
    turn: dict[str, Any],
    engine: Any | None,
    repo_root: Path,
    tool_results: dict[str, str],
    *,
    allow_network: bool,
) -> dict[str, Any]:
    kind = turn.get("kind")
    name = _tool_name(turn).lower()
    if kind == "file_edit":
        return _preview_edit(turn)
    if kind == "shell_command":
        recorded = tool_results.get(str(turn.get("tool_use_id") or ""), "")
        return _simulate_bash(turn, recorded)
    if _is_grep(turn):
        return _real_code_search(engine, _grep_query(turn), endpoint=None)
    if "read" in name or name == "cat":
        return _real_read(turn, repo_root)
    if "webfetch" in name or "web_fetch" in name or "fetch" in name:
        return _real_web_fetch(turn, allow_network=allow_network)
    return {"tool": name or "tool", "mode": "skipped"}


def _preview_edit(turn: dict[str, Any]) -> dict[str, Any]:
    diff = str(turn.get("diff") or turn.get("content") or "").strip()
    hunks = sum(1 for line in diff.splitlines() if line[:1] in ("+", "-") and not line.startswith(("+++", "---")))
    return {
        "tool": "edit",
        "mode": "preview",
        "note": "LemonCrow edit is verified in-memory and returns a terse result; the file is NOT written by replay.",
        "path": turn.get("path"),
        "applied": [str(turn.get("path") or "")],
        "changed_lines": hunks,
        "diff": diff[:4000],
    }


def _classify_bash(command: str) -> dict[str, Any]:
    decision: dict[str, Any] = {"tool": "bash", "command": command[:300]}
    try:
        from lemoncrow.pro.capabilities.tool_supervision.bash_exec import classify_command

        pol = classify_command(command)
        decision["category"] = str(getattr(pol, "category", "") or "")
        decision["action"] = str(getattr(pol, "action", "") or "")
        rewritten = getattr(pol, "rewrite_target", None) or getattr(pol, "rewritten_command", None)
        if rewritten and str(rewritten).strip() and str(rewritten).strip() != command:
            decision["rewrite"] = str(rewritten).strip()[:300]
    except Exception:  # noqa: BLE001
        pass
    return decision


def _simulate_bash(turn: dict[str, Any], recorded_output: str) -> dict[str, Any]:
    """Apply LemonCrow's real bash output-capper to the RECORDED output (no re-run)."""
    command = str(turn.get("content") or "").strip()
    out = _classify_bash(command)
    if not recorded_output:
        out["mode"] = "preview"
        out["note"] = "Command is NOT executed by replay; classified only (no recorded output to compact)."
        return out
    try:
        from lemoncrow.pro.capabilities.tool_supervision.bash_exec import compact_host_bash_output

        rr = compact_host_bash_output(command, recorded_output, "", 0)
        compacted = str(getattr(rr, "stdout", "") or "")
        before, after = len(recorded_output), len(compacted)
        from lemoncrow.pro.capabilities.prompt_compilation.tokens import approx_tokens

        before_tok, after_tok = approx_tokens(recorded_output), approx_tokens(compacted)
        out.update(
            mode="simulated",
            output=compacted[:2500],
            before_chars=before,
            after_chars=after,
            chars_omitted=int(getattr(rr, "chars_omitted", 0) or max(0, before - after)),
            before_tokens=before_tok,
            after_tokens=after_tok,
            tokens_omitted=max(0, before_tok - after_tok),
            lines_omitted=int(getattr(rr, "lines_omitted", 0) or 0),
            truncated=bool(getattr(rr, "truncated", False)),
            note="LemonCrow bash capping applied to the recorded output; the command is NOT re-run.",
        )
    except Exception:  # noqa: BLE001
        out["mode"] = "preview"
        out["note"] = "Command is NOT executed by replay; classified only."
    return out


def _real_read(turn: dict[str, Any], repo_root: Path) -> dict[str, Any]:
    path = _read_path(turn)
    if not path:
        return {"tool": "read", "mode": "skipped"}
    target = (repo_root / path) if not os.path.isabs(path) else Path(path)
    if not target.is_file():
        return {"tool": "read", "mode": "missing", "path": path}
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"tool": "read", "mode": "error", "path": path}
    total_lines = text.count("\n") + 1
    outline = _py_outline(text) if target.suffix == ".py" else []
    return {
        "tool": "read",
        "mode": "real",
        "path": path,
        "total_lines": total_lines,
        "outline": outline[:25],
        "note": (
            f"LemonCrow read returns a {len(outline)}-symbol outline / exact ranges instead of the "
            f"{total_lines}-line whole-file dump."
            if outline
            else f"LemonCrow read budgets the {total_lines}-line file to the relevant range."
        ),
    }


def _py_outline(text: str) -> list[str]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            out.append(f"class {node.name}  (L{node.lineno})")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append(f"  def {sub.name}  (L{sub.lineno})")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.append(f"def {node.name}  (L{node.lineno})")
    return out


def _real_web_fetch(turn: dict[str, Any], *, allow_network: bool) -> dict[str, Any]:
    args = turn.get("arguments") or {}
    url = ""
    if isinstance(args, dict):
        url = str(args.get("url") or args.get("uri") or "").strip()
    if not url:
        return {"tool": "web_fetch", "mode": "skipped"}
    if not allow_network:
        return {"tool": "web_fetch", "mode": "skipped", "url": url, "note": "network disabled (--no-network)"}
    try:
        from lemoncrow.core.capabilities.web_fetch import fetch_url

        res = fetch_url(url, summary=True, max_chars=800)
        content = str((res or {}).get("content") or "")
        return {"tool": "web_fetch", "mode": "real", "url": url, "content": content[:800]}
    except Exception as exc:  # noqa: BLE001
        return {"tool": "web_fetch", "mode": "error", "url": url, "error": str(exc)[:200]}
