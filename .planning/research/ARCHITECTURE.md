# Architecture Patterns: Reproducible A/B Benchmark Runner

**Domain:** Reproducible AI agent benchmarking system (Atelier public benchmarks)
**Researched:** 2026-05-28
**Confidence:** HIGH — based on direct codebase inspection

---

## 1. ATELIER_BENCH_MODE: Clean Toggle Without Production Pollution

### The Core Problem

The mode toggle must be zero-cost in production: no import-time side effects, no extra
conditionals in hot paths, no test-only shim leakage. The pattern that achieves this is a
**module-level singleton** read once at process start, with capability shims registered
in one place.

### Recommended Pattern: Bootstrap Module + Protocol Shims

**New file: `src/atelier/bench/mode.py`**

```python
"""Bench-mode singleton. Read once at startup; never re-read mid-process."""
from __future__ import annotations
import os
from enum import Enum

class BenchMode(str, Enum):
    ON  = "on"
    OFF = "off"
    UNSET = "unset"          # production: Atelier behaves normally

_BENCH_MODE: BenchMode | None = None  # set by bootstrap()

def bootstrap() -> BenchMode:
    """Call this exactly once, early in process startup."""
    global _BENCH_MODE
    raw = os.environ.get("ATELIER_BENCH_MODE", "").strip().lower()
    _BENCH_MODE = BenchMode(raw) if raw in BenchMode._value2member_map_ else BenchMode.UNSET
    return _BENCH_MODE

def get() -> BenchMode:
    if _BENCH_MODE is None:
        return BenchMode.UNSET   # safe default if bootstrap() was never called
    return _BENCH_MODE

def is_off() -> bool:
    return get() is BenchMode.OFF
```

**Touch points — one guard per capability, not scattered inline:**

| File | Guard pattern |
|------|--------------|
| `src/atelier/core/capabilities/cross_vendor_routing/router.py` | `if bench.is_off(): return PassthroughRoute(requested_model)` |
| `src/atelier/core/capabilities/context_compression/capability.py` | `if bench.is_off(): return CompressionResult.passthrough(ledger)` |
| `src/atelier/core/capabilities/cross_vendor_memory/*.py` adapters | `if bench.is_off(): return []` from `list()` / `read()` |
| `src/atelier/gateway/adapters/mcp_server.py` | Skip `@mcp_tool` registration for Atelier tools when bench is off (see below) |
| `src/atelier/gateway/cli/app.py` → `main()` | Call `bench.bootstrap()` as very first line |

**MCP tool gating pattern** (in `mcp_server.py`):

The `@mcp_tool` decorator fires at import time, so conditional registration must happen
before module-level tool functions are defined. The cleanest approach: read the mode at
module top and conditionally define (or stub) each tool:

```python
# mcp_server.py top
from atelier.bench import mode as _bench_mode
_BENCH_OFF = _bench_mode.is_off()   # evaluated once at import

# Then each tool:
if not _BENCH_OFF:
    @mcp_tool(name="route")
    def tool_route(...):
        ...
```

This is readable, doesn't pollute tool implementations, and the MCP server simply
advertises fewer tools when `ATELIER_BENCH_MODE=off`.

**Why NOT scattered `if bench.is_off()` inline?**

Inline guards:
- Are invisible to readers of each capability module
- Accumulate technical debt as guards multiply
- Can silently slip into code paths that shouldn't check mode

A single passthrough return at the top of each capability's main `process()`/`route()`
method keeps capability logic clean and bench logic auditable.

**Bootstrap call site:**

```python
# src/atelier/gateway/cli/app.py — main()
def main() -> None:
    from atelier.bench import mode as bench_mode
    bench_mode.bootstrap()
    if bench_mode.is_off():
        import logging
        logging.getLogger(__name__).info("ATELIER_BENCH_MODE=off — routing/compaction/memory disabled")
    cli()
```

For the MCP server entrypoint (`mcp_server.py:main()`), bootstrap must happen before the
module's tool registrations execute, so add it to the `__init__` / top of module before
the `@mcp_tool` decorators fire. Because `@mcp_tool` decorators run at import time,
`bootstrap()` must be called via the process entry point (the `main()` for
`atelier-mcp`) before the module is imported. Use a `__main__.py` or entry wrapper to
ensure ordering.

