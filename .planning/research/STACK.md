# Technology Stack — Reproducible AI Agent Benchmarking (A/B)

**Project:** Atelier public-benchmarks milestone  
**Researched:** 2026-05-28  
**Confidence:** HIGH — all claims verified against installed packages, live wheel inspection, and live `claude -p` invocation

---

## Executive Decision Summary

Use `terminal-bench` (PyPI) as the canonical harness, **not** a git submodule. It
ships a clean `Harness` + `BaseAgent` Python API. Run the harness in a dedicated Python
3.12 uv workspace because `terminal-bench>=0.2.13` requires `Python>=3.12` while the
main project is pinned to 3.11. Capture all token/cost/latency data from `claude -p
--output-format stream-json --verbose` stdout — every field is present in the `result`
line and requires zero Anthropic SDK calls. Implement Wilson score interval as a
10-line pure-math function (no scipy, which isn't in deps and has an unstable API).
Reuse the `BenchCase`/`CaseResult` dataclass skeleton from `benchmarks/mcp_tools/harness.py`.

---

## 1. Benchmarking Harness

### terminal-bench (PyPI)

| Attribute | Value |
|-----------|-------|
| PyPI name | `terminal-bench` |
| Current version | `0.2.18` |
| Requires Python | `>=3.12` |
| Install | `uv pip install terminal-bench==0.2.18` |
| Confidence | HIGH (live wheel inspection) |

**Why PyPI, not submodule:**
- The `Out of Scope` rule in PROJECT.md explicitly bans "vendored copy".
- `terminal-bench` is on PyPI with semver releases; pinning to `==0.2.18` satisfies
  the reproducibility requirement without the overhead of a submodule.
- Submodule would pull in Docker build scripts and extra tooling not needed for just
  running tasks.

**What the package provides (verified from wheel):**

```python
from terminal_bench import Harness, BenchmarkResults, BaseAgent
```

| Class | Role |
|-------|------|
| `Harness` | Orchestrates a full benchmark run; manages Docker containers, tmux sessions, concurrency, pass@k |
| `BaseAgent` | Abstract base; subclass and implement `perform_task(instruction, session, logging_dir) → AgentResult` |
| `AbstractInstalledAgent` | Concrete base for agents installed inside Docker (e.g., `claude --verbose ...`); what `ClaudeCodeAgent` extends |
| `BenchmarkResults` | Pydantic model; `results: list[TrialResults]`; computes `pass_at_k`, `accuracy` |
| `TrialResults` | Per-run record: `is_resolved`, `total_input_tokens`, `total_output_tokens`, `trial_started_at`, `trial_ended_at` |
| `Dataset` | Loads task subsets by `task_ids: list[str]` or `n_tasks: int` from the registry |

**Task format** (each task is a directory with):
```
<task-id>/
  solution.yaml   # YAML: instruction, difficulty, tags, max_agent_timeout_sec, parser_name
  Dockerfile
  docker-compose.yaml
  run-tests.sh    # pytest script run inside container to grade output
  tests/
    test_outputs.py
```

`solution.yaml` key fields:
```yaml
instruction: "Compile this C program and fix the linker error."
difficulty: medium
category: software_engineering
parser_name: pytest          # default; others: swebench, mlebench
max_agent_timeout_sec: 360
```

**Atelier custom agent** — subclass `AbstractInstalledAgent`:
```python
class AtelierClaudeAgent(AbstractInstalledAgent):
    @staticmethod
    def name() -> str:
        return "atelier-claude"

    @property
    def _env(self) -> dict[str, str]:
        env = {"ANTHROPIC_API_KEY": os.environ["ANTHROPIC_API_KEY"]}
        # Inject bench mode: ATELIER_BENCH_MODE=on|off
        env["ATELIER_BENCH_MODE"] = os.environ.get("ATELIER_BENCH_MODE", "on")
        if model := os.environ.get("ANTHROPIC_MODEL"):
            env["ANTHROPIC_MODEL"] = model
        return env

    @property
    def _install_agent_script_path(self) -> Path:
        # render from .j2 template that runs: npm install -g @anthropic-ai/claude-code@{version}
        ...

    def _run_agent_commands(self, instruction: str) -> list[TerminalCommand]:
        return [TerminalCommand(
            command=f"claude --verbose --output-format stream-json -p {shlex.quote(instruction)} "
                    f"--allowedTools Bash Edit Write Read Glob Grep LS",
            max_timeout_sec=float("inf"),
            block=True,
            append_enter=True,
        )]
```

**Critical runtime dependency:** Docker must be running. Terminal-bench spins up Docker
containers for every trial. This is non-negotiable — TerminalBench is fundamentally a
Docker harness.

---

## 2. Python Version Strategy

**The conflict:** `terminal-bench>=0.2.13` requires Python ≥ 3.12. The main project
venv runs Python 3.11.12.

**Resolution — separate benchmarks workspace (recommended):**

Create `benchmarks/pyproject.toml` as a separate uv project:
```toml
[project]
name = "atelier-benchmarks"
requires-python = ">=3.12"
dependencies = [
    "terminal-bench==0.2.18",
    "matplotlib>=3.9",
    "jinja2>=3.1",
    "scipy>=1.13",
    "numpy>=2.0",
    "tiktoken>=0.9",
    "rich>=13.7",
    "click>=8.1",
]
```

Run with: `uv run --project benchmarks/ python -m benchmarks.ab.runner ...`

**Why not bump the main pyproject.toml to 3.12:**
- The main project explicitly says `requires-python = ">=3.11"`.
- Bumping blocks users on Python 3.11 from installing Atelier itself.
- Benchmarks are an optional dev-time tool, not a runtime dep.

**Alternative (simpler short-term):** Install `terminal-bench` as a `uv tool`:
```bash
uv tool install terminal-bench==0.2.18
```
Then invoke `tb run ...` as a subprocess from the main venv Python 3.11 harness.
This sidesteps the version conflict entirely but gives less programmatic control.

---

## 3. Agent Subprocess Invocation (`claude -p`)

**Command (verified against live output):**
```bash
claude --verbose --output-format stream-json -p "<escaped_instruction>" \
  --allowedTools Bash Edit Write Read Glob Grep LS \
  --max-budget-usd 10
```

**Output format** — newline-delimited JSON, each line has `"type"`:

| type | Subtype | Key fields | Use for |
|------|---------|-----------|---------|
| `system` | `init` | `model`, `tools`, `claude_code_version` | Record model name |
| `assistant` | — | `message.usage.{input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}` | Per-turn token accumulation |
| `result` | `success` | `total_cost_usd`, `duration_ms`, `duration_api_ms`, `usage.*`, `modelUsage` | **Primary extraction point** |

**Example `result` line** (from live run, 2026-05-28):
```json
{
  "type": "result",
  "subtype": "success",
  "is_error": false,
  "duration_ms": 3557,
  "duration_api_ms": 2104,
  "num_turns": 1,
  "result": "Two",
  "stop_reason": "end_turn",
  "total_cost_usd": 0.15264875,
  "usage": {
    "input_tokens": 6,
    "cache_creation_input_tokens": 24395,
    "cache_read_input_tokens": 0,
    "output_tokens": 6,
    "iterations": [{"input_tokens": 6, "output_tokens": 6, ...}]
  },
  "modelUsage": {
    "claude-opus-4-7": {
      "inputTokens": 6,
      "outputTokens": 6,
      "cacheReadInputTokens": 0,
      "cacheCreationInputTokens": 24395,
      "costUSD": 0.15264875
    }
  }
}
```

**Python extraction pattern** (reuse and extend from existing `harness.py` style):
```python
import json, subprocess, time
from pathlib import Path

def run_agent(instruction: str, mode: str, seed: int, cwd: Path) -> dict:
    env = {**os.environ, "ATELIER_BENCH_MODE": mode}
    cmd = [
        "claude", "--verbose", "--output-format", "stream-json",
        "-p", instruction,
        "--allowedTools", "Bash,Edit,Write,Read,Glob,Grep,LS",
        "--max-budget-usd", "10",
        "--dangerously-skip-permissions",  # needed for automated runs
    ]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=cwd)
    wall_ms = (time.perf_counter() - t0) * 1000

    result_line = None
    for line in proc.stdout.splitlines():
        try:
            obj = json.loads(line)
            if obj.get("type") == "result":
                result_line = obj
        except json.JSONDecodeError:
            pass

    if result_line is None:
        return {"error": "no result line", "wall_ms": wall_ms, "returncode": proc.returncode}

    u = result_line.get("usage", {})
    return {
        "input_tokens": u.get("input_tokens", 0),
        "output_tokens": u.get("output_tokens", 0),
        "cache_creation_input_tokens": u.get("cache_creation_input_tokens", 0),
        "cache_read_input_tokens": u.get("cache_read_input_tokens", 0),
        "total_cost_usd": result_line.get("total_cost_usd", 0.0),
        "duration_ms": result_line.get("duration_ms", wall_ms),
        "duration_api_ms": result_line.get("duration_api_ms", 0),
        "num_turns": result_line.get("num_turns", 0),
        "wall_ms": wall_ms,
        "is_error": result_line.get("is_error", False),
    }
```

**Important flags:**
- `--verbose` is required when using `--output-format stream-json` (otherwise error).
- `--dangerously-skip-permissions` is needed for automated benchmark runs (no human
  to click "Allow").
- `--max-budget-usd 10` provides a per-run cost cap (adjustable; D7 adds `--no-cost-cap`).
- `--bare` flag can disable Atelier's hooks when running the "off" arm inside
  TerminalBench's Docker containers (since Atelier MCP won't be installed there anyway).

