"""Offline session analyzer — mine Claude Code & Codex session files for search patterns.

Automatically detects the current repo from your working directory and scans
both Claude Code and Codex CLI sessions:

* **Claude Code** — ``~/.claude/projects/<repo-dir-name>/*.jsonl``
* **Codex CLI** — ``~/.codex/sessions/**/*.jsonl``

Extracts all LemonCrow MCP search tool calls (grep, code_search, ToolSearch), groups
them into "search episodes" between user messages, and produces:

1. **Savings report** — how many individual grep calls each code_search replaced,
   and how many turns were saved per session.
2. **Benchmark pairs** — ``(query, gold_file)`` pairs mined from grep results,
   compatible with ``eval_external_provider_mrr.py`` / ``eval_cg_mrr.py`` MRR eval.

Usage::

    # Run from any repo — auto-detects everything
    python benchmarks/codebench/offline_session_analyzer.py --run-eval

    # Override filter for a different project
    python benchmarks/codebench/offline_session_analyzer.py \\
        --repo-filter my-other-project --run-eval

    # Specify a specific Claude Code session directory
    python benchmarks/codebench/offline_session_analyzer.py \\
        --session-dir ~/.claude/projects/-my-project \\
        --out /tmp/session_pairs.json

Environment variables:
    SESSION_ROOT          Override Claude Code session root
    SESSION_REPO_FILTER   Override repo filter (default: auto-detected from cwd)
    SESSION_PAIRS_OUT     Output path for mined pairs JSON
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

SEARCH_TOOLS = {
    "mcp__lc__grep",
    "mcp__lc__explore",
    "mcp__lc__code_search",
    "ToolSearch",
    "Grep",
    "mcp__plugin_lemoncrow_lc__grep",
}

# Regex for detecting grep/rg/ag etc. run via bash exec_command in Codex
_GREP_CMD_RE = re.compile(
    r"^(?:grep|rg|ripgrep|ag|ack|git\s+grep)\b",
    re.IGNORECASE,
)
# Extract the pattern from a grep command (heuristic)
_GREP_PATTERN_RE = re.compile(
    r"""
    (?:grep|rg|ripgrep|ag|ack)
    (?:\s+-\w+)*               # flags like -r, -n, -i, --include
    \s+
    (?:
        ["']([^"']+?)["']      # quoted pattern
        |
        (\S+?)                  # unquoted pattern
    )
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _detect_repo_info() -> tuple[str, str]:
    """Detect current repo directory name and git-based owner__repo prefix.

    Returns (dirname, owner__repo_prefix).
    """
    cwd = Path.cwd().resolve()
    dirname = cwd.name

    # Try git remote to derive owner__repo prefix
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(cwd),
        )
        if result.returncode == 0:
            url = result.stdout.strip()
            # git@github.com:owner/repo.git  or  https://github.com/owner/repo.git
            m = re.search(r"(?:github\.com[/:])([\w.-]+)/([\w.-]+?)(?:\.git)?$", url)
            if m:
                owner, repo = m.group(1), m.group(2)
                return dirname, f"{owner}__{repo}"
    except Exception:
        pass

    # Fallback: just use dirname as both
    return dirname, dirname


# ---------------------------------------------------------------------------
# Codex session scanning
# ---------------------------------------------------------------------------

_CODEX_SESSION_ROOT = Path("~/.codex/sessions").expanduser()


def _find_codex_session_files() -> list[Path]:
    """Walk ~/.codex/sessions/ and return all JSONL session files."""
    if not _CODEX_SESSION_ROOT.is_dir():
        return []
    return sorted(_CODEX_SESSION_ROOT.rglob("*.jsonl"))


def _extract_grep_pattern(command: str) -> str | None:
    """Extract the search pattern from a bash grep/rg command."""
    m = _GREP_PATTERN_RE.search(command)
    if m:
        return (m.group(1) or m.group(2)).strip()
    # Fallback: try to extract pattern from "grep -r PATTERN" unquoted
    # after stripping flags
    parts = command.split()
    for i, part in enumerate(parts):
        if part in ("grep", "rg", "ripgrep", "ag", "ack"):
            # Skip flags, take the first non-flag argument after the tool
            for j in range(i + 1, len(parts)):
                p = parts[j]
                if p.startswith("-"):
                    continue
                if p in ("rg", "ripgrep"):
                    continue
                return p.strip("\"'")
    return None