---

## 2. Resumable Runner: Checkpoint Pattern

### The Core Problem

A full 10-task × 2-mode × 5-rep sweep = 100 agent invocations, each costing real money.
A kill mid-run must not lose completed work; a resume must not re-bill for done cells.

### Recommended Pattern: File-per-Cell + Completion Index

**Cell identity:** `(suite, task_id, mode, rep)` → hashed to a stable filename:

```
bench/runs/<run-id>/raw/<task_id>__<mode>__rep<N>.json
```

The presence of this file **is** the checkpoint. No database, no lock file.

**Resumption logic in `benchmarks/ab/runner.py`:**

```python
class ABRunner:
    def run(self, config: RunConfig) -> None:
        run_dir = config.out_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "config.json").write_text(config.to_json())   # idempotent

        cells = list(config.cells())          # sorted, deterministic
        for cell in cells:
            out_path = run_dir / "raw" / cell.filename()
            if out_path.exists():
                _log.info("skip (done): %s", cell)
                continue                      # RESUME: skip completed
            result = self._run_cell(cell)
            out_path.parent.mkdir(exist_ok=True)
            out_path.write_text(result.to_json())   # atomic at OS level
        _aggregate(run_dir)                   # recompute summary.json from raw/*
```

**Why file presence over a separate index?** Because the index can get out of sync with
reality if the process is killed between writing raw data and updating the index. The raw
file is the single source of truth — if it's there, the cell ran. This is the same
pattern used by pytest-cache, mypy's incremental cache, and most CI artefact systems.

**Atomic write guarantee:** Write to `<path>.tmp`, then `os.replace(tmp, final)`.
`os.replace` is POSIX-atomic; `Path.write_text()` is NOT (partial writes possible on
kill). Use the atomic pattern for all raw cell files.

```python
import os, tempfile

def atomic_write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, path)
```

**Parallel execution consideration:** If future work runs cells in parallel, add a
`.lock` sentinel file per cell: create before, delete after. Presence of `.lock` +
absence of `.json` = interrupted mid-cell. Current design is serial; the lock pattern is
the migration path.

---

## 3. Output Directory Structure

```
bench/
└── runs/
    └── <run-id>/               # e.g. 20260604T140000-terminalbench-claude-sonnet-42
        ├── config.json          # full RunConfig: suite, tasks, modes, N, seed, model, commit_sha
        ├── raw/
        │   ├── task001__on__rep1.json
        │   ├── task001__on__rep2.json
        │   ├── task001__off__rep1.json
        │   ├── task001__off__rep2.json
        │   └── ...              # 100 files for 10t × 2m × 5r
        ├── summary.json         # aggregated: mean/CI per (task, mode) cell
        ├── plots/
        │   ├── cost_delta.png
        │   ├── latency_delta.png
        │   └── quality_delta.png
        └── report.md            # generated by benchmarks/ab/report.py
```

**`<run-id>` generation:** `{date}T{time}-{suite}-{model}-{seed}`. This is both
human-readable and sortable. Generate at `atelier bench run` invocation, store in
`config.json`, so `--run-id` flag re-uses an existing directory.

**`config.json` schema (captures full reproducibility context):**

```json
{
  "run_id": "20260604T140000-terminalbench-claude-sonnet-42",
  "suite": "terminalbench",
  "tasks": ["tb001", "tb002", ...],
  "modes": ["on", "off"],
  "n_reps": 5,
  "seed": 42,
  "model": "claude-sonnet-4-5",
  "commit_sha": "abc1234",
  "atelier_version": "0.2.0",
  "started_at": "2026-06-04T14:00:00Z",
  "cli_command": "atelier bench run --suite terminalbench --full --seed 42 --yes",
  "cost_cap_usd": 50.0
}
```

**`summary.json` schema:**

```json
{
  "run_id": "...",
  "cells": {
    "tb001": {
      "on":  {"mean_cost_usd": 0.082, "mean_latency_s": 42.1, "pass_rate": 0.8, "ci_lower": 0.44, "ci_upper": 0.97, "n": 5},
      "off": {"mean_cost_usd": 0.134, "mean_latency_s": 67.3, "pass_rate": 0.6, "ci_lower": 0.26, "ci_upper": 0.88, "n": 5}
    }
  },
  "losses": [
    {"task": "tb004", "metric": "quality", "on_value": 0.4, "off_value": 0.8, "delta": -0.4}
  ],
  "generated_at": "2026-06-04T16:22:00Z"
}
```