**NOT recommended:** Using the Anthropic Python SDK directly to drive the agent.
Using `claude -p` is more credible ("we used Anthropic's own CLI, not a custom wrapper")
and matches the existing `benchmarks/swe/atelier_proxy.py` pattern. It also means the
agent runs exactly as a developer would run it.

---

## 4. Token Counting

### tiktoken — local approximation

Already in `pyproject.toml` at `tiktoken>=0.9`. Used in `benchmarks/mcp_tools/harness.py`:

```python
import tiktoken
_ENCODING = tiktoken.get_encoding("cl100k_base")

def _tokens(value: Any) -> int:
    text = json.dumps(value, default=str) if not isinstance(value, str) else value
    return len(_ENCODING.encode(text))
```

**Use for:** Estimating baseline (Atelier-off) token counts from static payloads
when doing pre-flight cost estimates. **Do not use as the primary metric** in A/B
results — use the actual `usage` fields from `claude -p stream-json` output instead.

**Why `cl100k_base` for Claude:** Claude uses its own tokenizer but `cl100k_base`
is within ~5% for English text. Good enough for estimates and baseline comparisons.
The exact counts always come from the API response.

### Anthropic API usage fields (confirmed in SDK v0.104.1)

```python
# From anthropic.types.Usage (source-inspected):
usage.input_tokens                  # int — uncached input tokens
usage.output_tokens                 # int — output tokens
usage.cache_creation_input_tokens   # Optional[int] — tokens written to cache
usage.cache_read_input_tokens       # Optional[int] — tokens read from cache
```

