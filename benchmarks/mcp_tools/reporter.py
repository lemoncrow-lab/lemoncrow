"""Terminal reporter for MCP tool benchmark results."""

from __future__ import annotations

from benchmarks.mcp_tools.harness import ToolReport

_GREEN = "\033[32m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_CYAN = "\033[36m"
_BOLD = "\033[1m"
_RESET = "\033[0m"
_DIM = "\033[2m"


def _pass_fail(passed: bool) -> str:
    return f"{_GREEN}✓{_RESET}" if passed else f"{_RED}✗{_RESET}"


def _bar(pct: float, width: int = 20) -> str:
    filled = int(pct / 100 * width)
    return f"{'█' * filled}{'░' * (width - filled)}"


def render_tool_report(report: ToolReport) -> str:
    lines: list[str] = []

    # Header
    status_color = _GREEN if report.failed == 0 else _RED
    lines.append(
        f"\n{_BOLD}{_CYAN}● {report.tool_name}{_RESET}  {status_color}{report.passed}/{report.total} passed{_RESET}"
    )
    lines.append(
        f"  {_DIM}avg savings {report.avg_savings_pct:.0f}%  "
        f"total tokens saved {report.total_saved_tokens:,}  "
        f"effective tokens {report.total_effective_tokens:,.0f}{_RESET}"
    )
    lines.append("")

    # Column headers
    col_w = 36
    lines.append(
        f"  {'op':<{col_w}} {'status':<8} {'lemoncrow':>8} {'baseline':>9} "
        f"{'input':>9} {'saved':>7} {'saving%':>8} {'effective':>10}  {'ms':>5}"
    )
    lines.append(f"  {'-' * col_w} {'-' * 7} {'-' * 8} {'-' * 9} {'-' * 9} {'-' * 7} {'-' * 8} {'-' * 10}  {'-' * 5}")

    for r in report.results:
        status = _pass_fail(r.passed)
        saved_str = f"{r.tokens_saved:,}" if r.baseline_tokens > 0 else "—"
        pct_str = f"{r.savings_pct:.0f}%" if r.baseline_tokens > 0 else "—"
        baseline_str = f"{r.baseline_tokens:,}" if r.baseline_tokens > 0 else "—"
        input_str = f"{r.input_file_tokens:,}" if r.input_file_tokens > 0 else "—"
        effective_str = f"{r.effective_tokens:,.0f}"
        label = r.case.label[:col_w]
        lines.append(
            f"  {label:<{col_w}} {status}{'  ':<6} {r.lemoncrow_tokens:>8,} {baseline_str:>9} {input_str:>9} "
            f"{saved_str:>7} {pct_str:>8} {effective_str:>10}  {r.elapsed_ms:>5.0f}"
        )
        if not r.passed:
            lines.append(f"  {_RED}    └ {r.failure}{_RESET}")
        if r.baseline_commands:
            lines.append(f"  {_DIM}    cmds: {len(r.baseline_commands)} fallback commands{_RESET}")
        if r.spill_probe_hits > 0:
            lines.append(f"  {_DIM}    spill-probe: hits={r.spill_probe_hits} tokens={r.spill_probe_tokens:,}{_RESET}")

    return "\n".join(lines)


def render_summary(reports: list[ToolReport]) -> str:
    lines: list[str] = []
    total_passed = sum(r.passed for r in reports)
    total_cases = sum(r.total for r in reports)
    total_saved = sum(r.total_saved_tokens for r in reports)
    total_effective = sum(r.total_effective_tokens for r in reports)

    lines.append(f"\n{_BOLD}━━ LemonCrow MCP Benchmark ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{_RESET}")
    for report in reports:
        lines.append(render_tool_report(report))

    lines.append(f"\n{_BOLD}━━ Summary ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{_RESET}")
    lines.append(f"  tools:         {len(reports)}")
    lines.append(f"  cases:         {total_cases}")
    lines.append(f"  passed:        {total_passed} / {total_cases}")
    lines.append(f"  tokens saved:  {total_saved:,}")
    lines.append(f"  effective:     {total_effective:,.0f}")
    if reports:
        avg = sum(r.avg_savings_pct for r in reports) / len(reports)
        lines.append(f"  avg savings:   {avg:.0f}%")
    lines.append("")
    return "\n".join(lines)