**`.gitignore` for bench/ directory:**

```gitignore
# bench/runs/ contains raw API output — expensive to reproduce, not source code
bench/runs/*/raw/
bench/runs/*/plots/
# Keep config.json and summary.json (small, useful for audit)
!bench/runs/*/config.json
!bench/runs/*/summary.json
```

---

## 4. BenchCase/CaseResult Evolution for End-to-End Runs

### Current State

`benchmarks/mcp_tools/harness.py` has:
- `BenchCase`: describes a **single MCP tool call** (op, args, assert_keys, baseline_tokens)
- `CaseResult`: records tool response, token counts, pass/fail for one tool invocation
- `run_case(case, tool_fn)`: synchronous, in-process, calls `tool_fn(case.args)`

This is perfect for tool-level microbenchmarks. It is the **wrong abstraction** for
end-to-end agent runs.

### Recommended Pattern: New ABCase / AgentRunResult Layer

**New file: `benchmarks/ab/schema.py`**

```python
@dataclass
class AgentRunCase:
    """One cell in the A/B matrix: a task × mode × replication triple."""
    suite: str                    # "terminalbench", "long_session", "pr_replay"
    task_id: str                  # e.g. "tb001", "pr_github.com/user/repo/pull/123"
    task_prompt: str              # the exact prompt sent to the agent
    mode: Literal["on", "off"]
    rep: int                      # replication index (1-based)
    seed: int
    model: str
    harness_config: dict          # suite-specific config (e.g. TerminalBench task def)

    def cell_id(self) -> str:
        return f"{self.task_id}__{self.mode}__rep{self.rep}"

    def filename(self) -> str:
        return f"{self.cell_id()}.json"


@dataclass
class AgentRunResult:
    """Result of one end-to-end agent invocation."""
    case: AgentRunCase
    # Raw transcript (full stdout/stderr of agent subprocess)
    transcript: str
    # API-reported token counts (from response headers / usage block)
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    # Wall-clock
    latency_s: float
    # Cost in USD (computed from model pricing)
    cost_usd: float
    # Grader output
    grader_verdict: Literal["pass", "fail", "error"]
    grader_score: float           # 0.0–1.0; for pass/fail = 0 or 1
    grader_notes: str
    # Exit status
    exit_code: int
    passed: bool
    failure: str                  # empty string on pass
    # Metadata
    started_at: str               # ISO8601
    bench_mode_tag: str           # "bench_mode=on" or "bench_mode=off"
```

**Relationship to existing harness.py:**

- Do **not** modify `harness.py`. It is the right tool for tool-level benchmarks and
  should stay that way.
- Share only `tiktoken` utilities via a new `benchmarks/ab/token_utils.py` that imports
  from the same `cl100k_base` encoding.
- `AgentRunCase`/`AgentRunResult` live entirely in `benchmarks/ab/` and have no import
  dependency on `benchmarks/mcp_tools/harness.py`.

The key semantic difference:
- `BenchCase.tool_fn` is a synchronous in-process call → result in milliseconds
- `AgentRunCase` wraps a **subprocess** (the agent CLI) → result in seconds to minutes,
  with rich structured output requiring its own aggregation logic

---

## 5. Agent Subprocess Adapter

**New file: `benchmarks/terminalbench/agent_adapter.py`**

The agent runs as a subprocess matching the existing `benchmarks/swe/atelier_proxy.py`
pattern. This is the right call for three reasons:

1. More externally credible: "we ran `claude -p` exactly as a developer would"
2. Isolates bench infrastructure from agent internals
3. Bench-mode env var naturally propagates to the subprocess

