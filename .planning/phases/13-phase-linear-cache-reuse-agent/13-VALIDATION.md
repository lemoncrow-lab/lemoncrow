---
phase: 13
slug: phase-linear-cache-reuse-agent
status: draft
nyquist_compliant: true
wave_0_complete: false
created: 2026-05-28
---

# Phase 13 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest |
| **Config file** | `pyproject.toml` |
| **Quick run command** | `uv run pytest -q -x -m "not slow"` |
| **Full suite command** | `make test` |
| **Estimated runtime** | Fast suite varies by host; focused Wave 0 commands should remain under 60 seconds each. |

---

## Sampling Rate

- **After every task commit:** Run `uv run pytest -q -x -m "not slow"` unless the plan task specifies a narrower red/green command for TDD.
- **After every plan wave:** Run `make lint && make typecheck && make test`.
- **Before `/gsd-verify-work`:** `make pre-commit` plus the Phase 13 benchmark artifact must be complete.
- **Max feedback latency:** 60 seconds for focused task checks.

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 13-01-01 | 01 | 0 | LINEAR-01 | — | N/A | unit | `uv run pytest tests/core/test_phase_runner.py::test_models_have_required_fields -x` | No | pending |
| 13-01-02 | 01 | 0 | LINEAR-01 | — | N/A | unit | `uv run pytest tests/core/test_phase_runner.py::test_state_machine_schema -x` | No | pending |
| 13-01-03 | 01 | 0 | LINEAR-02 | T-13-01 | Reader profile cannot mutate files | unit | `uv run pytest tests/core/test_phase_runner.py::test_plan_continues_survey_messages -x` | No | pending |
| 13-01-04 | 01 | 0 | LINEAR-02 | T-13-03 | Static system prompt hash remains stable | unit | `uv run pytest tests/core/test_phase_runner.py::test_system_prompt_byte_stable -x` | No | pending |
| 13-01-05 | 01 | 0 | LINEAR-02 | T-13-03 | Cache breakpoint emitted per phase tail | unit | `uv run pytest tests/core/test_phase_runner.py::test_breakpoint_per_phase_tail -x` | No | pending |
| 13-01-06 | 01 | 0 | LINEAR-02 | T-13-01 | Implement starts with writer profile and no Survey/Plan history | unit | `uv run pytest tests/core/test_phase_runner.py::test_implement_starts_lean -x` | No | pending |
| 13-02-01 | 02 | 0 | LINEAR-03 | T-13-02 | Minifier performs string transforms only | unit | `uv run pytest tests/core/test_minify_source.py::test_collapses_blank_runs -x` | No | pending |
| 13-02-02 | 02 | 0 | LINEAR-03 | T-13-02 | Python parses after minification | unit | `uv run pytest tests/core/test_minify_source.py::test_python_semantics_preserved -x` | No | pending |
| 13-02-03 | 02 | 0 | LINEAR-03 | T-13-02 | YAML loads to identical structure after minification | unit | `uv run pytest tests/core/test_minify_source.py::test_yaml_semantics_preserved -x` | No | pending |
| 13-02-04 | 02 | 0 | LINEAR-03 | T-13-04 | Writer reads bypass minification and preserve exact bytes | unit | `uv run pytest tests/core/test_phase_runner_minify.py::test_writer_profile_exact_bytes -x` | No | pending |
| 13-02-05 | 02 | 0 | LINEAR-03 | T-13-04 | Minification telemetry records original and minified token counts | unit | `uv run pytest tests/core/test_phase_runner_minify.py::test_minify_telemetry -x` | No | pending |
| 13-03-01 | 03 | 0 | LINEAR-04 | — | N/A | unit | `uv run pytest tests/core/test_runtime_mode_dispatch.py::test_explicit_modes -x` | No | pending |
| 13-03-02 | 03 | 0 | LINEAR-04 | — | N/A | unit | `uv run pytest tests/core/test_runtime_mode_dispatch.py::test_auto_picks_linear -x` | No | pending |
| 13-03-03 | 03 | 0 | LINEAR-04 | — | N/A | unit | `uv run pytest tests/core/test_runtime_mode_dispatch.py::test_auto_falls_back_oversized -x` | No | pending |
| 13-03-04 | 03 | 0 | LINEAR-04 | — | N/A | unit | `uv run pytest tests/core/test_runtime_mode_dispatch.py::test_auto_falls_back_divergent -x` | No | pending |
| 13-04-01 | 04 | 0 | LINEAR-05, TBEVAL-01 | T-13-05 | Benchmark arms isolated by separate `ATELIER_ROOT` values | integration | `uv run pytest benchmarks/linear_vs_per_agent/tests/test_runner.py -q` | No | pending |
| 13-04-02 | 04 | 0 | LINEAR-05, TBEVAL-01 | T-13-05 | Benchmark report records cost, wall time, cache-hit ratio, minify delta, and task success | integration | `uv run pytest benchmarks/linear_vs_per_agent/tests/test_reporter.py -q` | No | pending |
| 13-04-03 | 04 | 3 | LINEAR-05 | T-13-05 | Benchmark artifact proves >=30% cost and >=25% wall-time reduction with equal-or-better success | benchmark | `uv run python -m benchmarks.linear_vs_per_agent.runner --out docs/plans/phase-linear-cache-reuse/results/` | No | pending |
| 13-R-01 | regression | each wave | regression | — | Dirty user changes preserved | regression | `uv run pytest tests/core/test_capabilities_production.py -q` | Yes | pending |
| 13-R-02 | regression | each wave | regression | — | Existing code-context behavior unaffected | regression | `uv run pytest tests/core/test_code_context.py -q` | Yes | pending |
| 13-R-03 | regression | each wave | regression | — | MCP/gateway surfaces unbroken | regression | `uv run pytest tests/gateway/test_mcp_tool_handlers.py tests/gateway/test_p0_mcp_surfaces.py -q` | Yes | pending |

---

## Wave 0 Requirements

- [ ] `tests/core/test_phase_runner.py` — stubs for LINEAR-01 and LINEAR-02.
- [ ] `tests/core/test_phase_runner_minify.py` — stubs for LINEAR-03 reader/writer profile distinction and telemetry.
- [ ] `tests/core/test_minify_source.py` — stubs for LINEAR-03 Python, YAML, and generic minification preservation.
- [ ] `tests/core/test_runtime_mode_dispatch.py` — stubs for LINEAR-04 explicit and auto mode dispatch.
- [ ] `benchmarks/linear_vs_per_agent/tests/` — fast runner and reporter shape tests for LINEAR-05 and TBEVAL-01.
- [ ] Fake provider fixture for `PhaseRunner` tests that records messages and scripted cache telemetry.
- [ ] Dirty diff snapshot for `context_reuse/capability.py`, `runtime/engine.py`, and `tests/core/test_capabilities_production.py`.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Linear-vs-per-agent benchmark artifact meets threshold on representative scenarios | LINEAR-05 | The artifact is a benchmark/proof output rather than a unit test assertion during every task | Run `uv run python -m benchmarks.linear_vs_per_agent.runner --out docs/plans/phase-linear-cache-reuse/results/` and verify the report shows >=30% lower cost and >=25% lower wall-time at equal-or-better task success. |

---

## Validation Sign-Off

- [x] All tasks have automated verify commands or Wave 0 dependencies.
- [x] Sampling continuity: no 3 consecutive tasks without automated verify.
- [x] Wave 0 covers all missing references.
- [x] No watch-mode flags.
- [x] Feedback latency target < 60 seconds for focused task checks.
- [x] `nyquist_compliant: true` set in frontmatter.

**Approval:** pending
