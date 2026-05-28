# Phase 13: Phase-Linear Cache-Reuse Agent — Pattern Map

**Mapped:** 2026-05-28
**Files analyzed:** 15 (new + modified)
**Analogs found:** 14 / 15

> **Locked input:** `docs/plans/phase-linear-cache-reuse/{index,00-rationale,01-PLAN,02-DESIGN-SPEC}.md`. All file shapes below are derived from CONTEXT D-01..D-18 and RESEARCH.md §"Recommended Project Structure" (lines 203-247).
>
> **Dirty-work guard (D-18):** `src/atelier/core/capabilities/context_reuse/capability.py`, `src/atelier/core/runtime/engine.py`, and `tests/core/test_capabilities_production.py` contain in-flight user edits. Planner must mark all changes to these three files as **additive-only** (no rewrites, no "fix to make tests green"). Inspect-then-extend; never overwrite.

---

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match |
|---|---|---|---|---|
| `src/atelier/core/capabilities/context_reuse/models.py` *(EXTEND)* | model | transform | self (same file, existing dataclasses) + `prefix_cache/planner.py::PrefixCachePlan` | exact |
| `src/atelier/core/capabilities/context_reuse/phase_runner.py` *(NEW)* | service / orchestrator | event-driven (turn loop) | `core/runtime/engine.py::AtelierRuntimeCore` + `prefix_cache/diagnostics.py::PrefixCacheDiagnostics` | role-match |
| `src/atelier/core/capabilities/context_reuse/prompts/{shell,survey,plan,implement}.md` *(NEW)* | config / static asset | n/a (read-once) | none — first static prompt assets under capabilities | none |
| `src/atelier/core/capabilities/context_compression/minify.py` *(NEW)* | utility | transform (string in → string out + metrics) | `context_compression/deduplication.py` (sibling pure-function module) | role-match |
| `src/atelier/core/capabilities/context_compression/models.py` *(EXTEND)* | model | transform | self (`CompressionResult`, `DroppedContext`) | exact |
| `src/atelier/core/runtime/engine.py` *(EXTEND, dirty)* | controller / dispatcher | request-response | self (existing `AtelierRuntimeCore.get_context`) | exact |
| `src/atelier/infra/runtime/run_ledger.py` *(EXTEND)* | infra / persistence | append-log | self (`record_call`) | exact |
| `benchmarks/linear_vs_per_agent/__init__.py` *(NEW)* | package marker | n/a | `benchmarks/ab/__init__.py` | exact |
| `benchmarks/linear_vs_per_agent/runner.py` *(NEW)* | controller / CLI | batch | `benchmarks/ab/runner.py` | exact |
| `benchmarks/linear_vs_per_agent/reporter.py` *(NEW)* | utility / aggregation | transform (raw → report) | `benchmarks/ab/aggregate.py` + `benchmarks/ab/report.py` | exact |
| `benchmarks/linear_vs_per_agent/scenarios.yaml` *(NEW)* | config | n/a | `benchmarks/terminalbench/tasks.yaml` (referenced from `ab/runner.py:32-38`) | role-match |
| `benchmarks/linear_vs_per_agent/tests/test_runner.py` *(NEW)* | test (integration) | request-response | `benchmarks/ab/tests/test_runner.py`, `test_bench_run.py` | exact |
| `benchmarks/linear_vs_per_agent/tests/test_reporter.py` *(NEW)* | test (unit) | transform | `benchmarks/ab/tests/test_aggregate.py` | exact |
| `tests/core/test_phase_runner.py`, `test_phase_runner_minify.py`, `test_runtime_mode_dispatch.py`, `test_minify_source.py` *(NEW)* | test (unit) | request-response | `tests/core/test_capabilities_runtime_core.py` | exact |
| `tests/core/test_capabilities_production.py` *(MODIFIED, dirty)* | test (regression) | n/a | self | exact |

---

## Pattern Assignments

### `src/atelier/core/capabilities/context_reuse/models.py` (model, EXTEND)

**Analogs:** existing `context_reuse/models.py` (lines 1-62) and `prefix_cache/planner.py::PrefixCachePlan` (lines 21-55).