```python
@dataclass
class AgentInvocation:
    """Parameters for one agent subprocess invocation."""
    model: str
    prompt: str
    bench_mode: Literal["on", "off"]
    seed: int
    cwd: Path                   # task working directory (isolated per run)
    timeout_s: float = 600.0

def run_agent(inv: AgentInvocation) -> AgentRunResult:
    env = os.environ.copy()
    env["ATELIER_BENCH_MODE"] = inv.bench_mode
    env["ANTHROPIC_API_KEY"] = env.get("ANTHROPIC_API_KEY", "")
    # Seed propagates as claude-p doesn't expose temperature directly;
    # use prompt-level seed injection for TerminalBench tasks
    cmd = [
        "claude",
        "--model", inv.model,
        "-p", inv.prompt,
        "--output-format", "json",     # structured output with usage stats
        "--max-turns", "30",
    ]
    t0 = time.perf_counter()
    result = subprocess.run(
        cmd,
        env=env,
        cwd=inv.cwd,
        capture_output=True,
        text=True,
        timeout=inv.timeout_s,
    )
    elapsed = time.perf_counter() - t0
    # Parse structured output for token counts
    return _parse_claude_output(result, elapsed, inv)
```

**`--output-format json` on `claude -p`** gives structured usage blocks. This is the
same approach used in the existing `swe/atelier_proxy.py` and is HIGH confidence that
it's the right interface.

**Task isolation:** Each cell runs in its own temporary working directory, cloned from a
clean task snapshot. This prevents cross-contamination between reps. For TerminalBench,
the harness manages the working dir; for PR replay, we use `git worktree`.

---

## 6. PR Replay Architecture

**New file: `benchmarks/ab/suites/pr_replay.py`**

```
PR URL → fetch PR metadata → git worktree at base commit → run both arms → diff scoring
```

### Sequence

```
1.  gh pr view <url> --json baseRefOid,number,files
2.  git worktree add bench/tmp/<run-id>/on  <base_oid>
3.  git worktree add bench/tmp/<run-id>/off <base_oid>
4.  Construct prompt from PR title + body + changed files context
5.  Run agent arm "on"  in worktree bench/tmp/<run-id>/on
6.  Run agent arm "off" in worktree bench/tmp/<run-id>/off
7.  Capture diffs: git diff HEAD...<base_oid> in each worktree
8.  Score diffs against real PR merge: edit_similarity(agent_diff, real_diff)
9.  Clean up worktrees
```

### Diff Scoring

Use `diff-match-patch` (already in `pyproject.toml` deps) for character-level similarity
between the agent-produced diff and the real merged diff. Score = Levenshtein ratio of
the two unified diffs, trimmed of file headers.

```python
from diff_match_patch import diff_match_patch

def score_diff(agent_diff: str, ground_truth_diff: str) -> float:
    dmp = diff_match_patch()
    diffs = dmp.diff_main(ground_truth_diff, agent_diff)
    dmp.diff_cleanupSemantic(diffs)
    # Levenshtein distance normalized by ground truth length
    distance = dmp.diff_levenshtein(diffs)
    max_len = max(len(ground_truth_diff), 1)
    return max(0.0, 1.0 - distance / max_len)
```

The grader verdict: `pass` if score ≥ 0.7, `fail` if < 0.7 (threshold configurable in
`config.json`). This is the same `grader_score` field used in TerminalBench runs, so the
aggregator and reporter work identically for both suites.

**Worktree cleanup:** Use `git worktree remove --force` in a `try/finally` block.
Register a `signal.SIGTERM` handler to clean up if killed. Do NOT leave worktrees
dangling — they consume disk and confuse git status.

---

## 7. Publication Pipeline Shape

### The Problem with `publisher.py`

The existing `src/atelier/infra/benchmarks/publisher.py`:
- Reads from `benchmarks/savings/*_latest.json` (internal weekly snapshots)
- Produces `reports/<week-label>/benchmark.md` (internal weekly format)
- Has no concept of transcript bundles, reproduce.sh, or external audiences

**Do not extend publisher.py.** Build a separate `external_publisher.py` with a
completely different output contract.

### New file: `src/atelier/infra/benchmarks/external_publisher.py`

```python
def publish_run(
    run_id: str,
    *,
    run_dir: Path,               # bench/runs/<run-id>/
    out_dir: Path,               # docs-site/blog/<slug>/
    slug: str,                   # e.g. "2026-06-04-terminalbench-claude-sonnet"
    post_meta: PostMeta,         # title, authors, tags
) -> None:
    # 1. Copy plots/
    shutil.copytree(run_dir / "plots", out_dir / "plots", dirs_exist_ok=True)
    # 2. Copy transcripts (raw/*.json) as Markdown summaries
    _copy_transcripts(run_dir / "raw", out_dir / "transcripts")
    # 3. Render index.md from Jinja2 template
    _render_post(run_dir / "summary.json", out_dir / "index.md", post_meta)
    # 4. Emit reproduce.sh
    _emit_reproduce_sh(run_dir / "config.json", out_dir / "reproduce.sh")
```