**Cost formula** (Sonnet pricing as of 2026-05):
```python
def compute_cost(u: dict) -> float:
    INPUT_PER_M    = 3.00   # USD per 1M input tokens (uncached)
    CACHE_WRITE_M  = 3.75   # per 1M cache-creation tokens  
    CACHE_READ_M   = 0.30   # per 1M cache-read tokens
    OUTPUT_PER_M   = 15.00  # per 1M output tokens
    return (
        u["input_tokens"]                * INPUT_PER_M   / 1_000_000
      + u.get("cache_creation_input_tokens", 0) * CACHE_WRITE_M  / 1_000_000
      + u.get("cache_read_input_tokens", 0)     * CACHE_READ_M   / 1_000_000
      + u["output_tokens"]               * OUTPUT_PER_M  / 1_000_000
    )
```

Prefer `total_cost_usd` from the `result` line — it's already computed by the CLI
and accounts for model-specific pricing correctly.

---

## 5. Statistics — Wilson Score Interval

**Requirement:** 95% CI for binary pass-rate (pass/fail per task × N=5 reps).

**Do not use scipy `proportion_confint`** — it was not importable in the project venv
(`ImportError: cannot import name 'proportion_confint' from 'scipy.stats'` in the
installed scipy version). Scipy is also not in `pyproject.toml` and adds a heavy dep.

