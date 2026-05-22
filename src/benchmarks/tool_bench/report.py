"""Rich terminal + JSON report for the tool benchmark."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .runner import BenchReport, ToolResult

HOSTS_ORDERED = ("builtin", "claude", "codex", "antigravity", "copilot", "opencode")


def _tokens_est(chars: int) -> str:
    t = chars // 4
    return f"{t//1000}k" if t >= 1000 else str(t)


def _ms(ms: float) -> str:
    if ms >= 1000:
        return f"{ms/1000:.1f}s"
    return f"{ms:.0f}ms"


def _saving(extra: dict) -> str:
    pct = extra.get("saving_pct", 0)
    chars = extra.get("saving_chars", 0)
    if pct > 0:
        return f"\033[32m+{pct:.0f}% ({chars//1000}k)\033[0m" if chars >= 1000 else f"\033[32m+{pct:.0f}%\033[0m"
    return "±0%"


def _extra_info(r: ToolResult) -> str:
    ex = r.extra
    parts: list[str] = []
    if r.tool == "read":
        mode = ex.get("mode", "")
        if mode == "outline":
            parts.append("outline✓")
        elif mode == "full":
            parts.append("full")
        if ex.get("cache_hit"):
            parts.append("cached")
        ts = ex.get("tokens_saved", 0)
        if ts:
            parts.append(f"tok_saved={ts}")
    elif r.tool == "shell":
        if ex.get("truncated"):
            parts.append(f"truncated→{ex.get('atelier_lines')}ln")
        if ex.get("ansi_stripped"):
            parts.append("ansi✓")
    elif r.tool == "search":
        fh = ex.get("file_hits", 0)
        bl = ex.get("result_blocks", 0)
        if fh:
            parts.append(f"{fh}files/{bl}blks")
        bt = ex.get("budget_tokens")
        if bt:
            parts.append(f"budget={bt}")
        if ex.get("ranked"):
            parts.append("ranked✓")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# Terminal table
# ---------------------------------------------------------------------------
def print_report(report: BenchReport, *, show_per_host: bool = True) -> None:
    tools = sorted({r.tool for r in report.results})

    for tool in tools:
        rows = report.for_tool(tool)
        labels = list(dict.fromkeys(r.label for r in rows))
        variants = [v for v in HOSTS_ORDERED if any(r.variant == v for r in rows)]

        print(f"\n\033[1;35m{'='*76}\033[0m")
        print(f"\033[1;35m  {tool.upper()} TOOL\033[0m")
        print(f"\033[1;35m{'='*76}\033[0m")

        # Header
        col_w = 22
        host_w = 14
        hdr = f"  {'Case':<{col_w}}"
        for v in variants:
            hdr += f"  {v:<{host_w}}"
        print(f"\033[2m{hdr}\033[0m")
        print(f"\033[2m  {'-'*col_w}{'  ' + '-'*host_w * len(variants)}\033[0m")

        for lbl in labels:
            row_results = {r.variant: r for r in rows if r.label == lbl}
            line = f"  {lbl:<{col_w}}"
            for v in variants:
                r = row_results.get(v)
                if r is None:
                    line += f"  {'—':<{host_w}}"
                    continue
                ok = "✓" if r.correct else "✗"
                if r.error:
                    cell = f"{ok} ERR"
                elif v == "builtin":
                    cell = f"{ok} {_tokens_est(r.chars_out)}t/{_ms(r.elapsed_ms)}"
                else:
                    saving = r.extra.get("saving_pct", 0)
                    cell = f"{ok} {_tokens_est(r.chars_out)}t/{_ms(r.elapsed_ms)}"
                    if saving > 0:
                        cell += f" -{saving:.0f}%"
                line += f"  {cell:<{host_w}}"
            print(line)

        # Per-tool summary
        print()
        builtin_rows = [r for r in rows if r.variant == "builtin"]
        atelier_rows = [r for r in rows if r.variant != "builtin"]
        total_b = sum(r.chars_out for r in builtin_rows)
        total_a_by_host: dict[str, int] = defaultdict(int)
        for r in atelier_rows:
            total_a_by_host[r.variant] += r.chars_out

        correct_by_host: dict[str, tuple[int, int]] = {}
        for v in variants:
            if v == "builtin":
                continue
            vrows = [r for r in rows if r.variant == v]
            correct_by_host[v] = (sum(1 for r in vrows if r.correct), len(vrows))

        if show_per_host and total_b > 0:
            print(f"  Baseline (builtin): {total_b:,} chars  {total_b//4:,} tokens")
            for v in [x for x in variants if x != "builtin"]:
                ta = total_a_by_host.get(v, 0)
                saving_pct = 100.0 * (1 - ta / total_b) if total_b > 0 else 0
                c, n = correct_by_host.get(v, (0, 0))
                avg_ms = (
                    sum(r.elapsed_ms for r in rows if r.variant == v) / max(1, len([r for r in rows if r.variant == v]))
                )
                print(
                    f"  \033[35m{v:<12}\033[0m: {ta:,} chars  "
                    f"\033[32m{saving_pct:.0f}%\033[0m fewer  "
                    f"correct={c}/{n}  avg={_ms(avg_ms)}"
                )


# ---------------------------------------------------------------------------
# Aggregate savings table
# ---------------------------------------------------------------------------
def print_savings_table(report: BenchReport) -> None:
    print(f"\n\033[1;35m{'='*76}\033[0m")
    print(f"\033[1;35m  SAVINGS & CORRECTNESS SUMMARY\033[0m")
    print(f"\033[1;35m{'='*76}\033[0m\n")

    variants = [v for v in HOSTS_ORDERED if v != "builtin" and any(r.variant == v for r in report.results)]
    tools = sorted({r.tool for r in report.results})

    # Header
    col_w = 10
    host_w = 28
    hdr_line = f"  {'Tool':<{col_w}}"
    for v in variants:
        hdr_line += f"  {v:<{host_w}}"
    print(f"\033[2m{hdr_line}\033[0m")
    print(f"\033[2m  {'─'*col_w}{'  ' + '─'*host_w * len(variants)}\033[0m")

    totals: dict[str, dict[str, Any]] = defaultdict(lambda: {"chars_saved": 0, "correct": 0, "total": 0, "ms": 0.0})

    for tool in tools:
        tool_rows = report.for_tool(tool)
        b_total = sum(r.chars_out for r in tool_rows if r.variant == "builtin")
        line = f"  {tool:<{col_w}}"
        for v in variants:
            v_rows = [r for r in tool_rows if r.variant == v]
            a_total = sum(r.chars_out for r in v_rows)
            correct = sum(1 for r in v_rows if r.correct)
            n = len(v_rows)
            saving_pct = 100.0 * (1 - a_total / max(b_total, 1)) if b_total > 0 else 0
            avg_ms = sum(r.elapsed_ms for r in v_rows) / max(n, 1)
            cell = (
                f"\033[32m{saving_pct:+.0f}%\033[0m chars  "
                f"{correct}/{n} correct  "
                f"avg {_ms(avg_ms)}"
            )
            line += f"  {cell:<{host_w + 20}}"

            totals[v]["chars_saved"] += b_total - a_total
            totals[v]["correct"] += correct
            totals[v]["total"] += n
            totals[v]["ms"] += avg_ms

        print(line)

    # Totals row
    print(f"\033[2m  {'─'*col_w}{'  ' + '─'*host_w * len(variants)}\033[0m")
    line = f"  {'TOTAL':<{col_w}}"
    for v in variants:
        t = totals[v]
        chars_s = t["chars_saved"]
        correct = t["correct"]
        total = t["total"]
        avg_ms = t["ms"] / max(len(tools), 1)
        cell = (
            f"\033[1;32m{chars_s//1000}k\033[0m chars saved  "
            f"\033[1m{correct}/{total}\033[0m correct  "
            f"avg {_ms(avg_ms)}"
        )
        line += f"  {cell:<{host_w + 20}}"
    print(line)

    # Overhead note
    b_avg = sum(r.elapsed_ms for r in report.results if r.variant == "builtin") / max(
        1, sum(1 for r in report.results if r.variant == "builtin")
    )
    a_avg = sum(r.elapsed_ms for r in report.results if r.variant != "builtin") / max(
        1, sum(1 for r in report.results if r.variant != "builtin")
    )
    overhead = a_avg - b_avg
    print(f"\n  \033[2mstdio spawn overhead: ~{_ms(overhead)} per call "
          f"(disappears in persistent in-process mode)\033[0m")


# ---------------------------------------------------------------------------
# Enforcement gap check
# ---------------------------------------------------------------------------
def print_enforcement_gap(settings_path: Path | None = None) -> None:
    import json as _json

    if settings_path is None:
        settings_path = Path.home() / ".claude" / "settings.json"

    # Project-level .claude/settings.json (no longer ships a deny list — enforcement
    # is now in the plugin agent frontmatter via disallowedTools).
    project_settings_path = Path(__file__).resolve().parents[3] / ".claude" / "settings.json"

    print(f"\n\033[1;35m{'='*76}\033[0m")
    print(f"\033[1;35m  TOOL ENFORCEMENT AUDIT\033[0m")
    print(f"\033[1;35m{'='*76}\033[0m\n")

    hook_map: dict[str, str] = {}
    denied: set[str] = set()

    def _load_settings(path: Path) -> dict:
        if not path.exists():
            return {}
        try:
            return _json.loads(path.read_text())
        except Exception:
            return {}

    global_cfg = _load_settings(settings_path)
    project_cfg = _load_settings(project_settings_path)

    # Collect PreToolUse hooks from global settings
    for hook in (global_cfg.get("hooks") or {}).get("PreToolUse") or []:
        m = hook.get("matcher", "")
        for h in hook.get("hooks") or []:
            hook_map[m] = h.get("command", "")

    # Collect denied tools from project settings (legacy path)
    for entry in (project_cfg.get("permissions") or {}).get("deny") or []:
        denied.add(str(entry).split("(")[0])  # strip args like Bash(*)

    # Collect denied tools from the installed plugin's agent.md frontmatter
    # (this is the wozcode/atelier-style enforcement)
    import re as _re
    for agent_path in (
        Path.home() / ".claude/plugins/cache/atelier/atelier/0.1.0/agents/code.md",
        Path.home() / ".atelier/claude-plugin-stable/agents/code.md",
    ):
        if agent_path.is_file():
            try:
                head = agent_path.read_text(encoding="utf-8").split("---", 2)
                fm = head[1] if len(head) >= 3 else ""
                m = _re.search(r'disallowedTools:\s*\[([^\]]*)\]', fm)
                if m:
                    for raw in m.group(1).split(","):
                        t = raw.strip().strip('"').strip("'")
                        if t:
                            denied.add(t)
                    break
            except Exception:
                pass

    # PostToolUse missed-savings hooks
    post_hooks: set[str] = set()
    for hook in (global_cfg.get("hooks") or {}).get("PostToolUse") or []:
        m = hook.get("matcher", "")
        cmds = [h.get("command", "") for h in (hook.get("hooks") or [])]
        if any("record_missed_saving" in c for c in cmds):
            for tool in m.split("|"):
                post_hooks.add(tool.strip())

    src_label = f"{settings_path}" + (
        f"\n  project: {project_settings_path}" if project_settings_path.exists() else ""
    )
    print(f"  \033[2mReading: global: {src_label}\033[0m\n")

    tools_need_redirect = {
        "Read": "mcp__atelier__read",
        "Grep": "mcp__atelier__search",
        "Glob": "mcp__atelier__search",
        "Edit": "mcp__atelier__edit",
        "Write": "mcp__atelier__edit",
        "Bash": "mcp__atelier__shell / mcp__atelier__search",
    }

    has_gap = False
    for native, preferred in tools_need_redirect.items():
        hooked = any(native in m or m in native for m in hook_map)
        is_denied = native in denied
        post_tracked = native in post_hooks

        if is_denied:
            print(f"  \033[32m✓ {native:<8}\033[0m \033[1mdenied\033[0m (hard block)")
        elif hooked:
            print(f"  \033[32m✓ {native:<8}\033[0m hooked → {preferred}")
        elif native == "Bash":
            print(f"  \033[2m~ {native:<8}\033[0m open by design — scoped allows + AGENTS.md → prefer {preferred}")
        else:
            print(f"  \033[33m⚠ {native:<8}\033[0m NOT enforced (relies on AGENTS.md only) → prefer {preferred}")
            has_gap = True

    if has_gap:
        print(
            "\n  \033[33mGap\033[0m: agents can fall back to builtin tools and bypass savings tracking.\n"
            "  Add permissions.deny in .claude/settings.json (project) for full enforcement."
        )
    else:
        print("\n  \033[32mAll core tools enforced.\033[0m Bash open by design (scoped allows in place).")


# ---------------------------------------------------------------------------
# Savings event check
# ---------------------------------------------------------------------------
def print_savings_events(atelier_root: Path | None = None) -> None:
    if atelier_root is None:
        atelier_root = Path.home() / ".atelier"

    eventsfile = atelier_root / "live_savings_events.jsonl"
    print(f"\n\033[1;35m{'='*76}\033[0m")
    print(f"\033[1;35m  LIVE SAVINGS EVENTS (last 5 from {eventsfile})\033[0m")
    print(f"\033[1;35m{'='*76}\033[0m\n")

    if not eventsfile.exists():
        print("  \033[33m⚠ No live_savings_events.jsonl found — atelier tools haven't been called in this session yet.\033[0m")
        return

    lines = eventsfile.read_text().splitlines()
    recent = lines[-5:]
    if not recent:
        print("  \033[33m⚠ File exists but is empty.\033[0m")
        return

    total_tokens = 0
    total_cost = 0.0
    total_calls = 0

    for line in recent:
        try:
            ev = json.loads(line)
            ts = ev.get("at", "")[:19]
            tool = ev.get("tool_name", ev.get("kind", "?"))
            lever = ev.get("lever", "?")
            tok = int(ev.get("tokens_saved") or ev.get("live_tokens_saved") or 0)
            cost = float(ev.get("cost_saved_usd") or 0)
            total_tokens += tok
            total_cost += cost
            total_calls += 1
            tok_str = f"{tok//1000}k" if tok >= 1000 else str(tok)
            print(f"  {ts}  \033[35m{tool:<20}\033[0m lever={lever:<20}  tok_saved={tok_str:<6}  cost_saved=${cost:.4f}")
        except Exception:
            print(f"  \033[2m{line[:80]}\033[0m")

    all_events = []
    for line in lines:
        try:
            all_events.append(json.loads(line))
        except Exception:
            pass

    total_all_tokens = sum(int(e.get("tokens_saved") or e.get("live_tokens_saved") or 0) for e in all_events)
    total_all_cost = sum(float(e.get("cost_saved_usd") or 0) for e in all_events)
    print(f"\n  Total ({len(all_events)} events): {total_all_tokens//1000}k tokens saved  ${total_all_cost:.4f} saved")


# ---------------------------------------------------------------------------
# Statusline preview
# ---------------------------------------------------------------------------
def print_statusline_preview(atelier_root: Path | None = None) -> None:
    from pathlib import Path as _Path
    import subprocess as _sp

    if atelier_root is None:
        atelier_root = _Path.home() / ".atelier"

    script = _Path(__file__).resolve().parents[3] / "integrations" / "claude" / "plugin" / "scripts" / "statusline.sh"
    print(f"\n\033[1;35m{'='*76}\033[0m")
    print(f"\033[1;35m  STATUSLINE PREVIEW\033[0m")
    print(f"\033[1;35m{'='*76}\033[0m\n")

    if not script.exists():
        print(f"  \033[33m⚠ statusline.sh not found at {script}\033[0m")
        return

    fake_input = json.dumps({
        "model": {"display_name": "claude-sonnet-4-5", "id": "claude-sonnet-4-5"},
        "context_window": {
            "used_percentage": 12.5,
            "current_usage": {
                "input_tokens": 8000,
                "output_tokens": 1200,
                "cache_read_input_tokens": 5000,
                "cache_creation_input_tokens": 200,
            },
        },
        "cost": {"total_cost_usd": 0.0423, "total_duration_ms": 95000},
        "session_id": "bench-preview",
    })
    env = {
        **os.environ,
        "ATELIER_ROOT": str(atelier_root),
        "ATELIER_STATUS_SESSION_ID": "bench-preview",
    }
    try:
        r = _sp.run(
            ["bash", str(script)],
            input=fake_input, capture_output=True, text=True, timeout=10, env=env,
        )
        out = r.stdout.strip()
        err = r.stderr.strip()
        if out:
            print(f"  {out}")
        if err:
            print(f"  \033[2mstderr: {err[:200]}\033[0m")
    except Exception as exc:
        print(f"  \033[31mError running statusline: {exc}\033[0m")


# ---------------------------------------------------------------------------
# JSON export
# ---------------------------------------------------------------------------
def export_json(report: BenchReport, path: Path) -> None:
    data = {
        "generated_at": datetime.now(UTC).isoformat(),
        "results": [
            {
                **asdict(r),
            }
            for r in report.results
        ],
    }
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n  Report saved → {path}")