**Output layout:**

```
docs-site/blog/2026-06-04-terminalbench-claude-sonnet/
├── index.md                  # Docusaurus blog post
├── plots/
│   ├── cost_delta.png
│   ├── latency_delta.png
│   └── quality_delta.png
├── transcripts/
│   ├── tb001__on__rep1.md    # rendered from raw JSON (human-readable)
│   └── ...
└── reproduce.sh              # chmod +x; runs the exact same benchmark
```

**`reproduce.sh` shape:**

```bash
#!/usr/bin/env bash
# Reproduces: 2026-06-04-terminalbench-claude-sonnet
# Commit: abc1234
# Atelier version: 0.2.0
set -e
echo "Estimated cost: ~$8–12 USD (10 tasks × 5 reps × 2 modes)"
read -p "Continue? [y/N] " ans && [[ "$ans" == "y" ]] || exit 0
git checkout abc1234
atelier bench run \
  --suite terminalbench \
  --full \
  --seed 42 \
  --model claude-sonnet-4-5 \
  --run-id 20260604T140000-terminalbench-claude-sonnet-42 \
  --yes
atelier bench publish 20260604T140000-terminalbench-claude-sonnet-42 \
  --out docs-site/blog/2026-06-04-terminalbench-claude-sonnet/
```

### Docusaurus Blog Enablement

**Current state:** `docs-site/docusaurus.config.ts` has `blog: false`.

**Required change** (D5 task):

```typescript
// docusaurus.config.ts
blog: {
  showReadingTime: true,
  blogSidebarCount: 10,
  blogSidebarTitle: 'Benchmark Reports',
  postsPerPage: 10,
  routeBasePath: 'blog',
  path: './blog',
},
```

And add to navbar:
```typescript
{ to: '/blog', label: 'Benchmarks', position: 'left' }
```

**Docusaurus blog post front-matter template:**

```markdown
---
slug: 2026-06-04-terminalbench-claude-sonnet
title: "TerminalBench × Claude Sonnet: Atelier-on vs Atelier-off (N=5)"
date: 2026-06-04
authors: [atelier]
tags: [benchmark, atelier-vs-baseline, claude-sonnet, terminalbench]
---
```

---

## 8. `atelier bench` CLI Subcommand

### Where It Lives

The existing CLI is a single large `app.py` (9199 lines) with `@cli.group(...)` and
`@cli.command(...)` patterns using Click. The established pattern for sub-groups is
`@cli.group("name")` in `app.py`.

**Recommended:** Create a new module `src/atelier/gateway/cli/bench_commands.py` and
register it as a group in `app.py`. This keeps `app.py` from growing further while
following existing conventions.

**New file: `src/atelier/gateway/cli/bench_commands.py`**

```python
"""atelier bench — reproducible A/B benchmark runner."""
import click
from pathlib import Path


@click.group("bench")
def bench_group() -> None:
    """Run and publish reproducible Atelier A/B benchmarks."""


@bench_group.command("run")
@click.option("--suite", default="terminalbench",
              type=click.Choice(["terminalbench", "long_session", "pr_replay"]),
              show_default=True)
@click.option("--quick", "preset", flag_value="quick",
              help="1 task, N=2. Runs in <5 min.")
@click.option("--full", "preset", flag_value="full", default=True,
              help="10 tasks, N=5.")
@click.option("--tasks", default=None, help="Comma-separated task IDs (overrides preset).")
@click.option("--n", default=None, type=int, help="Replications per cell (overrides preset).")
@click.option("--models", default="claude-sonnet-4-5", show_default=True)
@click.option("--seed", default=42, show_default=True, type=int)
@click.option("--run-id", default=None, help="Resume or re-use an existing run directory.")
@click.option("--out", default=None, type=click.Path(path_type=Path),
              help="Output dir. Default: ~/.atelier/bench/<run-id>/")
@click.option("--pr", default=None, help="GitHub PR URL for pr_replay suite.")
@click.option("--cost-cap", default=50.0, show_default=True, type=float,
              help="Hard stop if estimated cost exceeds this. Use 0 to disable.")
@click.option("--yes", is_flag=True, help="Skip cost confirmation prompt.")
def bench_run(suite, preset, tasks, n, models, seed, run_id, out, pr, cost_cap, yes):
    """Run an A/B benchmark sweep."""
    from atelier.bench.runner import run_sweep
    run_sweep(...)


@bench_group.command("publish")
@click.argument("run_id")
@click.option("--out", required=True, type=click.Path(path_type=Path),
              help="Output directory, e.g. docs-site/blog/2026-06-04-terminalbench-sonnet/")
@click.option("--slug", default=None, help="Blog slug (default: derived from run-id).")
def bench_publish(run_id, out, slug):
    """Assemble a self-contained blog post from a completed run."""
    from atelier.infra.benchmarks.external_publisher import publish_run
    ...
```

