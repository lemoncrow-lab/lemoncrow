#!/usr/bin/env python3
"""Stop hook — session summary.

Reads the hook payload (stdin: JSON with session_id, transcript_path).

Behavior:
1. Discussion-only session (no code-editing tools used in the transcript) →
   show plain stats under a "Session stats:" header.
2. Code work happened → show stats under an "lc session complete." header.

Token and tool-call counts are read directly from the Claude Code
transcript JSONL at `transcript_path`.
"""

from __future__ import annotations

import contextlib
import datetime
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

# Route hook logs to a file so tracebacks never leak to Claude Code's
# hook stderr pipeline. Fall back to NullHandler if the path can't be opened.
_log_path = (
    Path(os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT") or Path.home() / ".lemoncrow")
    / "stop_hook.log"
)
try:
    _log_path.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(_log_path),
        level=logging.ERROR,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
except (OSError, ValueError):
    logging.root.addHandler(logging.NullHandler())

logger = logging.getLogger(__name__)

# Tools that indicate real code work (not just discussion / exploration).
# Sessions that only used Read, Bash (read-only), Glob, WebFetch, etc. are
# classified as "discussion" and do not require a trace.
CODE_EDITING_TOOLS: frozenset[str] = frozenset(
    {
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
        "TodoWrite",
    }
)

# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _workspace_key(path: str) -> str:
    import re
    from hashlib import sha256
    from pathlib import Path as _Path

    resolved = _Path(path).expanduser().resolve()
    home = _Path.home().resolve()
    try:
        parts = resolved.relative_to(home).parts
    except ValueError:
        parts = [p for p in resolved.parts if p and p != "/"]
    sanitized = [re.sub(r"[^a-zA-Z0-9.\-_]", "-", p) for p in parts if p]
    label = re.sub(r"-{2,}", "-", "-".join(sanitized)).strip("-")
    if len(label) > 120:
        label = label[:110].rstrip("-") + "--" + sha256(str(resolved).encode()).hexdigest()[:6]
    return label or sha256(str(resolved).encode()).hexdigest()[:12]


def _state_path() -> Path:
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    h = _workspace_key(workspace)
    root = Path(
        os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT") or Path.home() / ".lemoncrow"
    )
    return root / "workspaces" / h / "session_state.json"


def _load_state() -> dict[str, Any]:
    sp = _state_path()
    if not sp.exists():
        return {}
    try:
        result = json.loads(sp.read_text("utf-8"))
        return result if isinstance(result, dict) else {}
    except Exception:
        logger.exception("Failed to load session state")
        return {}


_BASH_STOP_ACK_WINDOW_S = 30.0


def _proc_stat_fields(pid: int) -> list[str] | None:
    """Fields of /proc/<pid>/stat from the state field on (index 0 == state).

    The comm field (2nd) may contain spaces and parentheses, so split on the
    text after the final ')' rather than the whole line -- a plain .split()
    misaligns every field for such a process.
    """
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    rparen = raw.rfind(")")
    if rparen == -1:
        return None
    return raw[rparen + 2 :].split()


def _system_btime() -> float | None:
    try:
        for line in Path("/proc/stat").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("btime "):
                return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        return None
    return None


def _pid_alive(pid: object, started_at: object = None) -> bool:
    try:
        value = int(pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        fields = _proc_stat_fields(value)
        if fields:
            # fields[0] == process state; a 'Z' (zombie: exited, not yet
            # reaped) still answers os.kill(pid, 0), so reject it explicitly.
            if fields[0] == "Z":
                return False
            # Reused-PID guard: a leaked registration whose pid the OS later
            # handed to an unrelated process. field 22 (index 19 here) is the
            # start time in clock ticks since boot; a process that started
            # after the command was registered cannot be that command.
            if started_at is not None and len(fields) > 19:
                clk_tck = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 0
                btime = _system_btime()
                try:
                    recorded = float(started_at)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    recorded = 0.0
                if clk_tck and clk_tck > 0 and btime is not None and recorded > 0.0:
                    try:
                        proc_start = btime + int(fields[19]) / clk_tck
                    except ValueError:
                        proc_start = 0.0
                    # 2s slack absorbs integer-btime rounding + launch/record skew.
                    if proc_start > recorded + 2.0:
                        return False
        os.kill(value, 0)
        return True
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def _bash_stop_warning_path(session_id: str) -> Path:
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd())
    key = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in session_id).strip("-")
    if not key:
        key = _workspace_key(workspace)
    return _lemoncrow_root() / "stop_warnings" / f"{key}.bash.json"


