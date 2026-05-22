"""
Atelier tool benchmark CLI.

Usage:
    uv run python -m benchmarks.tool_bench
    uv run python -m benchmarks.tool_bench --hosts claude codex
    uv run python -m benchmarks.tool_bench --tools read shell
    uv run python -m benchmarks.tool_bench --hosts all --tools all --out /tmp/bench.json
    uv run python -m benchmarks.tool_bench --check-only   # enforcement + savings audit only
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .runner import HOSTS, run_benchmark
from .report import (
    export_json,
    print_enforcement_gap,
    print_report,
    print_savings_events,
    print_savings_table,
    print_statusline_preview,
)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="python -m benchmarks.tool_bench",
        description="Measure savings + correctness: builtin vs atelier MCP across all host CLIs.",
    )
    p.add_argument(
        "--hosts",
        nargs="+",
        default=["claude"],
        metavar="HOST",
        help=f"Hosts to test (default: claude). Use 'all' for all: {', '.join(HOSTS)}",
    )
    p.add_argument(
        "--tools",
        nargs="+",
        default=["read", "shell", "search"],
        metavar="TOOL",
        help="Tools to benchmark: read shell search (default: all three)",
    )
    p.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="PATH",
        help="Write JSON report to PATH",
    )
    p.add_argument(
        "--check-only",
        action="store_true",
        help="Skip benchmark, only run enforcement audit + savings events check",
    )
    p.add_argument(
        "--no-statusline",
        action="store_true",
        help="Skip statusline preview",
    )
    p.add_argument(
        "--atelier-root",
        type=Path,
        default=None,
        metavar="DIR",
        help="Override ATELIER_ROOT (default: ~/.atelier)",
    )
    args = p.parse_args(argv)

    # Resolve hosts
    hosts: tuple[str, ...]
    if "all" in args.hosts:
        hosts = HOSTS
    else:
        hosts = tuple(args.hosts)

    # Resolve tools
    tools_all = ("read", "shell", "search")
    if "all" in args.tools:
        tools: tuple[str, ...] = tools_all
    else:
        tools = tuple(t for t in args.tools if t in tools_all)

    atelier_root = args.atelier_root

    # ── Enforcement audit ──────────────────────────────────────────────────
    print_enforcement_gap()

    # ── Savings events check ───────────────────────────────────────────────
    print_savings_events(atelier_root)

    # ── Statusline preview ─────────────────────────────────────────────────
    if not args.no_statusline:
        print_statusline_preview(atelier_root)

    if args.check_only:
        return 0

    # ── Benchmark ─────────────────────────────────────────────────────────
    print(f"\n\033[1;35m{'='*76}\033[0m")
    print(f"\033[1;35m  RUNNING BENCHMARK: tools={list(tools)}  hosts={list(hosts)}\033[0m")
    print(f"\033[1;35m{'='*76}\033[0m")
    print(f"  \033[2m(each atelier call spawns a new stdio process — ~2s overhead is expected)\033[0m\n")

    report = run_benchmark(tools=tools, hosts=hosts)

    print_report(report)
    print_savings_table(report)

    if args.out:
        export_json(report, args.out)

    return 0


if __name__ == "__main__":
    sys.exit(main())