**Registration in `app.py`** (one line addition):

```python
# In app.py, near the other @cli.group registrations
from atelier.gateway.cli.bench_commands import bench_group
cli.add_command(bench_group)
```

**User-facing storage:** `~/.atelier/bench/<run-id>/` for runs started from the CLI.
For developer convenience, allow `--out ./bench/runs/<run-id>/` to use the repo-local
directory for the "official" published runs.

---

## 9. Component Map: How the Pieces Connect

```
ATELIER_BENCH_MODE env var
       │
       ▼
src/atelier/bench/mode.py  ─── bootstrap() called by main() ──→  mode singleton
       │
       ├── cross_vendor_routing/router.py   (passthrough shim)
       ├── context_compression/capability.py (passthrough shim)
       ├── cross_vendor_memory/*.py          (empty-list shim)
       └── gateway/adapters/mcp_server.py    (skip @mcp_tool decorators)

benchmarks/ab/
├── schema.py              AgentRunCase, AgentRunResult
├── runner.py              ABRunner.run() → file-per-cell checkpoint
├── aggregate.py           summarize raw/ → summary.json (Wilson CI)
├── report.py              summary.json → plots/ + report.md
├── token_utils.py         shared tiktoken helpers
└── suites/
    ├── terminalbench.py   loads tasks.yaml, calls agent_adapter
    ├── long_session.py    multi-turn tasks with recall grader
    └── pr_replay.py       git worktree + diff scoring

benchmarks/terminalbench/
├── agent_adapter.py       subprocess wrapper for claude -p
├── grader.py              TerminalBench pass/fail grader
└── tasks.yaml             pinned 10-task subset

src/atelier/infra/benchmarks/
├── publisher.py           (existing) internal weekly snapshots — DO NOT MODIFY
├── external_publisher.py  (new) blog post assembly pipeline
└── templates/
    └── post.md.j2         Jinja2 blog post template

src/atelier/gateway/cli/
├── app.py                 (existing) add bench_group registration
└── bench_commands.py      (new) atelier bench run / publish subcommands

docs-site/blog/
└── <slug>/                output of external_publisher.py
    ├── index.md
    ├── plots/
    ├── transcripts/
    └── reproduce.sh
```

---

## 10. Wilson Score CI for Pass-Rate

The plan calls this out explicitly. Use Wilson score, not normal approximation.
This is correct and MEDIUM confidence (standard stats, not novel):

```python
from math import sqrt

def wilson_ci(n_successes: int, n_trials: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% CI for a binary pass-rate."""
    if n_trials == 0:
        return (0.0, 1.0)
    p = n_successes / n_trials
    denominator = 1 + z**2 / n_trials
    centre = (p + z**2 / (2 * n_trials)) / denominator
    margin = z * sqrt(p * (1 - p) / n_trials + z**2 / (4 * n_trials**2)) / denominator
    return (max(0.0, centre - margin), min(1.0, centre + margin))
```

For cost and latency (continuous), use bootstrap CI or t-CI at N=5. Bootstrap is
more robust with small N and non-normal distributions (LLM latency is right-skewed).

---

## 11. Anti-Patterns to Avoid

### Anti-Pattern 1: Checking `ATELIER_BENCH_MODE` inline in hot paths

