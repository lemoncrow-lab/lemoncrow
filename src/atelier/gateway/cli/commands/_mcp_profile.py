"""MCP tool latency profiling with history + drift detection.

Backing logic for ``atelier perf``. Drives the MCP dispatch (``_handle``) for a
representative set of tool calls against a target repo, measures cold + warm
latency per tool AND the handler-vs-pipeline split, and compares a run against
the last recorded one so drift is visible -- including *where* a regression is
(the tool's own handler vs the dispatch pipeline overhead). History is plain
JSONL (one run per line, newest last), keyed by git sha/branch.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import statistics as st
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Profiled: deterministic, local, side-effect-free calls. Fixed args so runs stay
# comparable over time. ``edit`` mutates, so it is profiled separately against a
# self-contained scratch file.
PROFILED_CALLS: list[tuple[str, dict[str, Any]]] = [
    ("read", {"path": "README.md"}),
    ("grep", {"regex": "def ", "path": "src/atelier/core", "mode": "paths"}),
    ("grep", {"regex": "def _workspace_root", "path": "src/atelier"}),
    ("relations", {"symbol": "_workspace_root", "kind": "usages"}),
    ("search", {"query": "edit verify gate", "path": "."}),
    ("graph", {"path": "src/atelier/core/foundation/paths.py"}),
    ("blame", {"symbol_name": "_workspace_root"}),
    ("orient", {}),
    ("cache", {}),
    ("statusline_segment", {}),
]

# Tools deliberately NOT micro-profiled here, with why -- shown so "all tools" is
# honest about coverage. These are external, stateful, mutating, or so slow that a
# synthetic probe would be meaningless or harmful.
SKIP_REASONS: dict[str, str] = {
    "bash": "runs shell commands",
    "web_fetch": "network call",
    "agent": "spawns a subagent",
    "workflow": "spawns a workflow",
    "sql": "needs a db connection",
    "memory": "writes durable state",
    "verify": "remote/stateful",
    "rescue": "remote/stateful",
    "trace": "remote/stateful",
    "compact": "mutates context state",
    "context": "routed remotely",
    "scan": "full-repo scan (tens of seconds)",
    "index": "rebuilds the index",
    "codemod": "mutates files",
}

DEFAULT_HISTORY_REL = "reports/perf/mcp_latency_history.jsonl"
# A warm-latency drift under this many ms is jitter, not a regression -- a flat
# percentage alone flags fast tools (e.g. grep) on normal run noise.
DEFAULT_MIN_ABS_MS = 10.0


def default_history_path(repo: Path) -> Path:
    return repo / DEFAULT_HISTORY_REL


def _git(repo: Path, *args: str) -> str:
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def _call(handle: Callable[[dict[str, Any]], Any], name: str, args: dict[str, Any], rid: int) -> None:
    handle({"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": args}})


def _time_tool(
    handle: Callable[[dict[str, Any]], Any],
    name: str,
    args: dict[str, Any],
    *,
    warmup: int,
    runs: int,
    base_rid: int,
) -> dict[str, Any]:
    """Return cold (first in-process call) and warm-median wall latency in ms."""
    t0 = time.perf_counter()
    _call(handle, name, args, base_rid)
    cold = (time.perf_counter() - t0) * 1000
    for i in range(warmup):
        _call(handle, name, args, base_rid + 1 + i)
    samples: list[float] = []
    for i in range(runs):
        t = time.perf_counter()
        _call(handle, name, args, base_rid + 1 + warmup + i)
        samples.append((time.perf_counter() - t) * 1000)
    return {
        "cold_ms": round(cold, 1),
        "warm_ms": round(st.median(samples), 1),
        "warm_p95_ms": round(max(samples), 1),
        "runs": runs,
    }


def _profile_edit(
    handle: Callable[[dict[str, Any]], Any],
    repo: Path,
    *,
    warmup: int,
    runs: int,
    base_rid: int,
) -> dict[str, Any] | None:
    """Profile ``edit`` against a throwaway file created+removed in the repo."""
    scratch = repo / "._perf_edit_probe.py"
    try:
        scratch.write_text("VALUE = 0\n", encoding="utf-8")
        rel = scratch.name
        flip = {"0": "1", "1": "0"}
        samples: list[float] = []
        cold = 0.0
        cur = "0"
        for i in range(1 + warmup + runs):
            nxt = flip[cur]
            t = time.perf_counter()
            _call(
                handle,
                "edit",
                {"edits": [{"path": rel, "old_string": f"VALUE = {cur}", "new_string": f"VALUE = {nxt}"}]},
                base_rid + i,
            )
            dt = (time.perf_counter() - t) * 1000
            cur = nxt
            if i == 0:
                cold = dt
            elif i >= 1 + warmup:
                samples.append(dt)
        if not samples:
            return None
        return {
            "cold_ms": round(cold, 1),
            "warm_ms": round(st.median(samples), 1),
            "warm_p95_ms": round(max(samples), 1),
            "runs": runs,
        }
    finally:
        scratch.unlink(missing_ok=True)


def _merge_breakdown(tools: dict[str, Any], sink: Path) -> None:
    """Fold the per-call handler/overhead split (from the _handle profile sink)
    into each tool's record, so a regression can be attributed to the tool's own
    handler vs the dispatch pipeline. The first sample per tool is the cold call;
    drop it before taking the warm median."""
    if not sink.exists():
        return
    by_tool: dict[str, dict[str, list[float]]] = {}
    for line in sink.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = rec.get("tool")
        if name not in tools:
            continue
        slot = by_tool.setdefault(name, {"handler": [], "overhead": []})
        slot["handler"].append(float(rec.get("handler_ms", 0)))
        slot["overhead"].append(float(rec.get("overhead_ms", 0)))
    for name, slot in by_tool.items():
        handler = slot["handler"][1:] or slot["handler"]
        overhead = slot["overhead"][1:] or slot["overhead"]
        if handler:
            tools[name]["handler_ms"] = round(st.median(handler), 1)
        if overhead:
            tools[name]["overhead_ms"] = round(st.median(overhead), 1)


def run_profile(repo: Path, *, warmup: int = 2, runs: int = 7, include_edit: bool = True) -> dict[str, Any]:
    """Profile the MCP tools against *repo* and return a run record."""
    os.environ["ATELIER_WORKSPACE_ROOT"] = str(repo)
    sink_dir = Path(tempfile.mkdtemp(prefix="atelier-prof-"))
    sink = sink_dir / "calls.jsonl"
    os.environ["ATELIER_TOOL_PROFILE_PATH"] = str(sink)
    try:
        from atelier.gateway.adapters import mcp_server as mcp

        handle = mcp._handle
        tools: dict[str, Any] = {}
        rid = 1
        for name, args in PROFILED_CALLS:
            tools[name] = _time_tool(handle, name, args, warmup=warmup, runs=runs, base_rid=rid)
            rid += 100
        if include_edit:
            edit_stats = _profile_edit(handle, repo, warmup=warmup, runs=runs, base_rid=rid)
            if edit_stats is not None:
                tools["edit"] = edit_stats
        _merge_breakdown(tools, sink)
    finally:
        os.environ.pop("ATELIER_TOOL_PROFILE_PATH", None)
        with contextlib.suppress(Exception):
            shutil.rmtree(sink_dir, ignore_errors=True)
    return {
        "ts": time.time(),
        "git_sha": _git(repo, "rev-parse", "--short", "HEAD"),
        "git_branch": _git(repo, "rev-parse", "--abbrev-ref", "HEAD"),
        "repo": str(repo),
        "tools": tools,
        "skipped": dict(SKIP_REASONS),
    }


def _iter_history(history: Path) -> list[dict[str, Any]]:
    if not history.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in history.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def load_last_run(history: Path, repo: str) -> dict[str, Any] | None:
    """Most recent prior run recorded for *repo* (or None)."""
    matches = [rec for rec in _iter_history(history) if rec.get("repo") == repo]
    return matches[-1] if matches else None


def append_history(history: Path, record: dict[str, Any]) -> None:
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def _fmt_when(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def _breakdown_block(name: str, stats: dict[str, Any], prev: dict[str, Any]) -> list[str]:
    """Per-regression detail: where the time went (handler vs pipeline) and cold/tail."""
    rows: list[tuple[str, str]] = [
        ("warm wall", "warm_ms"),
        ("  handler (tool work)", "handler_ms"),
        ("  pipeline overhead", "overhead_ms"),
        ("cold start", "cold_ms"),
    ]
    out = [f"  {name}:"]
    for label, key in rows:
        cur = stats.get(key)
        if cur is None:
            continue
        pv = prev.get(key)
        if isinstance(pv, (int, float)) and pv:
            delta = cur - pv
            pct = delta / pv * 100
            out.append(f"    {label:24} {pv:>8.1f} -> {cur:>8.1f} ms  ({delta:+.1f}, {pct:+.0f}%)")
        else:
            out.append(f"    {label:24} {cur:>8.1f} ms  (no prior)")
    p95 = stats.get("warm_p95_ms")
    if isinstance(p95, (int, float)):
        out.append(f"    {'warm p95 (tail)':24} {p95:>8.1f} ms")
    return out


def render_drift(
    current: dict[str, Any], prev: dict[str, Any] | None, threshold: float, min_abs_ms: float = DEFAULT_MIN_ABS_MS
) -> tuple[str, bool]:
    """Render the per-tool drift table + a breakdown for each regression.

    A tool is flagged only when warm drift exceeds BOTH the percentage threshold
    and *min_abs_ms* absolute, so sub-10ms jitter on fast tools is not a false
    regression. Returns (text, any_regression).
    """
    lines: list[str] = []
    lines.append(
        f"MCP tool latency  --  {current.get('git_branch', '?')}@{current.get('git_sha', '?')}  ({_fmt_when(current['ts'])})"
    )
    if prev is not None:
        same = prev.get("git_sha") == current.get("git_sha")
        note = "" if same else "  [different commit -- drift mixes code + repo-state change]"
        lines.append(
            f"comparing vs previous run {prev.get('git_branch', '?')}@{prev.get('git_sha', '?')} ({_fmt_when(prev['ts'])}){note}"
        )
    else:
        lines.append("no previous run for this repo -- baseline only")
    lines.append("")
    hdr = f"{'tool':18}{'cold_ms':>9}{'warm_ms':>9}{'prev':>8}{'Δms':>8}{'drift':>8}"
    lines.append(hdr)
    lines.append("-" * (len(hdr) + 11))
    regressions: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    prev_tools = (prev or {}).get("tools", {})
    for name, stats in current["tools"].items():
        warm = stats["warm_ms"]
        pstats = prev_tools.get(name)
        if pstats and pstats.get("warm_ms"):
            pw = pstats["warm_ms"]
            delta = warm - pw
            drift = delta / pw * 100 if pw else 0.0
            flag = ""
            if drift > threshold and delta > min_abs_ms:
                flag = "  ⚠ REGRESS"
                regressions.append((name, stats, pstats))
            elif drift < -threshold and -delta > min_abs_ms:
                flag = "  ✓ faster"
            lines.append(f"{name:18}{stats['cold_ms']:>9.0f}{warm:>9.1f}{pw:>8.1f}{delta:>+8.1f}{drift:>+7.0f}%{flag}")
        else:
            lines.append(f"{name:18}{stats['cold_ms']:>9.0f}{warm:>9.1f}{'--':>8}{'--':>8}{'new':>8}")
    lines.append("-" * (len(hdr) + 11))
    runs = current["tools"][next(iter(current["tools"]))]["runs"] if current["tools"] else 0
    lines.append(
        f"flag = drift > ±{threshold:.0f}% AND > {min_abs_ms:.0f}ms  |  cold = first call, warm = median of {runs}"
    )
    skipped = current.get("skipped") or {}
    if skipped:
        lines.append("not profiled: " + ", ".join(f"{t} ({why})" for t, why in sorted(skipped.items())))
    if regressions:
        lines.append("")
        lines.append("Regression breakdown (where the time went):")
        for name, stats, pstats in regressions:
            lines.extend(_breakdown_block(name, stats, pstats))
    return "\n".join(lines), bool(regressions)


def summarize_history(history: Path, repo: str, last: int = 10) -> str:
    """Render warm_ms per tool across the last *last* recorded runs for *repo*."""
    records = [rec for rec in _iter_history(history) if rec.get("repo") == repo][-last:]
    if not records:
        return f"no recorded runs for {repo} in {history}"
    tool_names: list[str] = []
    for rec in records:
        for name in rec.get("tools", {}):
            if name not in tool_names:
                tool_names.append(name)
    lines: list[str] = [f"warm_ms history ({len(records)} runs) -- {history}", ""]
    hdr = f"{'when':18}{'sha':10}" + "".join(f"{n[:9]:>10}" for n in tool_names)
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for rec in records:
        row = f"{_fmt_when(rec['ts']):18}{rec.get('git_sha', '?')!s:10}"
        for n in tool_names:
            v = rec.get("tools", {}).get(n, {}).get("warm_ms")
            row += f"{v:>10.1f}" if isinstance(v, (int, float)) else f"{'--':>10}"
        lines.append(row)
    return "\n".join(lines)