def _check_codex_session_cwd(path: Path, repo_path: Path) -> bool:
    """Peek at a Codex session file to see if its cwd matches repo_path.

    Returns True if the session belongs to this repo (or cwd is unset).
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for _ in range(30):  # check first 30 lines
                line = fh.readline()
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    ev = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                # Format A: session_meta has cwd in payload
                if ev.get("type") == "session_meta":
                    cwd = str(ev.get("payload", {}).get("cwd", "") or "")
                    if cwd:
                        return Path(cwd).resolve() == repo_path
                # Format A: also check event_msg / user_message for cwd
                payload = ev.get("payload") or {}
                cwd = str(payload.get("cwd", "") or "")
                if cwd:
                    return Path(cwd).resolve() == repo_path
                # Format B: first line may have cwd at top level
                cwd = str(ev.get("cwd", "") or "")
                if cwd:
                    return Path(cwd).resolve() == repo_path
                # Format B: instructions line may have cwd
                if "instructions" in ev:
                    cwd = str(ev.get("cwd", "") or "")
                    if cwd:
                        return Path(cwd).resolve() == repo_path
    except Exception:
        pass
    # No cwd found — include the session (can't rule it out)
    return True


def _scan_codex_session_file(path: Path, repo_path: Path | None = None) -> list[dict]:
    """Parse a Codex JSONL session file, extract search tool calls.

    Detects both LemonCrow MCP search tools AND bash grep/rg/ag commands
    run via ``exec_command``. Skips sessions whose ``cwd`` does not match
    the current repo.

    Returns the same event format as scan_project_dir():
    ``{"type":"call", "tool":..., "id":..., "query":..., "result_files": [...]}``.
    """
    # Filter by repo cwd first
    if repo_path is not None and not _check_codex_session_cwd(path, repo_path):
        return []

    size = path.stat().st_size
    if size < 1000 or size > 50_000_000:
        return []

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []

    # Detect format
    is_event_msg = False
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        t = ev.get("type", "")
        if t in ("message", "reasoning"):
            break  # flat format B
        elif t in ("event_msg", "response_item") or t == "session_meta":
            is_event_msg = True
            break

    pending_calls: dict[str, dict] = {}  # call_id -> call event
    pending_execs: list[dict] = []  # ordered exec_command calls (Format A)
    session_events: list[dict] = []

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            continue

        if is_event_msg:
            _scan_codex_format_a(ev, pending_calls, pending_execs, session_events)
        else:
            _scan_codex_format_b(ev, pending_calls, session_events)

    return session_events


def _resolve_codex_search_call(
    name: str,
    namespace: str,
    args: dict,
    call_id: str,
) -> dict | None:
    """Check if a Codex function_call is a search tool and extract query.

    Handles both Codex namespace+name format (``namespace="mcp__lemoncrow",
    name="grep"``) and Claude Code dotted format (``name="mcp__lc__grep"``).

    Returns a call dict with ``tool``, ``id``, ``query``, and ``file_pattern``,
    or ``None`` if this is not a search call.
    """
    # Build the dotted name that SEARCH_TOOLS uses
    dotted = f"{namespace}__{name}" if namespace else name

    # Check all name forms against SEARCH_TOOLS
    effective = name if name in SEARCH_TOOLS else (dotted if dotted in SEARCH_TOOLS else None)
    # Codex also has native grep/explore/code_search tools under mcp__lemoncrow namespace
    # (not dotted — separate namespace and name fields)
    if effective is None:
        if namespace == "mcp__lemoncrow" and name in ("grep", "explore", "code_search", "search"):
            effective = f"{namespace}/{name}"
        elif name in ("grep", "explore", "code_search", "search", "Grep"):
            effective = name

    if effective is None or not call_id:
        return None

    # Extract query — content regex, or fall back to file glob patterns
    query = str(args.get("query", args.get("content_regex", args.get("pattern", args.get("regex", ""))))).strip()
    if not query:
        patterns = args.get("file_glob_patterns", [])
        if isinstance(patterns, list) and patterns:
            query = ", ".join(str(p) for p in patterns)
        elif isinstance(patterns, str) and patterns:
            query = patterns
        elif args.get("path"):
            query = str(args.get("path", ""))
    if not query:
        return None

    return {
        "tool": effective,
        "id": f"codex_{call_id}",
        "query": query[:500],
        "file_pattern": args.get("file_glob_patterns", ""),
    }


def _scan_codex_format_a(
    ev: dict,
    pending_calls: dict[str, dict],
    pending_execs: list[dict],
    session_events: list[dict],
) -> None:
    """Process a single Codex Format A line (event_msg wrapper).

    Handles:
    - LemonCrow MCP search tools (mcp__lc__grep etc.) via function_call + function_call_output
    - Bash grep/rg/ag via exec_command + exec_command_end
    """
    ev_type = ev.get("type", "")
    payload = ev.get("payload") or {}
    pt = payload.get("type", "")

    if ev_type == "response_item" and pt == "function_call":
        name = str(payload.get("name", ""))
        namespace = str(payload.get("namespace", ""))
        call_id = str(payload.get("call_id", "") or payload.get("id", ""))
        args_raw = payload.get("arguments", "{}")
        args = _coerce_args(args_raw)

        call = _resolve_codex_search_call(name, namespace, args, call_id)
        if call is not None:
            pending_calls[call_id] = call
            return

            # Bash grep via exec_command
            cmd = str(args.get("command", args.get("cmd", args_raw)))
            if _GREP_CMD_RE.match(cmd.strip()):
                pattern = _extract_grep_pattern(cmd)
                if pattern:
                    tag = call_id or f"exec_{len(pending_execs)}"
                    pending_execs.append(
                        {
                            "call_id": tag,
                            "call": {
                                "tool": "bash_grep",
                                "id": f"codex_{tag}",
                                "query": pattern[:500],
                                "file_pattern": "",
                            },
                        }
                    )

    elif ev_type == "event_msg" and pt == "function_call_output":
        call_id = str(payload.get("call_id", ""))
        call = pending_calls.pop(call_id, None)
        if call is None:
            # Could be an exec_command result in function_call_output too
            # (some Codex versions route exec results through here)
            for i, ec in enumerate(pending_execs):
                if ec["call_id"] == call_id:
                    call = ec["call"]
                    pending_execs.pop(i)
                    break
        if call is None:
            return
        output = payload.get("output", "")
        output_text = str(output) if isinstance(output, str) else json.dumps(output)
        files = parse_tool_result_files(output_text)
        call["result_files"] = files
        call["result_count"] = len(files)
        session_events.append({"type": "call", **call})

    elif ev_type == "response_item" and pt == "function_call_output":
        # Codex routes MCP tool results through response_item too (not just event_msg)
        call_id = str(payload.get("call_id", ""))
        call = pending_calls.pop(call_id, None)
        if call is not None:
            output = payload.get("output", "")
            output_text = str(output) if isinstance(output, str) else json.dumps(output)
            files = parse_tool_result_files(output_text)
            call["result_files"] = files
            call["result_count"] = len(files)
            session_events.append({"type": "call", **call})

    elif ev_type == "event_msg" and pt == "exec_command_end":
        # Match to the oldest pending exec_command (sequential order)
        if not pending_execs:
            return
        ec = pending_execs.pop(0)
        call = ec["call"]
        output = payload.get("output", "")
        output_text = str(output) if isinstance(output, str) else json.dumps(output)
        files = parse_tool_result_files(output_text)
        call["result_files"] = files
        call["result_count"] = len(files)
        session_events.append({"type": "call", **call})


def _scan_codex_format_b(
    ev: dict,
    pending_calls: dict[str, dict],
    session_events: list[dict],
) -> None:
    """Process a single Codex Format B line (flat).

    Handles both LemonCrow MCP tools and bash grep exec_command.
    """
    ev_type = ev.get("type", "")

    if ev_type == "function_call":
        name = str(ev.get("name", ""))
        namespace = str(ev.get("namespace", ""))
        call_id = str(ev.get("call_id", "") or ev.get("id", ""))
        args_raw = ev.get("arguments", "{}")
        args = _coerce_args(args_raw)

        call = _resolve_codex_search_call(name, namespace, args, call_id)
        if call is not None:
            pending_calls[call_id] = call
            return

        # Bash grep via exec_command
        if name == "exec_command":
            cmd = str(args.get("command", args.get("cmd", args_raw)))
            if _GREP_CMD_RE.match(cmd.strip()):
                pattern = _extract_grep_pattern(cmd)
                if pattern and call_id:
                    pending_calls[call_id] = {
                        "tool": "bash_grep",
                        "id": f"codex_{call_id}",
                        "query": pattern[:500],
                        "file_pattern": "",
                    }

    elif ev_type == "function_call_output":
        call_id = str(ev.get("call_id", ""))
        call = pending_calls.pop(call_id, None)
        if call is None:
            return
        output = ev.get("output", "")
        output_text = str(output) if isinstance(output, str) else json.dumps(output)
        files = parse_tool_result_files(output_text)
        call["result_files"] = files
        call["result_count"] = len(files)
        session_events.append({"type": "call", **call})


def _coerce_args(raw: str | dict) -> dict:
    """Parse function call arguments that may be a JSON string or already a dict."""
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


# ---------------------------------------------------------------------------
# Original Claude Code session scanning
# ---------------------------------------------------------------------------


def parse_tool_result_files(content) -> list[str]:
    """Extract file paths from a grep tool_result content."""
    files: list[str] = []
    seen: set[str] = set()

    if isinstance(content, str):
        chunks = [content]
    elif isinstance(content, list):
        chunks = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    chunks.append(item.get("text", ""))
                elif item.get("type") == "resource":
                    uri = item.get("resource", {}).get("uri", "")
                    if uri and uri not in seen:
                        seen.add(uri)
                        files.append(uri)
    else:
        return files

    # Regex: matches paths with at least one / and an extension,
    # possibly prefixed with `## `, `### `, or `# grep` headers
    _FILE_RE = re.compile(r"^(?:#+\s+)?([\w./-]+/(?:[\w./-]+\.\w+))")

    # Spill-file pattern: results were too large and written to /tmp/lemoncrow-spill/search-*.json
    _SPILL_RE = re.compile(r"/tmp/lemoncrow-spill/search-\d+\.json")

    for chunk in chunks:
        # Check for spilled results — recover from spill file
        if isinstance(chunk, str) and ("spilled" in chunk or "/tmp/lemoncrow-spill/" in chunk):
            spill_m = _SPILL_RE.search(chunk)
            if spill_m:
                spill_path = spill_m.group(0)
                try:
                    with open(spill_path) as _sfh:
                        spill_data = json.load(_sfh)
                    # Spill files contain the same format as grep output
                    # Try standard grep fields: ranked_file_map, files, content, text
                    spilled_files = parse_tool_result_files(json.dumps(spill_data))
                    if spilled_files:
                        for fp in spilled_files:
                            if fp not in seen:
                                seen.add(fp)
                                files.append(fp)
                        continue  # skip further plain-text parsing of spill message
                except (FileNotFoundError, json.JSONDecodeError, PermissionError):
                    pass  # spill file may have been cleaned up
                # If the spill file is gone, fall through to normal text parsing

        # Codex MCP output format: "Wall time: Xs\nOutput:\n[{content_blocks}]"
        # The inner JSON text field may contain unescaped newlines, so we
        # extract file paths directly from the raw text rather than re-parsing.
        if isinstance(chunk, str):
            _output_marker = "\nOutput:\n"
            idx = chunk.find(_output_marker)
            if idx != -1:
                rest = chunk[idx + len(_output_marker) :].strip()
                # Remove leading [ and trailing ] if present
                if rest.startswith("[") and rest.endswith("]"):
                    rest = rest[1:-1]
                # Try to parse as JSON object (for well-formed cases)
                try:
                    obj = json.loads(rest)
                    if isinstance(obj, dict) and obj.get("type") == "text":
                        inner = obj.get("text", "")
                        if inner:
                            files.extend(parse_tool_result_files(inner))
                            continue
                except (json.JSONDecodeError, TypeError):
                    pass
                # Fallback: extract "text" field content via regex
                m = re.search(r'"text"\s*:\s*"((?:[^"\\]|\\.)*)"', rest, re.DOTALL)
                if m:
                    inner = m.group(1)
                    # Unescape JSON-style escapes in the extracted text
                    inner = inner.replace("\\n", "\n").replace("\\t", "\t").replace('\\"', '"').replace("\\\\", "\\")
                    files.extend(parse_tool_result_files(inner))
                    continue
                # Last resort: parse file paths directly from the rest text
                files.extend(parse_tool_result_files(rest))
                continue

        # JSON format: {"ranked_file_map": [...]} or {"cached":..., "path":"..."}
        first = chunk.strip()
        if first.startswith("{"):
            try:
                obj = json.loads(chunk)
                # LemonCrow grep ranked results
                ranked = obj.get("ranked_file_map", obj.get("files", obj.get("content", [])))
                if isinstance(ranked, list):
                    for item in ranked:
                        if isinstance(item, dict):
                            fp = item.get("file_path", item.get("path", item.get("file", "")))
                            if fp and fp not in seen:
                                seen.add(fp)
                                files.append(fp)
                        elif isinstance(item, str):
                            if item not in seen:
                                seen.add(item)
                                files.append(item)
                    continue
                # Single resource result: {"cached": false, "path":"...", "summary":"..."}
                single_path = obj.get("path", "")
                if single_path and "/" in single_path:
                    if single_path not in seen:
                        seen.add(single_path)
                        files.append(single_path)
                    continue
            except json.JSONDecodeError:
                pass

        # Plain text format: grep output has file paths on lines, followed
        # by indented match details.
        # Format 1: file_path.py  (possibly with trailing \tcount)
        #   src/lemoncrow/core/capabilities/default_definitions.py
        #   - lines 12-13
        #
        # Format 2: ## file_path.py  (from file_paths_with_content mode)
        #   ## src/lemoncrow/gateway/adapters/mcp_server.py
        #   def _op_usages(
        #
        # Format 3: # grep — output_mode=... header line (skip)
        # Format 4: lemoncrow/src/lemoncrow/...\tcount  (tab-separated match count)

        for line in chunk.split("\n"):
            line_stripped = line.strip()
            if not line_stripped:
                continue
            # Skip match-detail lines and headers
            if line_stripped.startswith("- ") or line_stripped.startswith("# "):
                continue
            # Skip lines that are code content (indented or start with `def `, `class `, etc.)
            if line.startswith(" ") or line.startswith("\t"):
                continue

            # Try to extract file path from diff headers like "## file_path"
            m = _FILE_RE.match(line_stripped)
            if m:
                fp = m.group(1)
                # Remove leading `lemoncrow/` prefix if present (from absolute-path grep results
                # in the lemoncrow__lemoncrow workspace)
                if fp.startswith("lemoncrow/"):
                    fp = fp[len("lemoncrow/") :]
                if fp not in seen:
                    seen.add(fp)
                    files.append(fp)
                continue

            # Fallback: any line with a / and a file extension
            # (catches tab-separated results like "lemoncrow/src/...\t314")
            if "/" in line_stripped:
                # Take the first space/tab-separated token
                first_token = line_stripped.split()[0]
                if "." in first_token and "/" in first_token:
                    fp = first_token.rstrip(":")
                    if fp.startswith("lemoncrow/"):
                        fp = fp[len("lemoncrow/") :]
                    if fp not in seen:
                        seen.add(fp)
                        files.append(fp)

    return files


def _attach_tool_result(block: dict, session_events: list[dict]) -> None:
    """Match a tool_result block to its tool_use call in session_events."""
    tool_id = block.get("tool_use_id", "")
    if not tool_id:
        return
    content_data = block.get("content", "")
    files = parse_tool_result_files(content_data)
    for evt in reversed(session_events):
        if evt.get("id") == tool_id and evt["type"] == "call":
            evt["result_files"] = files
            evt["result_count"] = len(files)
            break


def scan_project_dir(project_dir: str) -> list[dict]:
    """Scan all session JSONL files under a project directory."""
    all_events: list[dict] = []
    dirpath = Path(project_dir)
    if not dirpath.is_dir():
        return all_events

    for fpath in sorted(dirpath.glob("*.jsonl")):
        sz = fpath.stat().st_size
        if sz < 1000 or sz > 50_000_000:  # skip tiny and huge files
            continue
        try:
            with open(fpath) as fh:
                lines = fh.readlines()
        except Exception:
            continue

        session_events = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = obj.get("type", "")

            # User messages — may contain real user text or tool_result blocks.
            # message is always a dict. When content is a string = user text;
            # when content is a list = tool_result wrapper (tool_use_id + content per block).
            if msg_type == "user":
                msg = obj.get("message", None)
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    # Real user text
                    preview = content[:150].replace("\n", " ")
                    session_events.append({"type": "user", "text": preview})
                elif isinstance(content, list):
                    # tool_result blocks
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            _attach_tool_result(block, session_events)

            # Assistant messages with tool_use (tool calls)
            elif msg_type == "assistant":
                content = obj.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type", "")
                    if btype == "tool_use":
                        name = block.get("name", "")
                        if name not in SEARCH_TOOLS:
                            continue
                        inp = block.get("input", {})
                        if not isinstance(inp, dict):
                            continue
                        query = inp.get("query", inp.get("content_regex", inp.get("pattern", inp.get("regex", ""))))
                        # Handle __unparsedToolInput — raw JSON string that failed to parse
                        if not query and isinstance(inp.get("__unparsedToolInput"), dict):
                            raw = inp["__unparsedToolInput"].get("raw", "")
                            try:
                                reparsed = json.loads(raw)
                                if isinstance(reparsed, dict):
                                    query = reparsed.get(
                                        "query",
                                        reparsed.get(
                                            "content_regex", reparsed.get("pattern", reparsed.get("regex", ""))
                                        ),
                                    )
                            except (json.JSONDecodeError, TypeError):
                                pass
                        tool_id = block.get("id", "")
                        session_events.append(
                            {
                                "type": "call",
                                "tool": name,
                                "id": tool_id,
                                "query": str(query)[:500],
                                "file_pattern": inp.get("file_glob_patterns", ""),
                            }
                        )

        if any(e["type"] == "call" for e in session_events):
            all_events.extend(session_events)

    return all_events


def build_search_episodes(events: list[dict]) -> list[list[dict]]:
    """Group search calls into episodes between user messages."""
    episodes: list[list[dict]] = []
    current: list[dict] = []

    for evt in events:
        if evt["type"] == "user":
            if current:
                episodes.append(current)
                current = []
        elif evt["type"] == "call":
            current.append(evt)

    if current:
        episodes.append(current)
    return episodes


def generate_pairs(
    events: list[dict],
    repo_prefix: str = "",
) -> tuple[list[tuple[str, str, str]], dict[str, list[str]], list[dict]]:
    """Generate (query, tid, prefix) pairs from grep calls that have result files.

    Args:
        events: Extracted tool-call events with result_files.
        repo_prefix: Owner__repo prefix (auto-detected from git if empty).

    Returns (pairs, true_map, savings_report) where:
    - pairs: [(query, tid, prefix), ...] — each query maps to its result files
    - true_map: {tid: [file_paths...]} — the actual files the grep returned
    - savings: list of per-episode search statistics
    """
    if not repo_prefix:
        _dirname, repo_prefix = _detect_repo_info()
    episodes = build_search_episodes(events)

    pairs: list[tuple[str, str, str]] = []
    true_map: dict[str, list[str]] = {}
    savings: list[dict] = []
    pair_id = 0

    for episode in episodes:
        grep_calls = [e for e in episode if "grep" in e["tool"].lower()]
        code_search_calls = [e for e in episode if "explore" in e["tool"].lower() or "code_search" in e["tool"].lower()]
        toolsearch_calls = [e for e in episode if e["tool"] == "ToolSearch"]

        _ws = Path.cwd().resolve()
        _code_like = re.compile(r"[a-zA-Z_]")
        for gc in grep_calls:
            query = gc.get("query", "")
            files = gc.get("result_files", [])
            if not query or not files:
                continue
            # Quality filter: skip garbage queries that produce noisy MRR.
            # - Too short (< 4 chars) → too ambiguous to benchmark
            # - No alphabetic content → numbers/punctuation/symbols, not code
            # - File-glob patterns ("*.py") → not content searches
            if len(query) < 4 or not _code_like.search(query):
                continue
            if query.startswith("*.") or query.startswith("**"):
                continue
            # Gold validation: only keep files that actually exist in this
            # workspace. Stale absolute paths from old or remote sessions
            # can never be matched by code_search, so they drag MRR to zero.
            valid_files = [f for f in files[:10] if (_ws / f).exists() or (f.startswith("/") and Path(f).exists())]
            if not valid_files:
                continue
            # Use the grep's own result files as the content gold.
            tid = f"session_{pair_id}"
            pair_id += 1
            true_map[tid] = valid_files
            pairs.append((query, tid, repo_prefix))

        # Generate savings metric for this episode
        if grep_calls:
            unique_queries = set(gc.get("query", "")[:60] for gc in grep_calls)
            savings.append(
                {
                    "episode_greps": len(grep_calls),
                    "episode_code_searches": len(code_search_calls),
                    "episode_toolsearches": len(toolsearch_calls),
                    "unique_grep_patterns": len(unique_queries),
                    "grep_savings": max(0, len(grep_calls) - len(code_search_calls) * 2),
                    # ^^ each code_search replaces ~3-5 greps, so saving is greps - 2*code_searches
                }
            )

    return pairs, true_map, savings


def generate_savings_report(savings: list[dict]) -> dict:
    """Generate a comprehensive savings report."""
    if not savings:
        # Same shape as the non-empty path below -- main() unconditionally reads
        # every one of these keys to print the summary, regardless of which
        # branch ran. A message-only dict here crashes on the first read
        # (e.g. a session with only code_search/ToolSearch calls and zero
        # plain greps: `savings` stays empty since grep_calls is falsy for
        # every episode, even though real search activity happened).
        return {
            "message": "No search tool calls found in session data.",
            "total_episodes": 0,
            "total_grep_calls": 0,
            "total_code_search_calls": 0,
            "total_toolsearch_calls": 0,
            "total_search_calls": 0,
            "episodes_with_code_search": 0,
            "episodes_without_code_search": 0,
            "avg_greps_per_episode": 0.0,
            "estimated_grep_turns_saved": 0,
            "estimated_cost_saved_usd": 0.0,
            "per_episode": [],
        }

    total_greps = sum(s["episode_greps"] for s in savings)
    total_code_searches = sum(s["episode_code_searches"] for s in savings)
    total_toolsearches = sum(s["episode_toolsearches"] for s in savings)
    total_episodes = len(savings)

    # Each ToolSearch call is ~1 turn (few hundred tokens)
    # Each grep call is ~1 turn (grep results can be many tokens)
    # Each code_search call replaces ~3-5 grep calls

    # Estimated savings from replacing greps with code_search
    estimated_grep_turns_saved = sum(
        max(0, s["episode_greps"] - s["episode_code_searches"] * 3) for s in savings if s["episode_code_searches"] > 0
    )
    # Each saved grep turn avoids: 1 tool_call + result processing + thinking ≈ 2K tokens
    # Claude Sonnet 4.6: ~$3/M input, ~$15/M output
    avg_saved_tokens_per_grep = 2000
    cost_per_million_input = 3.0
    cost_per_million_output = 15.0
    avg_cost_per_saved_turn = (
        avg_saved_tokens_per_grep / 1_000_000 * cost_per_million_input
        + avg_saved_tokens_per_grep / 1_000_000 * cost_per_million_output
    )
    estimated_cost_saved_usd = estimated_grep_turns_saved * avg_cost_per_saved_turn

    return {
        "total_episodes": total_episodes,
        "total_grep_calls": total_greps,
        "total_code_search_calls": total_code_searches,
        "total_toolsearch_calls": total_toolsearches,
        "total_search_calls": total_greps + total_code_searches + total_toolsearches,
        "episodes_with_code_search": sum(1 for s in savings if s["episode_code_searches"] > 0),
        "episodes_without_code_search": sum(1 for s in savings if s["episode_code_searches"] == 0),
        "avg_greps_per_episode": round(total_greps / max(total_episodes, 1), 1),
        "estimated_grep_turns_saved": estimated_grep_turns_saved,
        "estimated_cost_saved_usd": round(estimated_cost_saved_usd, 4),
        "per_episode": savings[:20],  # top 20 for detail
    }


def main():
    import argparse

    # Auto-detect repo info from cwd
    repo_dirname, git_prefix = _detect_repo_info()
    env_filter = os.environ.get("SESSION_REPO_FILTER", "")
    auto_filter = env_filter if env_filter else repo_dirname

    parser = argparse.ArgumentParser(description="Mine Claude Code & Codex session files for search patterns")
    parser.add_argument(
        "--session-dir",
        "-d",
        default=os.environ.get("SESSION_ROOT", ""),
        help="Claude Code session directory (default: ~/.claude/projects/<auto-repo>/)",
    )
    parser.add_argument(
        "--repo-filter",
        "-f",
        default=auto_filter,
        help=(f"Substring filter on project directory name (default: auto-detected '{auto_filter}')"),
    )
    parser.add_argument(
        "--out",
        "-o",
        default=os.environ.get("SESSION_PAIRS_OUT", "/tmp/session_pairs.json"),
        help="Output path for mined pairs JSON",
    )
    parser.add_argument(
        "--run-eval",
        action="store_true",
        help="Run the retrieval benchmark after generating pairs",
    )
    parser.add_argument(
        "--channel",
        choices=["lexical", "zoekt", "cg", "lexical+zoekt"],
        default="lexical",
        help="Which retrieval eval to run (default: lexical/code_search without Zoekt)",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run the eval on all mined pairs (no sample cap)",
    )
    parser.add_argument(
        "--synthetic",
        "-s",
        action="store_true",
        help="Augment mined pairs with synthetic queries mined from the repo source",
    )
    parser.add_argument(
        "--synthetic-per-file",
        type=int,
        default=4,
        help="Max synthetic queries per file (default: 4, only with --synthetic)",
    )
    args = parser.parse_args()

    repo_prefix = git_prefix  # owner__repo for the output JSON

    # -----------------------------------------------------------------------
    # 1. Scan Claude Code sessions
    # -----------------------------------------------------------------------
    claude_root = Path(args.session_dir) if args.session_dir else Path.home() / ".claude" / "projects"
    claude_events: list[dict] = []
    scanned_claude_dirs: list[str] = []

    if claude_root.is_dir():
        project_dirs = sorted(
            d for d in claude_root.iterdir() if d.is_dir() and (not args.repo_filter or args.repo_filter in d.name)
        )
        for proj_dir in project_dirs:
            events = scan_project_dir(str(proj_dir))
            if events:
                claude_events.extend(events)
                scanned_claude_dirs.append(proj_dir.name)
                print(
                    f"[claude] {proj_dir.name}: {sum(1 for e in events if e['type'] == 'call')} search calls",
                    file=sys.stderr,
                )

        print(
            f"[claude] Scanned {len(project_dirs)} project dir(s) matching '{args.repo_filter}' under {claude_root}",
            file=sys.stderr,
        )
    else:
        print(f"[claude] Session dir not found: {claude_root} (skipping)", file=sys.stderr)

    # -----------------------------------------------------------------------
    # 2. Scan Codex sessions (filtered by repo cwd)
    # -----------------------------------------------------------------------
    codex_events: list[dict] = []
    scanned_codex_files = 0
    scanned_codex_sessions = 0
    repo_path = Path.cwd().resolve()

    codex_files = _find_codex_session_files()
    scanned_codex_files = len(codex_files)
    for cpath in codex_files:
        events = _scan_codex_session_file(cpath, repo_path=repo_path)
        if events is not None:
            # events is empty list = scanned but no calls found
            # None would mean skipped by cwd filter — but we return [] for both
            pass
        if events:
            codex_events.extend(events)
            scanned_codex_sessions += 1

    if codex_files:
        print(
            f"[codex] Scanned {scanned_codex_files} session file(s) "
            f"under {_CODEX_SESSION_ROOT}, "
            f"{scanned_codex_sessions} with search calls "
            f"(filtered to repo: {repo_path.name})",
            file=sys.stderr,
        )
    else:
        print(f"[codex] No sessions found under {_CODEX_SESSION_ROOT} (skipping)", file=sys.stderr)

    # -----------------------------------------------------------------------
    # 3. Merge and generate pairs
    # -----------------------------------------------------------------------
    all_events = claude_events + codex_events

    if not all_events:
        print("[session] No search tool calls found in any session.", file=sys.stderr)
        print("[session] Tried:", file=sys.stderr)
        print(f"  Claude: {claude_root}", file=sys.stderr)
        print(f"  Codex:  {_CODEX_SESSION_ROOT}", file=sys.stderr)
        sys.exit(1)

    pairs, true_map, savings = generate_pairs(all_events, repo_prefix=repo_prefix)
    report = generate_savings_report(savings)

    print(f"\n{'=' * 60}", file=sys.stderr)
    print("OFFLINE SESSION ANALYSIS", file=sys.stderr)
    print(f"  Repo prefix: {repo_prefix}", file=sys.stderr)
    print(f"  Claude project dirs with calls: {len(scanned_claude_dirs)}", file=sys.stderr)
    print(f"  Codex sessions with calls:      {scanned_codex_sessions}", file=sys.stderr)
    print(f"  Total search tool calls: {report['total_search_calls']}", file=sys.stderr)
    print(f"    - grep calls:  {report['total_grep_calls']}", file=sys.stderr)
    print(f"    - code_search calls: {report['total_code_search_calls']}", file=sys.stderr)
    print(f"    - ToolSearch calls: {report['total_toolsearch_calls']}", file=sys.stderr)
    print(f"  Search episodes: {report['total_episodes']}", file=sys.stderr)
    print(f"  Episodes WITH code_search: {report['episodes_with_code_search']}", file=sys.stderr)
    print(f"  Episodes WITHOUT code_search (grep-only): {report['episodes_without_code_search']}", file=sys.stderr)
    print(f"  Avg greps per episode: {report['avg_greps_per_episode']}", file=sys.stderr)
    print(f"  Estimated grep turns saved: {report['estimated_grep_turns_saved']}", file=sys.stderr)
    print(f"  Estimated cost saved: ${report['estimated_cost_saved_usd']:.4f}", file=sys.stderr)
    print(
        f"  Generated {len(pairs)} query pairs from grep result files ({len(true_map)} unique queries)", file=sys.stderr
    )
    print(f"{'=' * 60}\n", file=sys.stderr)

    # Deduplicate pairs (same query + tid + prefix)
    deduped_pairs: list[tuple[str, str, str]] = []
    seen_pairs: set[tuple[str, str, str]] = set()
    for q, tid, prefix in pairs:
        key = (q, tid, prefix)
        if key not in seen_pairs:
            seen_pairs.add(key)
            deduped_pairs.append((q, tid, prefix))

    # -----------------------------------------------------------------------
    # 4. (Optional) Augment with synthetic pairs mined from repo source
    # -----------------------------------------------------------------------
    if args.synthetic:
        try:
            from synthetic_pair_miner import mine_synthetic_pairs as _mine_synthetic

            _syn_pairs, _syn_true = _mine_synthetic(
                repo_dir=Path.cwd(),
                repo_prefix=repo_prefix,
                max_queries_per_file=args.synthetic_per_file,
                verbose=True,
            )
            if _syn_pairs:
                print(
                    f"[synthetic] Adding {len(_syn_pairs)} synthetic pairs + {len(_syn_true)} true_map entries",
                    file=sys.stderr,
                )
                deduped_pairs.extend(_syn_pairs)
                true_map.update(_syn_true)
        except ImportError:
            print("[synthetic] WARNING: synthetic_pair_miner module not found. Skipping.", file=sys.stderr)
        except Exception as exc:
            print(f"[synthetic] WARNING: synthetic mining failed: {exc}", file=sys.stderr)

    total_pairs = len(deduped_pairs)
    print(f"  Total pairs after all mining: {total_pairs}", file=sys.stderr)
    if args.synthetic:
        syn_count = total_pairs - len(pairs)
        print(f"    ({len(pairs)} from sessions + {syn_count} synthetic)", file=sys.stderr)

    out_data = {
        # grep result files are *content* golds (files where the pattern
        # appears), not definition golds.  Label them correctly so the MRR
        # benchmark renders the "content" column instead of mislabelling as
        # "definition" and making scores look artificially low.
        "gold_kind": "content",
        "pairs": deduped_pairs,
        "true_map": true_map,
        "repos": {
            repo_prefix: {
                "ws": str(Path.cwd().resolve()),
            }
        },
    }

    with open(args.out, "w") as f:
        json.dump(out_data, f, indent=2)
    print(
        f"[session] Pairs written to {args.out} ({len(deduped_pairs)} pairs"
        f"{', includes synthetic' if args.synthetic else ''})",
        file=sys.stderr,
    )

    # Run eval if requested
    if args.run_eval and len(deduped_pairs) > 0:
        env = dict(os.environ)
        env["FITNESS_PAIRS"] = str(args.out)
        if args.channel == "cg":
            cmd = [sys.executable, "benchmarks/codebench/eval_cg_mrr.py"]
        else:
            # LemonCrow runs through the shipped MCP surface like any provider;
            # channel variants are env toggles the server honours.
            env["EVAL_CHANNEL_LABEL"] = args.channel
            if args.channel == "lexical":
                env["LEMONCROW_ZOEKT_MODE"] = "off"
                env["LEMONCROW_EXPLORE_SEMANTIC"] = "0"
            elif args.channel == "lexical+zoekt":
                env["LEMONCROW_EXPLORE_SEMANTIC"] = "0"
            cmd = [
                sys.executable,
                "benchmarks/codebench/eval_external_provider_mrr.py",
                "--provider",
                "lemoncrow",
            ]
            if args.full:
                cmd.append("--full")
            else:
                cmd.append("--sample")
                cmd.append("100")

        print(f"\n[session] Running eval: {' '.join(cmd)}", file=sys.stderr)
        r = subprocess.run(cmd, cwd=Path.cwd(), env=env, capture_output=True, text=True)
        for line in r.stderr.split("\n"):
            if line.strip():
                print(f"  [eval] {line}", file=sys.stderr)
        if r.stdout.strip():
            try:
                result = json.loads(r.stdout)
                print("\n  EVAL RESULT:", file=sys.stderr)
                print(
                    f"    MRR={result.get('mrr', '?'):.4f}  hit@1={result.get('hit1', '?'):.4f}  hit@3={result.get('hit3', '?'):.4f}  n={result.get('n', '?')}",
                    file=sys.stderr,
                )
                print(f"    latency_mean={result.get('latency_ms', {}).get('mean', '?'):.1f}ms", file=sys.stderr)
            except json.JSONDecodeError:
                print(f"  [eval] stdout: {r.stdout[:500]}", file=sys.stderr)


if __name__ == "__main__":
    main()