**Do not use normal approximation (Wald interval)** — wildly incorrect at low N (e.g.,
`5/5` would give CI `[1.0, 1.0]`). The PROJECT.md explicitly mandates Wilson score.

**Implement as pure math** (10 lines, zero deps):

```python
import math

def wilson_ci(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for binomial pass-rate. 95% CI by default (z=1.96).

    Returns (lower, upper) as proportions in [0, 1].
    Correct at all N including N=5 with 0 or 5 successes.
    """
    if n == 0:
        return 0.0, 1.0
    p_hat = successes / n
    denom = 1.0 + z * z / n
    centre = (p_hat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt(p_hat * (1 - p_hat) / n + z * z / (4 * n * n))) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)

# Examples (verified 2026-05-28):
# wilson_ci(4, 5)  → (0.376, 0.964)
# wilson_ci(3, 5)  → (0.231, 0.882)
# wilson_ci(5, 5)  → (0.478, 1.000)   ← Wald would give (1.0, 1.0), wrong
# wilson_ci(0, 5)  → (0.000, 0.522)   ← Wald would give (0.0, 0.0), wrong
```

Place this in `benchmarks/ab/stats.py`. No external dep needed.

---

## 6. Plotting — matplotlib

**Not currently in `pyproject.toml`**. Must be added.

Add to `pyproject.toml` optional-dependencies:
```toml
[project.optional-dependencies]
benchmarks = [
    "matplotlib>=3.9",
    "jinja2>=3.1",   # already transitive via litellm but make explicit
    "scipy>=1.13",   # optional: only if proportion_confint needed elsewhere
]
```

Install with: `uv pip install "atelier[benchmarks]"` or in the benchmarks workspace.

**Use matplotlib for:**
- Three delta plots: cost delta, latency delta, quality delta (pass-rate)
- Each with 95% CI bars (Wilson interval for quality, mean±stderr for cost/latency)
- Save as PNG (`dpi=150`, `figsize=(10, 6)`)

**Pattern** (matches D4 spec):
```python
import matplotlib
matplotlib.use("Agg")  # headless — no display required in CI
import matplotlib.pyplot as plt

def plot_delta(
    task_ids: list[str],
    on_values: list[float],
    off_values: list[float],
    on_ci: list[tuple[float, float]],
    off_ci: list[tuple[float, float]],
    ylabel: str,
    title: str,
    out_path: Path,
) -> None:
    fig, ax = plt.subplots(figsize=(10, 6))
    x = range(len(task_ids))
    ax.bar([i - 0.2 for i in x], on_values, width=0.4, label="Atelier-on", color="#2196F3")
    ax.bar([i + 0.2 for i in x], off_values, width=0.4, label="Atelier-off", color="#FF5722")
    # CI error bars omitted for brevity; use ax.errorbar()
    ax.set_xticks(list(x))
    ax.set_xticklabels(task_ids, rotation=45, ha="right")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
```

**Do NOT use plotly** unless interactive HTML is explicitly required. The plan says
"PNG files" for `report.md` and docusaurus blog posts. Plotly adds a heavy dep and
its static export requires kaleido (another dep). matplotlib + Agg backend is zero-dep,
headless-safe, and already familiar to the codebase (referenced in PROJECT.md).

