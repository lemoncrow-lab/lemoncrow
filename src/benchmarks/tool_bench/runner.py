"""Core benchmark runner: builtin baseline vs atelier MCP per host."""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ATELIER_ROOT = os.environ.get("ATELIER_ROOT", str(Path.home() / ".atelier"))
MCP_BIN = os.environ.get("ATELIER_MCP_BIN", "atelier-mcp")

HOSTS = ("claude", "codex", "antigravity", "copilot", "opencode")


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------
@dataclass
class ToolResult:
    label: str
    tool: str  # "read" | "shell" | "search"
    variant: str  # "builtin" or host name
    correct: bool
    chars_out: int
    tokens_est: int  # chars // 4
    elapsed_ms: float
    extra: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class BenchReport:
    results: list[ToolResult] = field(default_factory=list)

    def add(self, r: ToolResult) -> None:
        self.results.append(r)

    def for_tool(self, tool: str) -> list[ToolResult]:
        return [r for r in self.results if r.tool == tool]

    def for_variant(self, variant: str) -> list[ToolResult]:
        return [r for r in self.results if r.variant == variant]


# ---------------------------------------------------------------------------
# MCP helper
# ---------------------------------------------------------------------------
def _mcp_call(tool: str, args: dict[str, Any], host: str, timeout: int = 30) -> tuple[dict[str, Any], float]:
    msgs = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "bench", "version": "1"},
                "capabilities": {},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}},
    ]
    inp = "\n".join(json.dumps(m) for m in msgs) + "\n"
    env = {
        **os.environ,
        "ATELIER_ROOT": ATELIER_ROOT,
        "ATELIER_NO_AUTO_UPDATE": "1",
        "ATELIER_DEV_MODE": "1",
    }
    t0 = time.perf_counter()
    try:
        proc = subprocess.run(
            [MCP_BIN, "--host", host],
            input=inp,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        elapsed = time.perf_counter() - t0
        for line in proc.stdout.splitlines():
            try:
                d = json.loads(line)
                if d.get("id") == 2:
                    return d, elapsed
            except Exception:
                pass
        return {"error": proc.stderr[:300] or "no response"}, elapsed
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {timeout}s"}, timeout * 1000.0


def _structured(d: dict[str, Any]) -> dict[str, Any]:
    result = d.get("result", {})
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        if isinstance(first, dict):
            text = first.get("text")
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    return {}
                if isinstance(parsed, dict):
                    return parsed
    return {}


# ---------------------------------------------------------------------------
# Builtin baselines (pure Python / subprocess — no atelier)
# ---------------------------------------------------------------------------
def _builtin_read(path: str) -> tuple[str, float]:
    t0 = time.perf_counter()
    text = Path(path).read_text(encoding="utf-8")
    return text, time.perf_counter() - t0


def _builtin_shell(command: str, cwd: str | None = None) -> tuple[str, float]:
    t0 = time.perf_counter()
    r = subprocess.run(command, shell=True, capture_output=True, text=True, cwd=cwd, timeout=15)
    return r.stdout + r.stderr, time.perf_counter() - t0


def _builtin_grep(pattern: str, path: str) -> tuple[str, float]:
    t0 = time.perf_counter()
    r = subprocess.run(
        [
            "rg",
            "-n",
            "--no-heading",
            "--color",
            "never",
            "--glob",
            "*.py",
            pattern,
            "src",
            "tests",
        ],
        capture_output=True,
        text=True,
        cwd=path,
        timeout=15,
    )
    return r.stdout, time.perf_counter() - t0


def _builtin_grep_direct(args: dict[str, Any]) -> tuple[str, float]:
    """Run rg directly matching the atelier grep tool args."""
    pattern = str(args.get("content_regex", ""))
    path = str(args.get("file_path", "."))
    globs: list[str] = args.get("file_glob_patterns") or []
    output_mode = str(args.get("output_mode") or "file_paths_with_content")
    ignore_case = bool(args.get("ignore_case"))
    multiline = bool(args.get("multiline"))
    lines_before = int(args.get("lines_before") or 0)
    lines_after = int(args.get("lines_after") or 0)

    cmd = ["rg", "--no-heading", "--color", "never"]
    if output_mode == "file_paths_only":
        cmd.append("--files-with-matches")
    elif output_mode == "file_paths_with_match_count":
        cmd.append("--count")
    else:
        cmd.append("-n")
    if ignore_case:
        cmd.append("-i")
    if multiline:
        cmd.append("--multiline")
    if lines_before:
        cmd += ["-B", str(lines_before)]
    if lines_after:
        cmd += ["-A", str(lines_after)]
    for g in globs:
        cmd += ["--glob", g]
    cmd += [pattern, path]

    t0 = time.perf_counter()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return r.stdout, time.perf_counter() - t0


# ---------------------------------------------------------------------------
# Per-tool benchmarkers
# ---------------------------------------------------------------------------
def bench_read(
    label: str,
    args: dict[str, Any],
    hosts: tuple[str, ...],
) -> list[ToolResult]:
    results: list[ToolResult] = []
    path = str(args.get("file_path") or args.get("path") or "")
    if not path:
        raise ValueError("read benchmark case requires file_path")

    # Builtin baseline
    b_text, b_t = _builtin_read(path)
    b_chars = len(b_text)
    results.append(
        ToolResult(
            label=label,
            tool="read",
            variant="builtin",
            correct=True,
            chars_out=b_chars,
            tokens_est=b_chars // 4,
            elapsed_ms=b_t * 1000,
        )
    )

    # Atelier per host
    for host in hosts:
        d, a_t = _mcp_call("read", args, host)
        sc = _structured(d)
        if "error" in d and "result" not in d:
            results.append(
                ToolResult(
                    label=label,
                    tool="read",
                    variant=host,
                    correct=False,
                    chars_out=0,
                    tokens_est=0,
                    elapsed_ms=a_t * 1000,
                    error=str(d.get("error", "")),
                )
            )
            continue

        content = sc.get("content") or ""
        outline = sc.get("outline")
        a_chars = len(str(content)) + len(str(outline or ""))
        mode = sc.get("mode", "?")
        cached = bool(sc.get("cache_hit"))
        tok_saved = int(sc.get("tokens_saved") or 0)

        # Correctness: first 100 chars of source appear in content, OR outline present for .py/.ts
        is_code = Path(path).suffix in (".py", ".ts", ".tsx", ".js")
        correct = (b_text[:100].strip() in str(content)) or (is_code and outline is not None)

        saving_chars = b_chars - a_chars
        saving_pct = 100.0 * saving_chars / max(b_chars, 1)
        results.append(
            ToolResult(
                label=label,
                tool="read",
                variant=host,
                correct=correct,
                chars_out=a_chars,
                tokens_est=a_chars // 4,
                elapsed_ms=a_t * 1000,
                extra={
                    "mode": mode,
                    "cache_hit": cached,
                    "tokens_saved": tok_saved,
                    "saving_chars": saving_chars,
                    "saving_pct": saving_pct,
                    "has_outline": outline is not None,
                    "baseline_chars": b_chars,
                },
            )
        )
    return results


def bench_shell(
    label: str,
    args: dict[str, Any],
    hosts: tuple[str, ...],
) -> list[ToolResult]:
    results: list[ToolResult] = []
    bench_expect = cast(dict[str, Any], args.get("_expect") or {})
    call_args = {k: v for k, v in args.items() if k != "_expect"}
    cmd = call_args["command"]
    cwd = call_args.get("cwd")

    b_out, b_t = _builtin_shell(cmd, cwd)
    b_lines = len(b_out.splitlines())
    b_chars = len(b_out)
    results.append(
        ToolResult(
            label=label,
            tool="shell",
            variant="builtin",
            correct=True,
            chars_out=b_chars,
            tokens_est=b_chars // 4,
            elapsed_ms=b_t * 1000,
        )
    )

    for host in hosts:
        d, a_t = _mcp_call("shell", call_args, host)
        sc = _structured(d)
        if "error" in d and "result" not in d:
            results.append(
                ToolResult(
                    label=label,
                    tool="shell",
                    variant=host,
                    correct=False,
                    chars_out=0,
                    tokens_est=0,
                    elapsed_ms=a_t * 1000,
                    error=str(d.get("error", "")),
                )
            )
            continue

        a_out = sc.get("stdout", "") or sc.get("stderr", "") or sc.get("output", "") or ""
        a_chars = len(a_out)
        a_lines = len(a_out.splitlines())
        first_token = b_out.strip().split("\n")[0].strip() if b_out.strip() else ""
        rewritten = bool(sc.get("rewritten", False))
        rewrite_target = str(sc.get("rewrite_target") or "")
        exit_code = int(sc.get("exit_code", 0)) if isinstance(sc.get("exit_code"), int | float) else 0

        if bench_expect:
            expected_rewritten = bench_expect.get("rewritten")
            expected_target = str(bench_expect.get("rewrite_target") or "")
            expected_exit = bench_expect.get("exit_code")
            rewritten_ok = expected_rewritten is None or bool(expected_rewritten) == rewritten
            target_ok = not expected_target or rewrite_target == expected_target
            exit_ok = expected_exit is None or int(expected_exit) == exit_code
            correct = rewritten_ok and target_ok and exit_ok
        else:
            correct = bool(first_token) and first_token in a_out

        max_lines = int(call_args.get("max_lines", 200))
        truncated = b_lines > max_lines and a_lines <= max_lines

        results.append(
            ToolResult(
                label=label,
                tool="shell",
                variant=host,
                correct=correct,
                chars_out=a_chars,
                tokens_est=a_chars // 4,
                elapsed_ms=a_t * 1000,
                extra={
                    "baseline_lines": b_lines,
                    "atelier_lines": a_lines,
                    "truncated": truncated,
                    "ansi_stripped": True,
                    "rewritten": rewritten,
                    "rewrite_target": rewrite_target,
                    "exit_code": exit_code,
                },
            )
        )
    return results


def bench_search(
    label: str,
    args: dict[str, Any],
    hosts: tuple[str, ...],
) -> list[ToolResult]:
    results: list[ToolResult] = []
    pattern = args.get("content_regex") or args.get("query", "")
    path = str(args.get("file_path") or args.get("path") or ".")

    b_out, b_t = _builtin_grep(pattern, path)
    b_lines = len(b_out.splitlines())
    b_chars = len(b_out)
    results.append(
        ToolResult(
            label=label,
            tool="search",
            variant="builtin",
            correct=b_lines > 0,
            chars_out=b_chars,
            tokens_est=b_chars // 4,
            elapsed_ms=b_t * 1000,
            extra={"match_lines": b_lines},
        )
    )

    for host in hosts:
        d, a_t = _mcp_call("search", args, host)
        sc = _structured(d)
        if "error" in d and "result" not in d:
            results.append(
                ToolResult(
                    label=label,
                    tool="search",
                    variant=host,
                    correct=False,
                    chars_out=0,
                    tokens_est=0,
                    elapsed_ms=a_t * 1000,
                    error=str(d.get("error", "")),
                )
            )
            continue

        matches = [item for item in sc.get("matches", []) if isinstance(item, dict)]
        if matches:
            file_hits = len(matches)
            result_blocks = len(matches)
            a_chars = len(json.dumps(matches, ensure_ascii=False))
        else:
            # Backward-compat for older search payload shape.
            meta = sc.get("_meta", {})
            file_hits = int(meta.get("fileMatchCount") or 0)
            content_blocks = [b for b in sc.get("content", []) if isinstance(b, dict)]
            result_blocks = max(len(content_blocks) - 1, 0)  # block[0] is header
            a_chars = sum(len(str(b.get("text", ""))) for b in content_blocks)
        correct = file_hits > 0 and b_lines > 0

        saving_chars = b_chars - a_chars
        saving_pct = 100.0 * saving_chars / max(b_chars, 1)
        results.append(
            ToolResult(
                label=label,
                tool="search",
                variant=host,
                correct=correct,
                chars_out=a_chars,
                tokens_est=a_chars // 4,
                elapsed_ms=a_t * 1000,
                extra={
                    "file_hits": file_hits,
                    "result_blocks": result_blocks,
                    "budget_tokens": args.get("budget_tokens"),
                    "baseline_lines": b_lines,
                    "saving_chars": saving_chars,
                    "saving_pct": saving_pct,
                    "ranked": True,
                },
            )
        )
    return results


def bench_grep(
    label: str,
    args: dict[str, Any],
    hosts: tuple[str, ...],
) -> list[ToolResult]:
    results: list[ToolResult] = []

    b_out, b_t = _builtin_grep_direct(args)
    b_chars = len(b_out)
    b_lines = len([line for line in b_out.splitlines() if line.strip()])
    pattern = str(args.get("content_regex", ""))
    results.append(
        ToolResult(
            label=label,
            tool="grep",
            variant="builtin",
            correct=b_lines > 0,
            chars_out=b_chars,
            tokens_est=b_chars // 4,
            elapsed_ms=b_t * 1000,
            extra={"match_lines": b_lines},
        )
    )

    for host in hosts:
        d, a_t = _mcp_call("grep", args, host)
        sc = _structured(d)
        if "error" in d and "result" not in d:
            results.append(
                ToolResult(
                    label=label,
                    tool="grep",
                    variant=host,
                    correct=False,
                    chars_out=0,
                    tokens_est=0,
                    elapsed_ms=a_t * 1000,
                    error=str(d.get("error", "")),
                )
            )
            continue

        a_raw = json.dumps(sc, ensure_ascii=False)
        a_chars = len(a_raw)
        # Correctness: pattern or file match count present in output
        meta = sc.get("_meta", {})
        file_match_count = int(meta.get("fileMatchCount") or 0)
        correct = (file_match_count > 0 or pattern.replace(r"^\s+", "").replace("^", "") in a_raw) and b_lines > 0

        saving_chars = b_chars - a_chars
        saving_pct = 100.0 * saving_chars / max(b_chars, 1)
        results.append(
            ToolResult(
                label=label,
                tool="grep",
                variant=host,
                correct=correct,
                chars_out=a_chars,
                tokens_est=a_chars // 4,
                elapsed_ms=a_t * 1000,
                extra={
                    "file_match_count": file_match_count,
                    "baseline_lines": b_lines,
                    "saving_chars": saving_chars,
                    "saving_pct": saving_pct,
                },
            )
        )
    return results


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------
def run_benchmark(
    tools: tuple[str, ...] = ("read", "shell", "search"),
    hosts: tuple[str, ...] = HOSTS,
    cases_override: dict[str, list[tuple[str, dict[str, Any]]]] | None = None,
) -> BenchReport:
    from .cases import ALL_CASES

    cases = cases_override or ALL_CASES
    report = BenchReport()

    BENCH_FN = {
        "read": bench_read,
        "shell": bench_shell,
        "search": bench_search,
        "grep": bench_grep,
    }

    for tool in tools:
        if tool not in BENCH_FN:
            continue
        fn = BENCH_FN[tool]
        for label, args in cases.get(tool, []):
            for r in fn(label, args, hosts):
                report.add(r)

    return report