**Convention to follow (existing dataclasses, lines 9-23):**
```python
@dataclass
class ReuseSavings:
    procedures_retrieved: int = 0
    context_tokens_saved: int = 0
    reuse_events: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "procedures_retrieved": self.procedures_retrieved,
            ...
        }
```
- Module docstring `"""Data models for ..."""`
- `from __future__ import annotations`
- `@dataclass` (not Pydantic) with explicit `to_dict()` per D + CLAUDE.md "Public data contracts" rule.

**Frozen-dataclass + `to_dict()` model for cache-stable enums** (copy from `prefix_cache/planner.py:21-55`):
```python
@dataclass(frozen=True)
class PrefixCachePlan:
    static_prefix: tuple[PromptBlock, ...]
    ...
    invalidated_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"prefix_hash": self.prefix_hash, ...}
```

**New additions (per RESEARCH §"Recommended Project Structure" line 208 and §"Pattern 1" lines 256-277):**
- `Phase` (frozen dataclass, fields: `name`, `kind: Literal["agent","gate","side_effect"]`, `profile: Literal["reader","writer"]`, `objective_path: str|None`, `continue_from: str|None`, `next: str|None`).
- `PhasePlan` (mutable dataclass: `name`, `entry`, `phases: dict[str, Phase]`).
- `PhaseResult` (mutable: `phase_name`, `messages: list[dict]`, `cache_stats: PhaseCacheStats`, `output_text: str`).
- `PhaseCacheStats` — mirror fields from `PrefixCachePlan.to_dict()` + provider `cache_read_input_tokens` and `cache_creation_input_tokens` (D-07, D-11).
- `RunMode(StrEnum)` with `LINEAR`, `PER_AGENT`, `AUTO` (Pattern: Mode Dispatch sketch RESEARCH lines 460-463). Place in `models.py` so engine and benchmark can both import without cycle.

---

### `src/atelier/core/capabilities/context_reuse/phase_runner.py` (NEW, service / event-driven)

**Analog:** `prefix_cache/diagnostics.py` for the per-turn accumulation idiom; `AtelierRuntimeCore` for the orchestrator constructor shape; RESEARCH lines 395-427 for the canonical sketch.

**Imports pattern** (copy from `prefix_cache/planner.py:1-18` + `engine.py:1-38`):
```python
"""PhaseRunner — phase-linear conversation orchestrator (LINEAR-01/02)."""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atelier.core.capabilities.prefix_cache.planner import PrefixCachePlanner
from atelier.core.capabilities.prefix_cache.diagnostics import PrefixCacheDiagnostics
from atelier.core.capabilities.prompt_compilation.models import (
    BlockKind, PromptBlock, Stability,
)
from atelier.infra.runtime.run_ledger import RunLedger
from .models import Phase, PhasePlan, PhaseResult, PhaseCacheStats
```

**Constructor shape** (copy from `AtelierRuntimeCore.__init__` engine.py:55-72 — explicit deps, no global state):
```python
class PhaseRunner:
    def __init__(
        self,
        plan: PhasePlan,
        *,
        provider,                     # callable: (messages) -> (text, tokens_in, tokens_out, cache_read, cache_write)
        ledger: RunLedger,
        planner: PrefixCachePlanner,
        diag: PrefixCacheDiagnostics,
        prompts_dir: Path | None = None,
    ) -> None: ...
```

**Core orchestration pattern** (RESEARCH lines 402-426; cache breakpoint per phase tail per D-07):
```python
def run(self) -> dict[str, PhaseResult]:
    results: dict[str, PhaseResult] = {}
    messages: list[dict] = [{"role": "system", "content": self._shell_prompt}]
    for phase_name in self._phase_order():
        phase = self.plan.phases[phase_name]
        if phase.continue_from is None:                       # D-05: implement starts lean
            messages = [{"role": "system", "content": self._shell_prompt}]
        messages.append({"role": "user", "content": self._load_objective(phase)})
        messages, phase_stats, output_text = self._run_agent_loop(phase, messages)
        plan_record = self.planner.plan_with_history(
            blocks=self._to_blocks(messages),
            prior_prefix_hash=self.diag.last_prefix_hash,
        )
        self.diag.record_plan(plan_record)                     # breakpoint @ phase tail
        results[phase_name] = PhaseResult(
            phase_name=phase_name,
            messages=messages.copy(),
            cache_stats=phase_stats,
            output_text=output_text,
        )
    return results
```