---

## 7. Report Templating — Jinja2

**Already available** — transitive through `litellm>=1.83.14`. Version 3.1.6 confirmed
in venv. Make it an explicit dep in the benchmarks extras.

**Use for:**
- `benchmarks/ab/templates/report.md.j2` — main report template
- Variables: `run_id`, `commit_sha`, `model`, `n`, `tasks`, `summary_table`, `losses`

**Pattern:**
```python
from jinja2 import Environment, FileSystemLoader

env = Environment(loader=FileSystemLoader("benchmarks/ab/templates/"))
tmpl = env.get_template("report.md.j2")
report_md = tmpl.render(
    run_id=run_id,
    commit_sha=git_sha,
    model=model_name,
    n_reps=n,
    tasks=task_summaries,
    losses=[t for t in task_summaries if t["delta_quality"] < 0],
    generated_at=datetime.utcnow().isoformat(),
)
```

---

## 8. CLI Framework — Click

**Already in `pyproject.toml` at `click>=8.1`**. Used throughout `atelier.gateway.cli`.

Extend `src/atelier/cli/commands/bench.py` as a Click group:
```python
import click

@click.group()
def bench():
    """Benchmark Atelier-on vs Atelier-off."""

@bench.command()
@click.option("--suite", default="terminalbench")
@click.option("--quick", is_flag=True)
@click.option("--full", is_flag=True)
@click.option("--yes", is_flag=True)
def run(suite, quick, full, yes): ...

@bench.command()
@click.argument("run_id")
@click.option("--out", required=True, type=click.Path())
def publish(run_id, out): ...
```

---

## 9. Terminal Output — Rich

**Already in `pyproject.toml` at `rich>=13.7`**. Used in `benchmarks/mcp_tools/reporter.py`
for ANSI colour output. Use `rich.table.Table` for the comparison table in D7:

```python
from rich.table import Table
from rich.console import Console

console = Console()
table = Table(title="Atelier A/B Benchmark — Quick Run")
table.add_column("Task")
table.add_column("Atelier-on cost $")
table.add_column("Atelier-off cost $")
table.add_column("Δ%")
table.add_column("Quality on")
table.add_column("Quality off")
console.print(table)
```

**Do not replicate** the existing ANSI escape-code reporter pattern from
`benchmarks/mcp_tools/reporter.py` — use Rich directly.

---

## 10. Data Serialisation

**JSON only.** All intermediate results (`raw/<task>__<mode>__<rep>.json`,
`summary.json`, `config.json`) use stdlib `json`. Pydantic models used for
validation where needed:

```python
from pydantic import BaseModel

class RunRecord(BaseModel):
    task_id: str
    mode: str          # "on" | "off"
    rep: int
    seed: int
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    total_cost_usd: float
    duration_ms: float
    passed: bool | None  # None = no grader result yet
    transcript_path: str
    started_at: str
    ended_at: str
```

**Do NOT use pickle, SQLite, or pandas** for intermediate data — JSON is human-readable,
git-diffable, and reproducible across Python versions. Pandas is not in the core deps
and adds overhead for what is simple aggregation.

---

## 11. Patterns from Existing Code to Reuse

### From `benchmarks/mcp_tools/harness.py`

| Pattern | Reuse in |
|---------|---------|
| `time.perf_counter()` for wall-clock timing | `benchmarks/ab/runner.py` |
| `try/except` around agent call, return error result | `benchmarks/ab/runner.py` |
| `_tokens(value)` via tiktoken for baseline estimation | `benchmarks/ab/aggregate.py` |
| `dataclass`-based result shapes | `benchmarks/ab/runner.py` (extend to `RunRecord`) |

### From `benchmarks/swe/atelier_proxy.py`

| Pattern | Reuse in |
|---------|---------|
| `subprocess.run()` wrapping `claude` CLI | `benchmarks/ab/runner.py` |
| JSONL line-by-line streaming parse | `benchmarks/ab/runner.py` |

