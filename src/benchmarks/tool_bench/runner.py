"""Core benchmark runner: builtin baseline vs atelier MCP per host."""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
    return d.get("result", {}).get("structuredContent", {}) or {}


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


# ---------------------------------------------------------------------------
# Per-tool benchmarkers
# ---------------------------------------------------------------------------
def bench_read(
    label: str,
    args: dict[str, Any],
    hosts: tuple[str, ...],
) -> list[ToolResult]:
    results: list[ToolResult] = []
    path = args["path"]

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
                    label=label, tool="read", variant=host,
                    correct=False, chars_out=0, tokens_est=0,
                    elapsed_ms=a_t * 1000, error=str(d.get("error", "")),
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
                label=label, tool="read", variant=host,
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
    cmd = args["command"]
    cwd = args.get("cwd")

    b_out, b_t = _builtin_shell(cmd, cwd)
    b_lines = len(b_out.splitlines())
    b_chars = len(b_out)
    results.append(
        ToolResult(
            label=label, tool="shell", variant="builtin",
            correct=True, chars_out=b_chars, tokens_est=b_chars // 4,
            elapsed_ms=b_t * 1000,
        )
    )

    for host in hosts:
        d, a_t = _mcp_call("shell", args, host)
        sc = _structured(d)
        if "error" in d and "result" not in d:
            results.append(
                ToolResult(
                    label=label, tool="shell", variant=host,
                    correct=False, chars_out=0, tokens_est=0,
                    elapsed_ms=a_t * 1000, error=str(d.get("error", "")),
                )
            )
            continue

        a_out = sc.get("stdout", "") or sc.get("output", "") or str(sc)
        a_chars = len(a_out)
        a_lines = len(a_out.splitlines())
        first_token = b_out.strip().split("\n")[0].strip() if b_out.strip() else ""
        correct = bool(first_token) and first_token in a_out
        max_lines = int(args.get("max_lines", 200))
        truncated = b_lines > max_lines and a_lines <= max_lines

        results.append(
            ToolResult(
                label=label, tool="shell", variant=host,
                correct=correct, chars_out=a_chars, tokens_est=a_chars // 4,
                elapsed_ms=a_t * 1000,
                extra={
                    "baseline_lines": b_lines,
                    "atelier_lines": a_lines,
                    "truncated": truncated,
                    "ansi_stripped": True,
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
    path = args.get("path", ".")

    b_out, b_t = _builtin_grep(pattern, path)
    b_lines = len(b_out.splitlines())
    b_chars = len(b_out)
    results.append(
        ToolResult(
            label=label, tool="search", variant="builtin",
            correct=b_lines > 0, chars_out=b_chars, tokens_est=b_chars // 4,
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
                    label=label, tool="search", variant=host,
                    correct=False, chars_out=0, tokens_est=0,
                    elapsed_ms=a_t * 1000, error=str(d.get("error", "")),
                )
            )
            continue

        meta = sc.get("_meta", {})
        file_hits = int(meta.get("fileMatchCount") or 0)
        content_blocks = sc.get("content", [])
        result_blocks = max(len(content_blocks) - 1, 0)  # block[0] is header
        a_chars = sum(len(b.get("text", "")) for b in content_blocks)
        correct = file_hits > 0 and b_lines > 0

        saving_chars = b_chars - a_chars
        saving_pct = 100.0 * saving_chars / max(b_chars, 1)
        results.append(
            ToolResult(
                label=label, tool="search", variant=host,
                correct=correct, chars_out=a_chars, tokens_est=a_chars // 4,
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


# ---------------------------------------------------------------------------
# Top-level runner
# ---------------------------------------------------------------------------
def run_benchmark(
    tools: tuple[str, ...] = ("read", "shell", "search"),
    hosts: tuple[str, ...] = HOSTS,
    cases_override: dict[str, list[tuple[str, dict]]] | None = None,
) -> BenchReport:
    from .cases import ALL_CASES

    cases = cases_override or ALL_CASES
    report = BenchReport()

    BENCH_FN = {
        "read": bench_read,
        "shell": bench_shell,
        "search": bench_search,
    }

    for tool in tools:
        if tool not in BENCH_FN:
            continue
        fn = BENCH_FN[tool]
        for label, args in cases.get(tool, []):
            for r in fn(label, args, hosts):
                report.add(r)

    return report