**Stability tagging for cache anchoring** (RESEARCH lines 370-393):
```python
def _shell_block(self) -> PromptBlock:
    return PromptBlock(
        id="phase_runner/shell",
        kind=BlockKind.SYSTEM,
        stability=Stability.STATIC,
        content=self._shell_prompt,
    )

def _objective_block(self, phase_name: str) -> PromptBlock:
    return PromptBlock(
        id=f"phase_runner/objective/{phase_name}",
        kind=BlockKind.USER_TASK,
        stability=Stability.BRANCH,
        content=(self._prompts_dir / f"{phase_name}.md").read_text(),
        stability_override_reason="phase objective is per-phase but stable across turns",
    )
```
*(Override reason required because `USER_TASK`'s default stability is `TURN` per `prompt_compilation/models.py:62`.)*

**Tool-profile enforcement** (D-08, RESEARCH lines 289-292):
```python
_READER_TOOLS = frozenset({"read", "search", "glob", "code_intel", "web"})
_WRITER_TOOLS = _READER_TOOLS | {"write", "edit", "delete"}

def _allowed_tools(self, phase: Phase) -> frozenset[str]:
    return _WRITER_TOOLS if phase.profile == "writer" else _READER_TOOLS
```
Tool dispatcher must assert `tool_name in self._allowed_tools(phase)` before each call.

**Telemetry hook** — every agent-loop turn calls:
```python
self.ledger.record_call(
    operation=f"phase:{phase.name}",
    model=model,
    input_tokens=in_tok,
    output_tokens=out_tok,
    cache_read_tokens=cache_read,
    stable_prefix_hash=plan_record.prefix_hash,
    prefix_invalidated_reason=plan_record.invalidated_reason,
)
```

---

### `src/atelier/core/capabilities/context_reuse/prompts/*.md` (NEW, static config)

**No code analog.** Plain markdown files; treat byte-stability as a hard contract (D-06, D-07; test `test_system_prompt_byte_stable`).

- `shell.md` — fixed system prompt used across all phases. **Must not be templated with phase variables.**
- `survey.md`, `plan.md`, `implement.md` — short user objective texts injected as `{"role": "user", "content": ...}`.

Planner action: create `prompts/__init__.py` (empty) and check files into git with `LF` newlines + no trailing whitespace (so `PromptBlock.version_hash` is stable across OSes).

---

### `src/atelier/core/capabilities/context_compression/minify.py` (NEW, utility / transform)

**Analog:** `context_compression/deduplication.py` (sibling pure-function module pattern). Sketch in RESEARCH lines 429-452.

**Module shape:**
```python
"""Read-context source minifier (LINEAR-03).

Conservative whitespace-only transform. Never minifies for writer-profile reads
(D-09); writer paths must read exact bytes.
"""
from __future__ import annotations

import re

_BLANK_RUN = re.compile(r"\n{3,}")
_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_WHITESPACE_SIGNIFICANT = {"python", "py", "yaml", "yml", "makefile", "haml"}

def minify_source(text: str, lang: str) -> tuple[str, int, int]:
    """Return (minified, original_tokens, minified_tokens). Never edits leading WS
    for whitespace-significant languages (D-10)."""
    original = text
    out = _TRAILING_WS.sub("", text)
    out = _BLANK_RUN.sub("\n\n", out)
    # additional safe collapses only for non-WS-significant langs
    return out, _count_tokens(original), _count_tokens(out)
```

**Token-counter reuse** — import from `atelier.core.capabilities.prompt_compilation.tokens` (used by `PromptBlock.token_estimate`, see `prompt_compilation/models.py:120`), do NOT introduce a new tokenizer.

**Telemetry contract** (D-11): caller (PhaseRunner) records `(original_tokens, minified_tokens, lang)` per read into `PhaseCacheStats.minify_deltas: list[dict]`.

---

### `src/atelier/core/capabilities/context_compression/models.py` (EXTEND, model)

**Analog:** self (lines 1-60). Convention: simple `@dataclass` + `to_dict()`.

**Add:**
```python
@dataclass
class MinificationDelta:
    """Per-read minification telemetry (LINEAR-03, D-11)."""
    path: str
    lang: str
    original_tokens: int
    minified_tokens: int

    @property
    def saved_tokens(self) -> int:
        return max(0, self.original_tokens - self.minified_tokens)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "lang": self.lang,
            "original_tokens": self.original_tokens,
            "minified_tokens": self.minified_tokens,
            "saved_tokens": self.saved_tokens,
        }
```

---

### `src/atelier/core/runtime/engine.py` (EXTEND, dispatcher) — **DIRTY: ADDITIVE ONLY**

**Analog:** self. Existing constructor at lines 55-72 wires capabilities one-by-one. Existing `get_context()` at line 87 must remain byte-identical.

**Pattern for dispatcher addition** (RESEARCH lines 455-481):
```python
# New imports — add to existing import block, do not reorder
from atelier.core.capabilities.context_reuse.models import RunMode, PhasePlan
from atelier.core.capabilities.context_reuse.phase_runner import PhaseRunner

# Add NEW method; do NOT modify get_context() or __init__() bodies beyond
# appending one capability wiring line for the runner factory.

def run_phased(
    self,
    plan: PhasePlan,
    *,
    mode: RunMode = RunMode.AUTO,
    projected_prefix_tokens: int = 0,
    divergence_signal: bool = False,
) -> dict[str, Any]:
    chosen = self._resolve_run_mode(mode, projected_prefix_tokens, divergence_signal)
    if chosen is RunMode.LINEAR:
        runner = self._build_phase_runner(plan)
        return {"mode": chosen.value, "results": runner.run()}
    return {"mode": chosen.value, "results": self._run_per_agent(plan)}

def _resolve_run_mode(self, mode, prefix_tokens, divergence) -> RunMode:
    if mode is not RunMode.AUTO:
        return mode
    if divergence or prefix_tokens > LINEAR_PREFIX_THRESHOLD:   # D-13
        return RunMode.PER_AGENT
    return RunMode.LINEAR
```

**Constraints:**
- Do **not** rename existing fields, restructure `__init__`, or "clean up" dirty work.
- Add `LINEAR_PREFIX_THRESHOLD` as a module-level constant near the top of `engine.py` (do not bury in method body).
- Reuse `Phase 12 ModelRouter.recommend()` only via the existing `self.quality_router` or new dep — do NOT instantiate a second router (per "Don't Hand-Roll" line 311).

---

### `src/atelier/infra/runtime/run_ledger.py` (EXTEND, infra)

**Analog:** self, `record_call` at lines 191-239.

**Additive contract** — extend keyword-only signature (preserve back-compat):
```python
def record_call(
    self,
    *,
    operation: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,           # NEW (D-07)
    phase: str | None = None,              # NEW (D-11 attribution)
    ...
    stable_prefix_hash: str | None = None,
    prefix_invalidated_reason: str = "",
) -> LedgerEvent:
```
- Add `"cache_write_tokens"` and `"phase"` keys to the inner `record(..., {...})` payload dict (lines 224-238).
- Old run-ledger JSON remains readable: snapshot/loader code must `.get("cache_write_tokens", 0)` and `.get("phase")` (RESEARCH line 324).
- Do **not** modify `CostTracker.record_call` signature; the new fields are ledger-only.

---

### `benchmarks/linear_vs_per_agent/runner.py` (NEW, CLI/batch)

**Analog:** `benchmarks/ab/runner.py` (lines 1-159) — copy the whole shape.

**Imports + CLI** (copy from `ab/runner.py:14-22, 96-115`):
```python
import argparse, datetime, json, os
from pathlib import Path

def main() -> None:
    parser = argparse.ArgumentParser(prog="benchmarks.linear_vs_per_agent.runner")
    parser.add_argument("--out", required=True)
    parser.add_argument("--scenarios", default=str(
        Path(__file__).parent / "scenarios.yaml"))
    parser.add_argument("--modes", default="linear,per_agent")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    ...
```

**Resumable cell pattern** (copy from `ab/runner.py:62-93`):
```python
def run_cell(scenario_id, mode, rep, raw_dir, ...) -> bool:
    dest = raw_dir / f"{scenario_id}__{mode}__rep{rep}.json"
    if dest.exists():
        print(f"  skip {dest.name} (already done)")
        return True
    result = run_phase_linear_trial(scenario_id, mode=mode, ...)
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps(result.to_dict(), indent=2, default=str))
    os.replace(tmp, dest)        # atomic write — copy from ab/runner.py:90-92
    return not result.is_error
```

**Per-cell result must include** (LINEAR-05, TBEVAL-01): `cost_usd`, `wall_time_ms`, `cache_read_tokens`, `cache_write_tokens`, `cache_hit_ratio`, `minify_delta_tokens`, `task_success`, `mode`. Use isolated `ATELIER_ROOT` per arm (T-13-05, validation 13-04-01).

---

### `benchmarks/linear_vs_per_agent/reporter.py` (NEW, transform)

**Analogs:** `benchmarks/ab/aggregate.py:31-72` and `benchmarks/ab/report.py:46-80`.

**Aggregation skeleton** (copy from `aggregate.py:31-72`):
```python
def compute_report(run_id: str, raw_dir: Path) -> dict:
    cells: dict[str, dict] = {}
    for path in sorted(raw_dir.glob("*.json")):
        if path.suffix == ".tmp" or path.name.endswith(".json.tmp"):
            continue
        cell_key = path.stem.rsplit("__rep", 1)[0]
        record = json.loads(path.read_text())
        cell = cells.setdefault(cell_key, {"cost": [], "wall": [], ...})
        cell["cost"].append(record["cost_usd"])
        ...
    # mean + linear-vs-per_agent deltas; separate cache savings from minify savings (D-17)
```

**Report fields required** (D-15, D-16, D-17): cost, wall time, cache-hit ratio, **minify delta separated from cache savings**, task success. Threshold check: ≥30% cost reduction, ≥25% wall-time reduction at equal-or-better success.

---

### `benchmarks/linear_vs_per_agent/scenarios.yaml` (NEW, config)

**Analog:** `benchmarks/terminalbench/tasks.yaml` (loaded by `ab/runner.py:32-38`).

**Shape:**
```yaml
dataset:
  name: phase-linear-cache-reuse
  version: "0.1.0"
scenarios:
  - id: survey_plan_implement_small
    description: 3-file refactor; high context sharing
    expected_mode: linear
  ...
```
Must contain ≥7 representative scenarios (D-15). At least one must be a "divergent" case where `auto` should pick `per_agent` (validates test 13-03-04).

---

### `benchmarks/linear_vs_per_agent/tests/test_runner.py` & `test_reporter.py` (NEW)

**Analog:** `benchmarks/ab/tests/test_runner.py`, `test_aggregate.py:1-50`.

**Test shape (copy from `test_aggregate.py:42-50`):**
```python
def test_compute_report_schema():
    with tempfile.TemporaryDirectory() as d:
        raw = pathlib.Path(d) / "raw"; raw.mkdir()
        (raw / "s1__linear__rep1.json").write_text(json.dumps({
            "cost_usd": 0.5, "wall_time_ms": 1000,
            "cache_read_tokens": 800, "cache_write_tokens": 200,
            "minify_delta_tokens": 150, "task_success": True, "mode": "linear",
        }))
        report = compute_report("test-run", raw)
        assert "cells" in report and "deltas" in report
```

**Isolated ATELIER_ROOT per arm** (copy `test_bench_run.py:24-37` `monkeypatch` + `patch.dict(os.environ, {"ATELIER_ROOT": d})`) — required for T-13-05.

---

### `tests/core/test_phase_runner.py`, `test_phase_runner_minify.py`, `test_runtime_mode_dispatch.py`, `test_minify_source.py` (NEW)

**Analog:** `tests/core/test_capabilities_runtime_core.py:1-80`.

**Fixture pattern** (copy from `test_capabilities_runtime_core.py:13-22`):
```python
def _init_root(root: Path) -> None:
    runner = CliRunner()
    res = runner.invoke(cli, ["--root", str(root), "init"])
    assert res.exit_code == 0, res.output

def test_plan_continues_survey_messages(tmp_path: Path) -> None:
    root = tmp_path / ".atelier"; _init_root(root)
    rt = AtelierRuntimeCore(root)
    ...
```

**Fake provider fixture** (Wave 0 dep, validation §"Wave 0 Requirements"):
```python
class _FakeProvider:
    def __init__(self) -> None:
        self.calls: list[list[dict]] = []
    def complete(self, messages, *, cache_read=0, cache_write=0):
        self.calls.append([m.copy() for m in messages])
        return "ok", 100, 50, cache_read, cache_write
```
Asserts to verify: (a) plan-phase first call's `messages[:len(survey)]` ≡ survey-tail messages bytewise (D-04); (b) system prompt hash identical across calls (D-06); (c) implement-phase first call has only `[system, user(plan_text)]`, no survey/plan history (D-05); (d) writer-profile read returns exact bytes vs reader-profile minified (D-09).

---

## Shared Patterns

### Stability tagging (cache anchoring)
**Source:** `src/atelier/core/capabilities/prompt_compilation/models.py:24-66`
**Apply to:** `phase_runner.py` and any place injecting messages into the runner.
```python
# Shell prompt = STATIC ; phase objectives = BRANCH (with override_reason)
PromptBlock(id="...", kind=BlockKind.SYSTEM, stability=Stability.STATIC, content=...)
PromptBlock(id="...", kind=BlockKind.USER_TASK, stability=Stability.BRANCH,
            stability_override_reason="phase objective", content=...)
```
**Why:** `PrefixCachePlanner.plan_with_history()` (planner.py:97-117) auto-emits `invalidated_reason` and a stable hash when blocks are classified correctly.

### Telemetry recording (every LLM call)
**Source:** `src/atelier/infra/runtime/run_ledger.py:191-239`
**Apply to:** `phase_runner.py`, `benchmarks/linear_vs_per_agent/runner.py`.
```python
ledger.record_call(
    operation=f"phase:{phase.name}",
    model=model,
    input_tokens=..., output_tokens=...,
    cache_read_tokens=cache_read,
    cache_write_tokens=cache_write,         # new field
    phase=phase.name,                       # new field
    stable_prefix_hash=plan_record.prefix_hash,
    prefix_invalidated_reason=plan_record.invalidated_reason,
)
```
Cross-check `cache_read_tokens > 0` against `plan_record.prefix_hash` match (D-07: never assume cache hit from structure alone).

### Public dataclass contract
**Source:** `src/atelier/core/capabilities/context_reuse/models.py:9-23` and `context_compression/models.py:9-48`.
**Apply to:** all new/extended models.
- `from __future__ import annotations`
- `@dataclass` (or `@dataclass(frozen=True)` for cache-stable schema records)
- Explicit `to_dict()` method (no `asdict()` round-trip; round each float)
- No Pydantic in core; use it only at gateway boundaries (already enforced project-wide).

### Atomic file write
**Source:** `benchmarks/ab/runner.py:55-59, 90-92`.
**Apply to:** any benchmark output writer.
```python
tmp = dest.with_suffix(".tmp")
tmp.write_text(payload)
os.replace(tmp, dest)
```

### `uv run` everywhere
**Source:** RESEARCH lines 496, validation §"Test Infrastructure".
**Apply to:** every command in test/CI strings — `uv run pytest`, `uv run mypy`, `uv run python -m benchmarks.linear_vs_per_agent.runner`. Never bare `python` / `pytest`.

---

## Dirty-Work Preservation Contract (D-18)

These files have uncommitted user work. Planner tasks touching them MUST be additive-only:

| File | Allowed | Forbidden |
|---|---|---|
| `src/atelier/core/capabilities/context_reuse/capability.py` | Read for reuse signal; import new symbols from `models.py`/`phase_runner.py` | Refactor existing classes; rename methods; "fix" failing tests against this file |
| `src/atelier/core/runtime/engine.py` | Add new constants, `run_phased`, `_resolve_run_mode`, `_build_phase_runner`; add one import block | Modify `__init__`, `get_context`, or any existing method bodies |
| `tests/core/test_capabilities_production.py` | Add new test functions at end of file | Edit/rename/delete any existing test; modify shared fixtures |

Each plan that touches these files MUST cite this row and include a pre-edit `git diff -- <file>` snapshot capture step (validation Wave 0 line 73).

---

## No Analog Found

| File | Reason |
|---|---|
| `src/atelier/core/capabilities/context_reuse/prompts/*.md` | No precedent for capability-scoped static prompt assets in the repo. Plan should establish convention: directory adjacent to capability module, byte-stable LF newlines, no template variables. |

---

## Metadata

**Analog search scope:**
- `src/atelier/core/capabilities/{context_reuse,context_compression,prefix_cache,prompt_compilation}/`
- `src/atelier/core/runtime/`, `src/atelier/infra/runtime/`
- `benchmarks/ab/`, `benchmarks/ab/tests/`, `benchmarks/terminalbench/`
- `tests/core/`

**Files scanned (read):** 10 (models.py ×3, planner.py, diagnostics.py, prompt_compilation/models.py, engine.py header, run_ledger.py record_call region, ab/runner.py, ab/aggregate.py, test_capabilities_runtime_core.py header)

**Pattern extraction date:** 2026-05-28