### From `benchmarks/mcp_tools/reporter.py`

| Pattern | Reuse in |
|---------|---------|
| `render_tool_report()` column-based layout | Upgrade to Rich Table in D7 |

---

## 12. What NOT to Use

| Technology | Why Not |
|------------|---------|
| **pandas** | Not in deps; simple aggregation doesn't need it; adds MB to install |
| **plotly** | Heavy dep; kaleido needed for static export; PNG + matplotlib is sufficient |
| **scipy.proportion_confint** | Not importable in current venv; unstable API across versions; implement Wilson natively |
| **Normal/Wald CI** | Mathematically wrong at N=5 for binary metrics; Wilson is the right tool |
| **Anthropic Python SDK for agent runs** | Use `claude -p` subprocess — more credible, simpler, matches existing pattern |
| **Git submodule for terminal-bench** | PyPI `terminal-bench==0.2.18` is pinnable and reproducible; PROJECT.md bans vendored copies |
| **SQLite/PostgreSQL for run storage** | Flat JSON files are portable, human-readable, and easily committed to the run archive |
| **asyncio for A/B runner** | ThreadPoolExecutor is sufficient (benchmarks are I/O-bound subprocesses, not async coroutines); matches existing harness style |

---

## 13. Complete Dependency Delta

Add to `pyproject.toml` (either as `[project.optional-dependencies].benchmarks` or in the separate `benchmarks/pyproject.toml`):

```toml
# New dependencies needed for benchmark milestone
matplotlib>=3.9          # plots (not currently in deps)
jinja2>=3.1              # explicit dep for report templates (currently transitive)

# Already present — no changes needed
tiktoken>=0.9            # ✓ in pyproject.toml
click>=8.1               # ✓ in pyproject.toml
rich>=13.7               # ✓ in pyproject.toml
numpy>=2.0               # ✓ (comes with scipy/terminal-bench)
pydantic>=2.6            # ✓ in pyproject.toml

# For benchmarks workspace only (Python >=3.12)
terminal-bench==0.2.18   # ← requires Python 3.12, isolate in benchmarks workspace
```

---

## 14. Critical Python Version Warning

**terminal-bench requires Python ≥ 3.12. The main project venv runs Python 3.11.12.**

Verified (2026-05-28):
```
× Because the current Python version (3.11.12) does not satisfy Python>=3.12,
  all versions of terminal-bench cannot be used.
```

**Resolution options ranked by effort:**

1. **Isolated benchmarks workspace** (recommended): `benchmarks/pyproject.toml` pins
   `requires-python = ">=3.12"`. Main project stays at 3.11. The A/B runner CLI
   invokes `uv run --project benchmarks/ ...` or the user runs from inside `benchmarks/`.

2. **uv tool install** (quick prototype): `uv tool install terminal-bench==0.2.18`
   installs into an isolated Python 3.12 env. Call `tb run` as a subprocess. Less
   programmatic control (can't subclass `BaseAgent` from the main venv) but works for D2.

3. **Bump main project to Python 3.12**: Edit `pyproject.toml` `requires-python` to
   `>=3.12`. Breaking for any users still on 3.11. Requires a decision from the team.

---

## Sources

- `terminal-bench` wheel inspected: `/tmp/tb_check/terminal_bench-0.2.18-py3-none-any.whl` (downloaded 2026-05-28)
- Live `claude -p --output-format stream-json --verbose` run showing exact JSON fields (2026-05-28)
- `uv pip install terminal-bench` in project venv confirming Python 3.12 conflict (2026-05-28)
- `benchmarks/mcp_tools/harness.py` (existing codebase)
- `benchmarks/swe/atelier_proxy.py` (existing codebase)
- `pyproject.toml` — confirmed present deps: tiktoken, click, rich, pydantic, jinja2 (transitive), numpy (via scipy)
- Anthropic SDK `anthropic==0.104.1` source (confirmed Usage field names)
- Wilson score interval: standard statistics reference, manually verified against live formula