def _running_managed_bash(payload: dict[str, Any]) -> list[dict[str, Any]]:
    workspace_raw = str(payload.get("cwd") or os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd())
    workspace = str(Path(workspace_raw).expanduser().resolve())
    session_id = str(payload.get("session_id") or "")
    found: dict[str, dict[str, Any]] = {}
    sessions_dir = _lemoncrow_root() / "mcp_sessions"
    with contextlib.suppress(OSError):
        for registration in sessions_dir.glob("*.json"):
            try:
                data = json.loads(registration.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            # Skip registrations whose owning MCP server process is gone: a
            # hard-killed MCP leaves its file behind with orphaned managed_bash
            # rows that would otherwise block Stop forever.
            if not _pid_alive(data.get("pid")):
                continue
            registered_workspace = str(data.get("workspace") or "")
            if registered_workspace:
                with contextlib.suppress(OSError):
                    registered_workspace = str(Path(registered_workspace).expanduser().resolve())
                if registered_workspace != workspace:
                    continue
            registered_session = str(data.get("claude_session_id") or "")
            if session_id and registered_session and registered_session != session_id:
                continue
            for row in data.get("managed_bash", []):
                if not isinstance(row, dict) or not _pid_alive(row.get("pid"), row.get("started_at")):
                    continue
                command_id = str(row.get("session_id") or "")
                if command_id:
                    found[command_id] = row
    return list(found.values())


def _format_bash_jobs(jobs: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for row in jobs[:8]:
        command_id = str(row.get("session_id") or "")
        logs = [str(row.get(key) or "") for key in ("log_file", "log_file_stderr")]
        logs = [path for path in logs if path]
        suffix = f"; logs: {', '.join(logs)}" if logs else ""
        lines.append(f"id={command_id}{suffix}")
    if len(jobs) > 8:
        lines.append(f"... {len(jobs) - 8} more")
    return "\n".join(lines)


def _bash_stop_guard(payload: dict[str, Any]) -> tuple[dict[str, str] | None, str]:
    """Warn once for foreground Bash, then acknowledge Stop for 30 seconds."""
    jobs = _running_managed_bash(payload)
    foreground = [row for row in jobs if not bool(row.get("explicit_background"))]
    background = [row for row in jobs if bool(row.get("explicit_background"))]
    session_id = str(payload.get("session_id") or "")
    warning_path = _bash_stop_warning_path(session_id)
    background_info = ""
    if background:
        background_info = (
            f"Background Bash still running ({len(background)}, preserved after MCP exit):\n"
            f"{_format_bash_jobs(background)}"
        )
    if not foreground:
        with contextlib.suppress(OSError):
            warning_path.unlink(missing_ok=True)
        return None, background_info

    now = time.time()
    foreground_ids = sorted(str(row.get("session_id") or "") for row in foreground)
    previous: dict[str, Any] = {}
    with contextlib.suppress(OSError, json.JSONDecodeError):
        loaded = json.loads(warning_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            previous = loaded
    warned_at = float(previous.get("warned_at") or 0.0)
    warned_ids = sorted(str(value) for value in previous.get("command_ids", []))
    if warned_ids == foreground_ids and 0.0 <= now - warned_at <= _BASH_STOP_ACK_WINDOW_S:
        with contextlib.suppress(OSError):
            warning_path.unlink(missing_ok=True)
        info = (
            f"Foreground Bash stopping with MCP ({len(foreground)}; process groups will be killed):\n"
            f"{_format_bash_jobs(foreground)}"
        )
        if background_info:
            info = f"{info}\n{background_info}"
        return None, info

    with contextlib.suppress(OSError):
        warning_path.parent.mkdir(parents=True, exist_ok=True)
        warning_path.write_text(
            json.dumps({"warned_at": now, "command_ids": foreground_ids}),
            encoding="utf-8",
        )
    reason = (
        f"WARNING: {len(foreground)} foreground Bash command(s) still running. "
        "Stop again within 30 seconds to exit and kill their process groups; "
        "after 30 seconds this warning resets.\n"
        f"{_format_bash_jobs(foreground)}"
    )
    if background_info:
        reason = f"{reason}\n{background_info}"
    return {"decision": "block", "reason": reason}, background_info


# ---------------------------------------------------------------------------
# RunLedger token-count writer (fail-open)
# ---------------------------------------------------------------------------


def _lemoncrow_root() -> Path:
    state = _load_state()
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if root:
        return Path(root)
    if state.get("lemoncrow_root"):
        return Path(state["lemoncrow_root"])
    return Path.home() / ".lemoncrow"


def _sessions_root() -> Path:
    """Session store root -- the plain lemoncrow root, matching the MCP writer and the
    sibling hooks (session_start / post_tool_use). Sessions are keyed by a globally
    unique id, so they are NOT workspace-scoped: the previous ``workspaces/<key>``
    path silently missed the canonical run.json (its ``if not exists: return`` guards
    no-op'd) whenever LEMONCROW_WORKSPACE_ROOT/CLAUDE_WORKSPACE_ROOT was set, dropping
    the session-end token event, cost row, and enrichment writes."""
    return _lemoncrow_root()


def _write_token_event(stats: dict[str, Any], session_id: str | None = None) -> None:
    """Append a session_stats note event to the active run file."""
    if not session_id:
        # Fallback: read from workspace state (only when caller didn't supply it).
        state = _load_state()
        session_id = state.get("session_id") or state.get("active_session_id")
    if not session_id:
        return
    try:
        from lemoncrow.core.foundation.paths import session_dir

        run_file = session_dir(_sessions_root(), "claude", session_id) / "run.json"
    except ImportError:
        return
    if not run_file.exists():
        return
    try:
        data = json.loads(run_file.read_text("utf-8"))
    except Exception:
        logger.exception("Failed to load run file in _write_token_event")
        return

    events: list[dict[str, Any]] = data.setdefault("events", [])
    events.append(
        {
            "kind": "note",
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
            "summary": (
                f"session end — {stats['total_tokens']:,} tokens "
                f"(+{stats['output_tokens']:,} out), "
                f"~${stats['est_cost_usd']:.4f}"
            ),
            "payload": {
                "input_tokens": stats["input_tokens"],
                "output_tokens": stats["output_tokens"],
                "total_tokens": stats["total_tokens"],
                "est_cost_usd": stats["est_cost_usd"],
                "tool_calls": stats["tool_calls"],
                "top_tools": dict(sorted(stats["tools_used"].items(), key=lambda x: -x[1])[:8]),
                "event": "Stop",
            },
        }
    )
    data["events"] = events

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=run_file.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = tmp.name
        Path(tmp_path).replace(run_file)
    except Exception:
        logger.exception("Failed to write token event")
        if tmp_path:
            with contextlib.suppress(Exception):
                Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Transcript helpers — thin wrappers around the shared savings_summary module.
# ---------------------------------------------------------------------------


def _is_real_model_id(raw: object) -> bool:
    try:
        from lemoncrow.core.capabilities.savings_summary import is_real_model

        return is_real_model(raw)
    except (ImportError, ModuleNotFoundError):
        return bool(raw) and raw != "unknown"


def _resolve_model_id(raw: str | None) -> str:
    try:
        from lemoncrow.core.capabilities.savings_summary import resolve_model_id

        return resolve_model_id(raw or "")
    except (ImportError, ModuleNotFoundError):
        return raw or "unknown"


def _estimate_cost_usd(
    *,
    model_id: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> float:
    try:
        from lemoncrow.core.capabilities.savings_summary import estimate_cost_usd

        return estimate_cost_usd(
            model_id=model_id,
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            cache_read_tokens=int(cache_read_tokens or 0),
            cache_write_tokens=int(cache_write_tokens or 0),
        )
    except (ImportError, ModuleNotFoundError):
        return 0.0


def _read_transcript_stats(transcript_path: str) -> dict[str, Any] | None:
    """Parse the Claude Code transcript JSONL and return session stats.

    Delegates to savings_summary.read_transcript_stats() for all parsing,
    then converts the TranscriptStats dataclass to the dict format stop.py
    has always returned.
    """
    try:
        from lemoncrow.core.capabilities.savings_summary import TranscriptStats, read_transcript_stats
    except (ImportError, ModuleNotFoundError):
        return None
    stats: TranscriptStats | None = read_transcript_stats(transcript_path)
    if stats is None:
        return None

    return {
        "tool_calls": stats.tool_calls,
        "turns": stats.turns,
        "input_tokens": stats.input_tokens,
        "output_tokens": stats.output_tokens,
        "cache_read_tokens": stats.cache_read_tokens,
        "cache_write_tokens": stats.cache_write_tokens,
        "total_tokens": stats.input_tokens + stats.output_tokens + stats.cache_read_tokens + stats.cache_write_tokens,
        "est_cost_usd": stats.est_cost_usd,
        "model": stats.model,
        "last_model": stats.last_model,
        "models_used": stats.models_used,
        "tools_used": stats.tools_used,
    }


def _extract_session_title(transcript_path: str) -> str | None:
    """Return the first real user message from the transcript as session title.

    Skips:
    - ``local-command-caveat`` system injections
    - Slash-command entries (text starts with ``/`` or wrapped in XML tags)
    - Entries with ``parentUuid`` set (continuations, not root turns)

    Caps the title at 500 characters.
    """
    if not transcript_path:
        return None
    p = Path(transcript_path)
    if not p.exists():
        return None

    import re

    try:
        with p.open(encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    logger.exception("Failed to parse transcript entry in _extract_session_title")
                    continue

                if entry.get("type") != "user":
                    continue
                # Only root turns (parentUuid is null)
                if entry.get("parentUuid") is not None:
                    continue

                msg = entry.get("message", {}) or {}
                content = msg.get("content", "")

                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            break

                # Skip system injections and empty content
                if "local-command-caveat" in text:
                    continue

                # Strip XML command tags (e.g. <command-name>…</command-name>)
                clean = re.sub(r"<[^>]+>.*?</[^>]+>", "", text, flags=re.DOTALL).strip()

                # Skip pure slash-command inputs
                if not clean or clean.startswith("/"):
                    continue

                return clean[:500]
    except Exception:
        logger.exception("Failed to extract session title")
        return None
    return None


def _extract_user_prompts(transcript_path: str) -> list[str]:
    """Return all real user prompts from the transcript (capped at 2 KB each)."""
    _MAX_PROMPT = 2048
    if not transcript_path:
        return []
    p = Path(transcript_path)
    if not p.exists():
        return []

    import re

    prompts: list[str] = []
    try:
        with p.open(encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except Exception:
                    logger.exception("Failed to parse transcript entry in _extract_user_prompts")
                    continue

                if entry.get("type") != "user":
                    continue
                if entry.get("isSidechain"):
                    continue

                msg = entry.get("message", {}) or {}
                content = msg.get("content", "")

                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            break

                if "local-command-caveat" in text:
                    continue

                clean = re.sub(r"<[^>]+>.*?</[^>]+>", "", text, flags=re.DOTALL).strip()
                if not clean or clean.startswith("/"):
                    continue

                prompts.append(clean[:_MAX_PROMPT])
    except Exception:
        logger.exception("Failed to extract user prompts")
        pass
    return prompts


def _extract_edited_paths(transcript_path: str) -> list[str]:
    """Full paths of files edited this session (from edit/Write tool_use calls)."""
    if not transcript_path:
        return []
    p = Path(transcript_path)
    if not p.exists():
        return []
    seen: list[str] = []
    out: set[str] = set()
    try:
        with p.open(encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
                if entry.get("type") != "assistant":
                    continue
                content = (entry.get("message", {}) or {}).get("content", "")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_use":
                        continue
                    low = str(block.get("name") or "").split("__")[-1].lower()
                    if low not in ("edit", "write", "multiedit") and not low.endswith("edit"):
                        continue
                    inp = block.get("input") or {}
                    if not isinstance(inp, dict):
                        continue
                    cands: list[str] = []
                    for key in ("file_path", "path", "filename"):
                        v = inp.get(key)
                        if isinstance(v, str) and v:
                            cands.append(v)
                    edits = inp.get("edits")
                    if isinstance(edits, list):
                        for e in edits:
                            if isinstance(e, dict):
                                fp = e.get("file_path") or e.get("path")
                                if isinstance(fp, str) and fp:
                                    cands.append(fp)
                    for c in cands:
                        c = c.split("#")[0].split(":L")[0]
                        if c and c not in out:
                            out.add(c)
                            seen.append(c)
    except Exception:
        logger.exception("Failed to extract edited paths")
    return seen


def _format_deferred_edits(transcript_path: str) -> None:
    """When LEMONCROW_DEFER_EDIT_HOOKS was on, the edit tool skipped the mutating
    format / organize-imports steps so the formatter could not reflow files
    mid-session and break the agent's read anchors. Run them once now, at Stop,
    over the files edited this session. Fail-open: never break the Stop hook."""
    if os.environ.get("LEMONCROW_DEFER_EDIT_HOOKS", "0").strip().lower() not in {"1", "true", "on", "yes"}:
        return
    paths = _extract_edited_paths(transcript_path)
    if not paths:
        return
    try:
        from lemoncrow.core.capabilities.tool_supervision.post_edit_hooks import (
            HookConfig,
            run_post_edit_hooks,
        )

        root = Path(os.environ.get("CLAUDE_WORKSPACE_ROOT", os.getcwd()))
        resolved = [str(root / pp if not Path(pp).is_absolute() else Path(pp)) for pp in paths]
        resolved = [pp for pp in resolved if Path(pp).exists()]
        if resolved:
            run_post_edit_hooks(
                resolved,
                repo_root=root,
                config=HookConfig(run_lint_autofix=False, run_diagnostics=False),
            )
    except Exception:
        logger.exception("Failed to run deferred format at Stop")


def _write_session_enrichment(
    session_id: str,
    session_title: str | None,
    user_prompts: list[str],
    transcript_path: str,
) -> None:
    """Append session_metadata note to the active run file.

    Written by the Stop hook so the run file always contains the real session
    title (first user message) and the full prompt history, regardless of what
    the agent reported via ``record``.
    """
    if not session_id:
        return
    try:
        from lemoncrow.core.foundation.paths import session_dir

        run_file = session_dir(_sessions_root(), "claude", session_id) / "run.json"
    except ImportError:
        return
    if not run_file.exists():
        return
    try:
        data = json.loads(run_file.read_text("utf-8"))
    except Exception:
        logger.exception("Failed to load run file in _write_session_enrichment")
        return

    # Update top-level task with session_title when the agent left it blank
    if session_title and not (data.get("task") or "").strip():
        data["task"] = session_title

    events: list[dict[str, Any]] = data.setdefault("events", [])
    events.append(
        {
            "kind": "note",
            "at": datetime.datetime.now(datetime.UTC).isoformat(),
            "summary": f"session_title: {(session_title or '')[:80]}",
            "payload": {
                "session_title": session_title,
                "transcript_path": transcript_path,
                "user_prompts": user_prompts[:50],  # cap at 50 turns
                "prompt_count": len(user_prompts),
                "event": "SessionEnrichment",
            },
        }
    )
    data["events"] = events

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=run_file.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp:
            json.dump(data, tmp, indent=2)
            tmp_path = tmp.name
        Path(tmp_path).replace(run_file)
    except Exception:
        logger.exception("Failed to write session enrichment")
        if tmp_path:
            with contextlib.suppress(Exception):
                Path(tmp_path).unlink(missing_ok=True)


def _load_session_aggregate(session_id: str) -> dict[str, Any]:
    if not session_id:
        return {}
    try:
        from lemoncrow.core.capabilities.plugin_runtime import aggregate_session_stats

        aggregate = aggregate_session_stats(_lemoncrow_root(), session_id=session_id)
        return aggregate if isinstance(aggregate, dict) else {}
    except Exception:
        logger.exception("Failed to load session aggregate")
        return {}


def _merge_session_aggregate(stats: dict[str, Any] | None, aggregate: dict[str, Any] | None) -> dict[str, Any] | None:
    if not aggregate:
        return stats

    if stats is None:
        stats = {
            "tool_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": 0,
            "est_cost_usd": 0.0,
            "tools_used": {},
        }

    usage_raw = aggregate.get("usage")
    usage: dict[str, Any] = usage_raw if isinstance(usage_raw, dict) else {}
    # Transcript is authoritative; aggregate is a fallback for zero values.
    # Never let potentially-inflated aggregate values override correct transcript totals.
    stats["tool_calls"] = int(stats.get("tool_calls", 0) or 0) or int(aggregate.get("total_tool_calls", 0) or 0)
    stats["input_tokens"] = int(stats.get("input_tokens", 0) or 0) or int(usage.get("input_tokens", 0) or 0)
    stats["output_tokens"] = int(stats.get("output_tokens", 0) or 0) or int(usage.get("output_tokens", 0) or 0)
    stats["cache_read_tokens"] = int(stats.get("cache_read_tokens", 0) or 0) or int(
        usage.get("cache_read_tokens", 0) or 0
    )
    stats["cache_write_tokens"] = int(stats.get("cache_write_tokens", 0) or 0) or int(
        usage.get("cache_write_tokens", 0) or 0
    )
    # Pre-compact usage: token totals from turns that may have been erased when /compact
    # rewrote the transcript.  The compact.py PreCompact hook stores the HIGH-WATER MARK
    # (max across all compacts, not a running sum) so pre_compact represents the full
    # session state at the most recent compact.
    #
    # Merge strategy — delta-only (add only what the transcript is missing):
    #   • Claude Code preserves the full conversation after /compact (old turns remain
    #     in the JSONL below compact_boundary markers), so read_transcript_stats() already
    #     accounts for every token via msg_id dedup.  In that case pre_compact ≤ transcript
    #     and the delta is 0 — nothing is added.
    #   • If compact DID erase entries (older behaviour), pre_compact > transcript and we
    #     recover only the truly missing portion.
    #
    # The old "unconditional add" inflated cost by up to Nx (N = compact count) because
    # it summed N growing snapshots on top of a transcript that already contained them all.
    pre_compact = aggregate.get("pre_compact_usage")
    if isinstance(pre_compact, dict):
        for _field in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
            _pre = int(pre_compact.get(_field, 0) or 0)
            _cur = int(stats[_field])
            if _pre > _cur:
                stats[_field] = _pre  # transcript was missing tokens; use the larger snapshot
        _pre_cost = float(pre_compact.get("est_cost_usd", 0.0) or 0.0)
        _cur_cost = float(stats.get("est_cost_usd", 0.0) or 0.0)
        if _pre_cost > _cur_cost:
            stats["est_cost_usd"] = _pre_cost
    stats["total_tokens"] = (
        int(stats["input_tokens"])
        + int(stats["output_tokens"])
        + int(stats["cache_read_tokens"])
        + int(stats["cache_write_tokens"])
    )
    return stats


def _is_task_session(stats: dict[str, Any] | None, session_aggregate: dict[str, Any] | None = None) -> bool:
    """Return True only if code-editing tools were used this session.

    A session that only called Read, Bash (read-only), Glob, WebFetch,
    WebSearch, or had zero tool calls is classified as a "discussion" session
    and does not require an LemonCrow trace.
    """
    if session_aggregate and int(session_aggregate.get("edit_tool_calls", 0) or 0) > 0:
        return True
    if stats is None or stats.get("tool_calls", 0) == 0:
        return False
    tools_used: set[str] = set(stats.get("tools_used", {}).keys())
    return bool(CODE_EDITING_TOOLS & tools_used)


def _write_session_cost(
    session_id: str,
    cost_usd: float,
    total_tokens: int,
    carry_usd: float = 0.0,
    carry_tokens: int = 0,
) -> None:
    """Append a session-end cost row to savings.jsonl for historical spend tracking.

    The row has ``kind=="session_end"`` so ``_read_historical_savings`` in
    savings_summary.py can accumulate actual spend and carry for historical
    statusline frames without touching the savings totals.
    """
    if not session_id or cost_usd <= 0:
        return
    try:
        from lemoncrow.core.foundation.paths import session_dir

        path = session_dir(_sessions_root(), "claude", session_id) / "savings.jsonl"
    except ImportError:
        return
    if not path.exists():
        return  # no savings sidecar → session produced no MCP events; skip
    row = {
        "kind": "session_end",
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
        "est_cost_usd": round(cost_usd, 6),
        "total_tokens": int(total_tokens or 0),
        "carry_usd": round(carry_usd, 6),
        "carry_tokens": int(carry_tokens or 0),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _sidecar_path(session_id: str) -> Path | None:
    try:
        from lemoncrow.core.foundation.paths import session_dir

        return session_dir(_sessions_root(), "claude", session_id) / "savings.jsonl"
    except ImportError:
        return None


def _refresh_statusline_frames(session_id: str) -> None:
    """Rewrite the session's statusline frames from the just-updated ledger.

    The MCP server refreshes ``statusline_frames`` only when a tool call
    appends a savings row, so rows written HERE at Stop time (output_style,
    session cost/carry) would otherwise not surface until the next turn's
    first tool call -- exactly while the user is idle at the prompt looking
    at the statusline. Display-only; caller suppresses all failures.
    """
    if not session_id:
        return
    import time

    from lemoncrow.core.capabilities.savings_summary import savings_frames
    from lemoncrow.core.foundation.paths import find_session_dir

    root = _sessions_root()
    seg_dir = find_session_dir(root, session_id)
    if seg_dir is None:
        return
    frames = savings_frames(session_id=session_id, lemoncrow_root=root)
    if not frames:
        return
    (seg_dir / "statusline_frames").write_text("\n".join(frames) + "\n", encoding="utf-8")
    # Legacy single-frame sidecar for older installed statusline.sh.
    (seg_dir / "statusline_segment").write_text(frames[int(time.time() // 5) % len(frames)], encoding="utf-8")


_CODE_FENCE_RE = None  # compiled lazily; stop.py imports re only where needed
_CODEISH_LINE_RE = None  # bare code/diff/JSON lines outside fences — same filter the bench ratio used


def _compressible_prose_chars(text: str) -> int:
    """Chars of genuinely compressible prose: fences gone, code-ish lines gone.

    MUST mirror the filter used to measure the bench ratio (swe-lite
    2026-07-06): the ratio was computed on both arms' output after stripping
    fenced blocks AND bare code/diff/JSON-looking lines, so the runtime basis
    has to strip identically or the credit is applied to a different quantity
    than the one the ratio was measured on.
    """
    stripped = _CODE_FENCE_RE.sub("", text)
    return sum(len(line) for line in stripped.splitlines() if line.strip() and not _CODEISH_LINE_RE.match(line))


def _prose_output_tokens(transcript_path: str) -> int:
    """Estimated compressible PROSE output tokens (reply text blocks only).

    Fixed output is excluded on purpose — telegraphic style compresses the
    reply, nothing else — so the basis drops: fenced code blocks, bare
    code/diff/JSON lines, and THINKING blocks entirely (reasoning volume is
    style-invariant — both bench arms think the same; only reply prose
    differed). Claude Code writes one transcript line per content block
    sharing the message id; blocks are deduped by (msg_id, text-hash) so a
    re-emitted snapshot line never double-counts.
    """
    import re

    global _CODE_FENCE_RE, _CODEISH_LINE_RE
    if _CODE_FENCE_RE is None:
        _CODE_FENCE_RE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
    if _CODEISH_LINE_RE is None:
        _CODEISH_LINE_RE = re.compile(r"^\s*(\+|\-|@@|\{|\}|\[|def |class |import |from \S+ import|#|\$|>>>|\.\.\.)")
    p = Path(transcript_path) if transcript_path else None
    if p is None or not p.exists():
        return 0
    chars = 0
    seen: set[tuple[str, int]] = set()
    try:
        with p.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entry = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                msg = entry.get("message") or {}
                if not isinstance(msg, dict):
                    continue
                mid = str(msg.get("id") or "")
                content = msg.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "text":
                        continue
                    text = str(block.get("text") or "")
                    if not text:
                        continue
                    key = (mid, hash(text))
                    if key in seen:
                        continue
                    seen.add(key)
                    chars += _compressible_prose_chars(text)
    except OSError:
        return 0
    return chars // 4


def _write_output_style_row(session_id: str, stats: dict[str, Any], transcript_path: str) -> None:
    """Credit the telegraphic output style: prose the model did NOT emit.

    Personas instruct telegraphic replies; default-prose Claude answers the
    same content in more words. Credit = measured prose output tokens x
    (ratio - 1), priced at the output rate plus a cache write (the avoided
    prose would have re-entered context). Code output and code fences are
    excluded from the basis. Incremental per Stop fire via the cumulative
    marker on the last ``output_style`` row. ``LEMONCROW_OUTPUT_STYLE_RATIO``
    (<=1 disables) defaults to 2.09 -- measured AND reconciled, not guessed.
    Matched telegraphic Q&A head-to-head (2026-07-08, opus-4-8, 20 prompts x
    5 reps x 2 arms = 200 runs; prose isolated on both arms by stripping
    fences/code-ish lines/the session-title JSON turn): baseline reply prose
    is 2.09x LemonCrow's pooled (40.3k vs 19.2k tokens). Supersedes swe-lite's
    smaller 10-instance measurement. No turn-cut overlap to net out here
    (unlike swe-lite): baseline's extra turns are 100% the benchmark
    harness's title-generation turn (LemonCrow skips it outright, 0/100 vs
    100/100 runs); once that's excluded from both arms, answering-turn
    counts are flat (138 vs 142), so none of the prose delta double-counts
    with the turn_cut row -- the pooled ratio applies directly. Per-prompt
    ratios ranged 1.37x-8.33x across the 20 prompts (median 1.85x). Raw data:
    benchmarks/codebench/results/telegraphic_2026_07_08_5rep/.
    """
    if not session_id:
        return
    try:
        ratio = float(os.environ.get("LEMONCROW_OUTPUT_STYLE_RATIO", "2.09"))
    except ValueError:
        return
    if ratio <= 1.0:
        return
    path = _sidecar_path(session_id)
    if path is None or not path.exists():
        return  # no sidecar → session not LemonCrow-instrumented; nothing to fold into
    prose_tokens = _prose_output_tokens(transcript_path)
    if prose_tokens <= 0:
        return
    prev_cum = 0
    with contextlib.suppress(OSError):
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("kind") == "output_style":
                prev_cum = max(prev_cum, int(row.get("cum_prose_tokens") or 0))
    delta = prose_tokens - prev_cum
    if delta <= 0:
        return
    saved = int(delta * (ratio - 1.0))
    if saved <= 0:
        return
    try:
        from lemoncrow.core.capabilities.pricing import get_model_pricing
        from lemoncrow.core.capabilities.savings_summary import resolve_model_id

        model = str(stats.get("last_model") or stats.get("model") or "")
        pricing = get_model_pricing(resolve_model_id(model)) if model else None
        if pricing is None or not pricing.known or pricing.output <= 0:
            return  # never guess a rate
        usd = pricing.request_cost_usd(output_tokens=saved, cache_write_tokens=saved)
    except Exception:
        logger.exception("Failed to price output-style row")
        return
    row_out = {
        "kind": "output_style",
        "tokens": int(saved),
        "cost_saved_usd": round(usd, 6),
        "model": model,
        "ratio": ratio,
        "cum_prose_tokens": int(prose_tokens),
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row_out) + "\n")


# Avoided host turns per executed turn, measured on the matched swe-lite
# head-to-head (2026-07-06, opus-4-8, 10 instances, reconciled reps):
# baseline needed 152 median turns where LemonCrow needed 123 — (152-123)/123.
# Avoided host turns per executed turn, measured on the swe50 head-to-head
# (2026-06-30, opus-4-8, 50 SWE-bench instances x 5 reps x 2 arms, 489
# successful runs -- supersedes the smaller 10-instance swe-lite sample):
# baseline needed 28.59 avg turns/run where LemonCrow needed 17.41 --
# (28.59-17.41)/17.41. Per-task median across the 50 tasks is 0.695,
# consistent with this pooled figure. Raw data:
# benchmarks/codebench/results/swe50_2026_06_30/.
_TURN_CUT_RATIO_DEFAULT = 0.642


def _write_turn_cut_row(session_id: str, stats: dict[str, Any]) -> None:
    """Bench-calibrated turn-cut credit (``kind == "turn_cut"``).

    Per-call ``calls`` credits (read batching, code_search netting, code-intel
    deferral) only capture the directly observable slice of the turn cut; the
    measured cut on identical tasks is larger. This row tops the session up to
    the bench-measured floor: ``target = turns x ratio`` minus every avoided
    call already in the ledger — same counterfactual, so never double-counted,
    and sessions whose explicit credits already exceed the bench ratio get
    nothing extra. Self-converging across Stop fires: prior turn_cut rows
    count toward the target.

    Each avoided turn is priced exactly like the dispatcher's avoided-call
    rule: one context re-send at the cache-read rate (session's measured
    average context per turn) plus one turn of average output (output rate,
    re-entering context at the cache-write rate). Unknown model → no row,
    never guess a rate. ``LEMONCROW_TURN_CUT_RATIO`` overrides; <=0 disables.
    """
    if not session_id:
        return
    try:
        ratio = float(os.environ.get("LEMONCROW_TURN_CUT_RATIO", str(_TURN_CUT_RATIO_DEFAULT)))
    except ValueError:
        return
    if ratio <= 0:
        return
    path = _sidecar_path(session_id)
    if path is None or not path.exists():
        return  # no sidecar → session not LemonCrow-instrumented; no turn cut to credit
    turns = int(stats.get("turns") or 0)
    if turns <= 0:
        return
    target = int(turns * ratio)
    if target <= 0:
        return
    credited = 0
    with contextlib.suppress(OSError):
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                credited += max(0, int(row.get("calls") or 0))
    delta = target - credited
    if delta <= 0:
        return
    avg_ctx = int(stats.get("cache_read_tokens") or 0) // turns
    avg_out = int(stats.get("output_tokens") or 0) // turns
    if avg_ctx <= 0 and avg_out <= 0:
        return
    try:
        from lemoncrow.core.capabilities.pricing import get_model_pricing
        from lemoncrow.core.capabilities.savings_summary import resolve_model_id

        model = str(stats.get("last_model") or stats.get("model") or "")
        pricing = get_model_pricing(resolve_model_id(model)) if model else None
        if pricing is None or not pricing.known or pricing.cache_read <= 0:
            return  # never guess a rate
        usd = pricing.request_cost_usd(
            cache_read_tokens=delta * avg_ctx,
            output_tokens=delta * avg_out,
            cache_write_tokens=delta * avg_out,
        )
    except Exception:
        logger.exception("Failed to price turn-cut row")
        return
    if usd <= 0:
        return
    row_out = {
        "kind": "turn_cut",
        "calls": int(delta),
        "calls_usd": round(usd, 6),
        "model": model,
        "ratio": ratio,
        "turns": turns,
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row_out) + "\n")


# Baseline's avg cache-read per turn vs LemonCrow's, measured on the same
# swe50 head-to-head as turn_cut above: 26.1k vs 22.4k tokens/turn -- 1.163x.
# See _write_input_style_row's docstring for the full derivation and the
# cross-check against Harbor's independent Terminal-Bench-2.1 suite.
_INPUT_STYLE_RATIO_DEFAULT = 1.16


def _write_input_style_row(session_id: str, stats: dict[str, Any]) -> None:
    """Credit leaner per-turn context: cache-read tokens NOT re-sent.

    Complements ``_write_turn_cut_row`` above rather than overlapping it: that
    credits *whole avoided turns* (each priced at the session's own avg
    context/turn); this credits the other, orthogonal effect measured on the
    same bench -- turns that DO execute still carry less context each,
    because LemonCrow's context stays leaner turn over turn (compressed reads,
    code_search over raw grep/cat, prefix-cache-aware batching) than
    baseline's would. Credit = incremental session cache-read tokens x
    (ratio - 1), priced at the cache-read rate. Incremental per Stop fire via
    the cumulative marker on the last ``input_style`` row, same pattern as
    ``_write_output_style_row``.

    ``LEMONCROW_INPUT_STYLE_RATIO`` (<=1 disables) defaults to 1.16 -- measured
    on the same swe50 head-to-head as the turn_cut ratio above (2026-06-30,
    opus-4-8, 50 SWE-bench instances x 5 reps x 2 arms, 489 successful runs):
    baseline's avg cache-read is 26.1k tokens/turn vs LemonCrow's 22.4k --
    1.163x. That per-turn gap is the residual left over after turn_cut: the
    raw cache-read delta (745.5k vs 390.5k tokens/run, 1.909x total) factors
    as turn count (1.642x, matching 1+0.642) times per-turn leanness
    (1.163x) -- this row's basis is the second factor only, so the two
    credits are multiplicative on disjoint bases and never double-count.
    Cross-checked directionally against the independent Harbor
    Terminal-Bench-2.1 suite (89 real long-horizon tasks, no turn-cut
    decomposition possible there -- see
    context (fresh input + cache, 83 matched tasks with cost data on both
    sides) there is 1.23x baseline/lemoncrow -- same
    direction, smaller than the isolated 1.909x cache-read figure here as
    expected (blended across fresh input and cache-creation too, on a very
    different task distribution/turn profile from SWE-bench code fixes).
    Raw data: benchmarks/codebench/results/swe50_2026_06_30/.
    """
    if not session_id:
        return
    try:
        ratio = float(os.environ.get("LEMONCROW_INPUT_STYLE_RATIO", str(_INPUT_STYLE_RATIO_DEFAULT)))
    except ValueError:
        return
    if ratio <= 1.0:
        return
    path = _sidecar_path(session_id)
    if path is None or not path.exists():
        return  # no sidecar → session not LemonCrow-instrumented; nothing to fold into
    cache_read_tokens = int(stats.get("cache_read_tokens") or 0)
    if cache_read_tokens <= 0:
        return
    prev_cum = 0
    with contextlib.suppress(OSError):
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("kind") == "input_style":
                prev_cum = max(prev_cum, int(row.get("cum_cache_read_tokens") or 0))
    delta = cache_read_tokens - prev_cum
    if delta <= 0:
        return
    saved = int(delta * (ratio - 1.0))
    if saved <= 0:
        return
    try:
        from lemoncrow.core.capabilities.pricing import get_model_pricing
        from lemoncrow.core.capabilities.savings_summary import resolve_model_id

        model = str(stats.get("last_model") or stats.get("model") or "")
        pricing = get_model_pricing(resolve_model_id(model)) if model else None
        if pricing is None or not pricing.known or pricing.cache_read <= 0:
            return  # never guess a rate
        usd = pricing.request_cost_usd(cache_read_tokens=saved)
    except Exception:
        logger.exception("Failed to price input-style row")
        return
    row_out = {
        "kind": "input_style",
        "tokens": int(saved),
        "cost_saved_usd": round(usd, 6),
        "model": model,
        "ratio": ratio,
        "cum_cache_read_tokens": int(cache_read_tokens),
        "ts": datetime.datetime.now(datetime.UTC).isoformat(),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row_out) + "\n")


def _rtk_total_tokens_saved(payload: Any) -> int:
    """Total tokens saved from ``rtk gain --format json`` output.

    rtk v0.43 emits ``{"summary": {"total_saved": N, "avg_savings_pct": ...}}``
    (verified against the real binary). Accept that plus older/alternate
    spellings (``tokens_saved``, ``saved_tokens``, ``total_tokens_saved``) and
    take the maximum — totals dominate per-command entries. Percentage fields
    are excluded.
    """
    best = 0
    stack = [payload]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            for key, value in node.items():
                lk = str(key).lower()
                if isinstance(value, (dict, list)):
                    stack.append(value)
                    continue
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    continue
                if "pct" in lk or "percent" in lk:
                    continue
                if "sav" in lk and ("token" in lk or "total" in lk or lk == "saved"):
                    best = max(best, int(value))
        elif isinstance(node, list):
            stack.extend(node)
    return best


def _credit_rtk_gain(session_id: str, stats: dict[str, Any]) -> None:
    """Fold rtk's own measured savings into the ledger as a bash row.

    When the external-compactor integration routes bash commands through an
    installed ``rtk`` binary, LemonCrow never sees the raw output — rtk does,
    and records raw-vs-filtered tokens in its gain ledger. ``rtk gain`` is
    PROJECT-scoped (cwd), so the probe runs from this session's workspace and
    the cumulative marker in ``rtk_gain_state.json`` is keyed per workspace:
    rtk tokens saved in another repo (or your own terminal use of rtk
    elsewhere) are never attributed to this session, and each project token
    is credited exactly once across that project's sessions. Credit the delta
    since the last credit as a measured ``external_compactor`` row (priced at
    the input rate — content that would have entered context once).
    ``LEMONCROW_RTK_GAIN_CREDIT=0`` disables.
    """
    if not session_id or os.environ.get("LEMONCROW_RTK_GAIN_CREDIT", "1") == "0":
        return
    import shutil
    import subprocess

    rtk_bin = shutil.which("rtk")
    if not rtk_bin:
        return
    path = _sidecar_path(session_id)
    if path is None or not path.exists():
        return
    workspace = os.environ.get("CLAUDE_WORKSPACE_ROOT") or os.getcwd()
    ws_key = _workspace_key(workspace)
    try:
        proc = subprocess.run(
            [rtk_bin, "gain", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=workspace,
        )
        payload = json.loads(proc.stdout or "{}")
    except Exception:
        logger.exception("rtk gain probe failed")
        return
    total = _rtk_total_tokens_saved(payload)
    marker = _lemoncrow_root() / "rtk_gain_state.json"
    credited_map: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        raw_marker = json.loads(marker.read_text(encoding="utf-8"))
        if isinstance(raw_marker, dict) and isinstance(raw_marker.get("credited_by_workspace"), dict):
            credited_map = raw_marker["credited_by_workspace"]
    credited = int(credited_map.get(ws_key) or 0)
    if total < credited:
        credited = 0  # rtk ledger was reset; start over rather than under-credit forever
    delta = total - credited
    usd = 0.0
    if delta > 0:
        try:
            from lemoncrow.core.capabilities.pricing import get_model_pricing
            from lemoncrow.core.capabilities.savings_summary import resolve_model_id

            model = str(stats.get("last_model") or stats.get("model") or "")
            pricing = get_model_pricing(resolve_model_id(model)) if model else None
            if pricing is not None and pricing.known and pricing.input > 0:
                usd = pricing.request_cost_usd(input_tokens=delta)
            else:
                return  # never guess a rate
        except Exception:
            logger.exception("Failed to price rtk gain row")
            return
        row = {
            "kind": "external_compactor",
            "tool": "bash",
            "tokens": int(delta),
            "cost_saved_usd": round(usd, 6),
            "model": model,
            "ts": datetime.datetime.now(datetime.UTC).isoformat(),
        }
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    credited_map[ws_key] = int(max(total, credited))
    tmp = marker.with_suffix(".tmp")
    with contextlib.suppress(OSError):
        tmp.write_text(json.dumps({"credited_by_workspace": credited_map}), encoding="utf-8")
        tmp.replace(marker)


def _load_session_savings(session_id: str, transcript_path: str = "") -> dict[str, Any]:
    """Return session savings summary for the Claude session.

    Delegates to ``compute_savings_summary`` — the same function behind the
    statusline's ``lc savings --segment`` — so the statusline
    figure and this stop-hook summary are always derived from the same
    source (``sessions/<session_id>/savings.jsonl``, priced per-row
    at the model captured when each row was written).
    """
    zero = {
        "saved_usd": 0.0,
        "routing_usd": 0.0,
        "tokens_saved": 0,
        "calls_avoided": 0,
        "carry_usd": 0.0,
        "carry_tokens": 0,
        "estimated": False,
    }
    if not session_id:
        return zero
    try:
        from lemoncrow.core.capabilities.savings_summary import compute_savings_summary

        summary = compute_savings_summary(session_id, lemoncrow_root=_lemoncrow_root())
        return {
            "saved_usd": float(summary.saved_usd),
            "routing_usd": float(summary.routing_saved_usd),
            "tokens_saved": int(summary.ctx_saved),
            "calls_avoided": int(summary.smart_calls),
            "carry_usd": float(summary.carry_usd),
            "carry_tokens": int(summary.carry_tokens),
            "output_usd": float(summary.output_saved_usd),
            "output_tokens": int(summary.output_saved_tokens),
            "estimated": False,
        }
    except Exception:
        logger.exception("Failed to load session savings")
        return zero


def _format_stats(
    stats: dict[str, Any],
    savings: dict[str, Any] | None = None,
    real_cost: bool = False,
) -> str:
    # Shared token-count formatter (savings_summary.py) -- this file used to
    # carry its own hand-rolled duplicate (with a 1-decimal-M/extra-B-tier
    # scheme that had drifted from the canonical 2-decimal-M formatter every
    # other Python savings surface uses); import it instead of redefining it.
    try:
        from lemoncrow.core.capabilities.savings_summary import (
            _fmt_tok,
            _fmt_usd,
            estimate_time_saved_seconds,
            fmt_duration,
        )
    except ImportError:
        # This hook script is synced from the dev repo independently of the
        # installed `lc` package (uv tool / pip) -- an older installed
        # package predating these private helpers must not crash Stop on
        # every turn. Fall back to the same formatting inline.
        def _fmt_usd(v: float) -> str:
            return f"${float(v or 0.0):,.2f}"

        def _fmt_tok(n: int) -> str:
            n = int(n or 0)
            if n >= 1_000_000:
                return f"{n / 1_000_000:.2f}M"
            if n >= 1000:
                return f"{n / 1000:.1f}k"
            return str(n)

        def estimate_time_saved_seconds(*, calls_avoided: int, output_saved_tokens: int = 0) -> float:
            return max(0, int(calls_avoided or 0)) * 4.5 + max(0, int(output_saved_tokens or 0)) / 50.0

        def fmt_duration(seconds: float) -> str:
            s = max(0.0, float(seconds or 0.0))
            if s < 60:
                return f"{int(s)}s"
            if s < 3600:
                return f"{round(s / 60)}m"
            hours = s / 3600.0
            return f"{hours:.1f}h" if hours < 10 else f"{round(hours)}h"

    inp = int(stats.get("input_tokens", 0) or 0)
    out = int(stats.get("output_tokens", 0) or 0)
    cache_read = int(stats.get("cache_read_tokens", 0) or 0)
    cache_write = int(stats.get("cache_write_tokens", 0) or 0)
    total = inp + out + cache_read + cache_write
    calls = int(stats.get("tool_calls", 0) or 0)
    turns = int(stats.get("turns", 0) or 0)
    cost = float(stats.get("est_cost_usd", 0.0) or 0.0)

    # Top tools (up to 4)
    top = sorted(stats.get("tools_used", {}).items(), key=lambda x: -x[1])[:4]
    tools_str = " · ".join(f"{n}×{c}" for n, c in top) if top else "none"  # noqa: RUF001

    cost_prefix = "cost: " if real_cost else "est. cost: "
    # One-line tokens with all 4 Anthropic billing categories. No separate
    # cache line — cW (cache write, expensive at ~$6.25/M for Opus) and cR
    # (cache read, cheap at $0.50/M) get equal billing prominence so users
    # see the real cost structure at a glance.
    # "input processed" = new uncached input + tokens written to cache this
    # session. Anthropic's `input_tokens` field only counts the non-cached
    # delta per turn, which collapses to near-zero on cache-friendly sessions
    # and confuses readers. cW is also "new input the model processed"; only
    # cR is recycled content. So we surface (in+cW) as the meaningful
    # cumulative input figure and keep the raw breakdown for transparency.
    fresh_in = inp + cache_write
    calls_str = f"{calls} tool call{'s' if calls != 1 else ''}"
    turns_str = f"{turns} turn{'s' if turns != 1 else ''}" if turns > 0 else ""
    activity = " · ".join(p for p in (turns_str, calls_str) if p)
    # One dense line per metric. Cost is omitted when negligible (<$0.01) -- the
    # exact sub-cent figure is noise. tokens stays a single line with the 4
    # Anthropic billing categories (cW/cR weighted by their real $ prominence).
    tokens_line = (
        f"tokens: {_fmt_tok(fresh_in)} in ({_fmt_tok(inp)} new + {_fmt_tok(cache_write)} cW) · "
        f"{_fmt_tok(cache_read)} cR · {_fmt_tok(out)} out · {_fmt_tok(total)} total"
    )
    lines = [activity, tokens_line]
    if cost >= 0.01:
        lines.append(f"{cost_prefix}${cost:.4f}")

    # Always show savings — even at $0 — so the stop output shape is stable
    # across sessions. No display-time clamps; each saving was priced at the
    # model in use when it was emitted, so we trust the numbers as-is.
    savings = savings or {}
    saved_usd = float(savings.get("saved_usd", 0.0) or 0.0)
    tokens_saved = int(savings.get("tokens_saved", 0) or 0)
    calls_avoided = int(savings.get("calls_avoided", 0) or 0)
    routing_usd = float(savings.get("routing_usd", 0.0) or 0.0)
    carry_usd = float(savings.get("carry_usd", 0.0) or 0.0)
    carry_tokens = int(savings.get("carry_tokens", 0) or 0)
    output_usd = float(savings.get("output_usd", 0.0) or 0.0)
    output_tokens = int(savings.get("output_tokens", 0) or 0)
    savings_line = f"savings: {_fmt_usd(saved_usd)} · {_fmt_tok(tokens_saved)} tok · {calls_avoided} calls avoided"
    if output_usd > 0:
        out_tokens_str = f"/{_fmt_tok(output_tokens)} tok" if output_tokens > 0 else ""
        savings_line += f" · O {_fmt_usd(output_usd)}{out_tokens_str}"
    if carry_usd > 0:
        carry_tokens_str = f"/{_fmt_tok(carry_tokens)} tok" if carry_tokens > 0 else ""
        savings_line += f" · carry {_fmt_usd(carry_usd)}{carry_tokens_str}"
    if routing_usd > 0:
        savings_line += f" · routing {_fmt_usd(routing_usd)}"
    faster_s = estimate_time_saved_seconds(calls_avoided=calls_avoided, output_saved_tokens=output_tokens)
    if faster_s >= 60:
        savings_line += f" · ~{fmt_duration(faster_s)} faster"
    lines.append(savings_line)

    lines.append(f"top tools: {tools_str}")

    return "\n".join(lines)


def _format_review_findings(session_id: str) -> str:
    """Surface unconsumed NEEDS_FIX live-reviewer verdicts; mark them consumed.

    Advisory only — returns a short suffix appended to the session message.
    Fail-open: any problem yields an empty suffix.
    """
    if not session_id:
        return ""
    try:
        from lemoncrow.core.capabilities.live_reviewer.sink import (
            latest_unconsumed,
            mark_consumed,
        )
    except ImportError:
        return ""
    root = _lemoncrow_root()
    pending = latest_unconsumed(root, session_id)
    if not pending:
        return ""
    mark_consumed(root, session_id)
    needs_fix = [row for row in pending if row.get("verdict") == "NEEDS_FIX"]
    if not needs_fix:
        return ""
    lines = ["", "Code review (lc) — NEEDS_FIX:"]
    for row in needs_fix[:5]:
        paths = ", ".join(str(p) for p in (row.get("paths") or []))
        missing = str(row.get("missing") or "").strip().replace("\n", " ")
        lines.append(f"  • {paths}: {missing[:300]}" if missing else f"  • {paths}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        logger.exception("Failed to parse main payload")
        payload = {}

    session_id: str = payload.get("session_id", "") or ""
    transcript_path: str = payload.get("transcript_path", "") or ""
    try:
        bash_decision, bash_suffix = _bash_stop_guard(payload)
    except Exception:
        logger.exception("bash stop guard failed")
        bash_decision, bash_suffix = None, ""
    if bash_decision is not None:
        print(json.dumps(bash_decision))
        return 0
    # Deferred-format pass: when LEMONCROW_DEFER_EDIT_HOOKS moved format off the
    # per-edit path, format the session's edited files once now (fail-open).
    with contextlib.suppress(Exception):
        _format_deferred_edits(transcript_path)
    stats = _read_transcript_stats(transcript_path)
    session_aggregate = _load_session_aggregate(session_id)
    stats = _merge_session_aggregate(stats, session_aggregate)
    # Claude's Stop payload usually omits total_cost; fall back to the
    # transcript-derived estimate already computed in _read_transcript_stats.
    payload_cost: float = float(payload.get("total_cost_usd") or payload.get("total_cost") or 0.0)
    real_cost = False
    if stats is not None and payload_cost > 0:
        stats["est_cost_usd"] = payload_cost
        real_cost = True

    # ── Always write token/cost summary to RunLedger (fail-open) ─────────────
    if stats and stats.get("total_tokens", 0) > 0:
        with contextlib.suppress(Exception):
            _write_token_event(stats, session_id)

    # ── Fold output-style + input-style + external-compactor (rtk) savings into
    # the ledger FIRST, so this Stop's own summary already includes them.
    with contextlib.suppress(Exception):
        _write_output_style_row(session_id, stats or {}, transcript_path)
    with contextlib.suppress(Exception):
        _write_input_style_row(session_id, stats or {})
    with contextlib.suppress(Exception):
        _write_turn_cut_row(session_id, stats or {})
    with contextlib.suppress(Exception):
        _credit_rtk_gain(session_id, stats or {})

    # ── Load per-session savings breakdown (before writing session_end so carry is persisted)
    savings: dict[str, Any] | None = None
    with contextlib.suppress(Exception):
        savings = _load_session_savings(session_id, transcript_path)

    # Public rollup: no longer pushed from here. The servicectl daemon's daily
    # tick computes it directly from the same savings.jsonl ledger written
    # below (see lemoncrow.core.service.telemetry.public_rollup), so there is
    # nothing to send on every Stop -- the hook now touches neither the
    # network nor a side queue file for this.

    # ── Write session cost + carry to savings.jsonl for historical 7d/30d spend tracking
    if stats and stats.get("est_cost_usd", 0) > 0:
        with contextlib.suppress(Exception):
            _write_session_cost(
                session_id,
                float(stats["est_cost_usd"]),
                int(stats.get("total_tokens", 0)),
                carry_usd=float((savings or {}).get("carry_usd", 0.0) or 0.0),
                carry_tokens=int((savings or {}).get("carry_tokens", 0) or 0),
            )

    # ── Refresh the statusline sidecar so Stop-time rows (output_style ↓O,
    # session cost/carry) show at the prompt now, not one tool call later.
    with contextlib.suppress(Exception):
        _refresh_statusline_frames(session_id)

    # ── Enrich run file with session title + full prompt history ─────────────────────
    with contextlib.suppress(Exception):
        session_title = _extract_session_title(transcript_path)
        user_prompts = _extract_user_prompts(transcript_path)
        if session_title or user_prompts:
            _write_session_enrichment(session_id, session_title, user_prompts, transcript_path)

    # ── Surface unconsumed live-reviewer findings (advisory) ─────────────────
    review_suffix = ""
    with contextlib.suppress(Exception):
        review_suffix = _format_review_findings(session_id)
    if bash_suffix:
        review_suffix = f"{review_suffix}\n\n{bash_suffix}" if review_suffix else f"\n\n{bash_suffix}"

    # Transcript JSONL stays as the source of truth even after stop —
    # cost, tokens, and savings are all derivable from it. No snapshot needed.

    # ── Always show stats (discussion and task sessions alike) ───────────────
    # If no code-editing tools were used, show plain session stats.
    if not _is_task_session(stats, session_aggregate):
        if stats and stats["total_tokens"] > 0:
            summary = _format_stats(stats, savings, real_cost=real_cost)
            print(json.dumps({"systemMessage": f"Session stats:\n{summary}{review_suffix}"}))
        elif bash_suffix:
            print(json.dumps({"systemMessage": bash_suffix}))
        return 0

    # ── Code work happened: show the session-complete summary ────────────────
    # (Stop hooks can only emit a systemMessage — hookSpecificOutput is not
    # valid here, unlike PreToolUse/PostToolUse/UserPromptSubmit/PostToolBatch.)
    if stats and stats["total_tokens"] > 0:
        summary = _format_stats(stats, savings, real_cost=real_cost)
        print(json.dumps({"systemMessage": f"lc session complete.\n{summary}{review_suffix}"}))
    elif bash_suffix:
        print(json.dumps({"systemMessage": bash_suffix}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