```python
# BAD: os.environ.get() in every router call
def route(self, ...):
    if os.environ.get("ATELIER_BENCH_MODE") == "off":
        return passthrough
```

**Why bad:** `os.environ.get()` is not free under hot-path conditions; more importantly,
this spreads mode logic across the codebase and makes it hard to audit.

**Instead:** Read once in `bench/mode.py:bootstrap()`, call `bench.is_off()` (a simple
attribute lookup) at the top of the capability's main entry method only.

### Anti-Pattern 2: Accumulating raw transcripts in memory during a sweep

**Why bad:** 100 agent runs × multi-MB transcripts each = OOM in long sweeps.

**Instead:** Write each `AgentRunResult` to disk immediately after the subprocess
returns. `aggregate.py` reads them back one at a time for summarization.

### Anti-Pattern 3: Vendoring TerminalBench into the repo

**Why bad:** Defeats reproducibility — a "stranger cloning the repo" gets the vendored
snapshot, not the pinned-but-updateable dep. Changes to TerminalBench tasks won't be
pulled.

**Instead:** Use a pinned git submodule (`git submodule add <url> benchmarks/external/terminalbench
--ref v0.x.y`) or a PyPI dep. A submodule with a pinned ref is easier to inspect and
`reproduce.sh`-friendly.

### Anti-Pattern 4: Publishing from a dirty working tree

**Why bad:** `config.json` records `commit_sha = "abc1234dirty"` — the `reproduce.sh`
clone at that SHA won't match what actually ran.

**Instead:** `atelier bench run` should call `git status --porcelain` and refuse to start
(or warn prominently) if the working tree is dirty, unless `--allow-dirty` is passed.

### Anti-Pattern 5: Single `bench` top-level group in app.py (9199 lines)

**Why bad:** `app.py` is already 9k lines; adding another 200-line command group inline
makes navigation worse.

**Instead:** `bench_commands.py` module with `cli.add_command(bench_group)` in `app.py`.
The existing pattern shows `@cli.group("worker")`, `@cli.group("servicectl")`, etc. — all
inline. The bench group is large enough to justify extraction.

---

## 12. Sequencing / Dependency Graph (Architecture View)

```
D1: bench/mode.py + shims
    (standalone; unblocks all subprocess work)
           │
    ┌──────┴──────────────────────────┐
    ▼                                 ▼
D2: benchmarks/terminalbench/     benchmarks/ab/schema.py
    agent_adapter.py                  (data contracts)
           │                               │
           └──────────┬────────────────────┘
                      ▼
               D3: benchmarks/ab/runner.py
               (ABRunner + checkpoint pattern)
                      │
               D3: benchmarks/ab/aggregate.py
               (summary.json + Wilson CI)
                      │
               D4: benchmarks/ab/report.py
               (plots + report.md)
                      │
               D5: external_publisher.py
               + bench_commands.py (publish)
               + docusaurus blog: true
                      │
               D7: bench_commands.py (run)
               (wraps D3 with CLI UX, cost cap, --quick)
```

D6 (long_session) slots in parallel with D7 — it's a new suite that reuses D3 runner
infrastructure unchanged.

---

## Sources and Confidence

| Claim | Source | Confidence |
|-------|--------|------------|
| Click `@cli.group` + `cli.add_command` pattern | Inspected `app.py` directly | HIGH |
| `atelier = "atelier.gateway.cli:main"` entry point | `pyproject.toml` lines 85-87 | HIGH |
| `blog: false` in docusaurus config | `docs-site/docusaurus.config.ts` line 32 | HIGH |
| `diff-match-patch` in deps | `pyproject.toml` | HIGH |
| `@mcp_tool` decorator at import time | `mcp_server.py` lines 93-120 | HIGH |
| File-per-cell checkpoint pattern | Standard practice (pytest-cache, mypy cache) | HIGH |
| Wilson score CI for binary metrics | Statistics literature | HIGH |
| `os.replace()` atomic writes on POSIX | POSIX spec, Python docs | HIGH |
| TerminalBench available as submodule/PyPI | Plan doc assumption — **needs verification** | LOW |
| `claude -p --output-format json` usage block | Assumed from SWE proxy pattern; verify with `claude --help` | MEDIUM |
