---
phase: 5
slug: scale-decision-and-extended-retrieval-reach
status: planned
created: 2026-05-19
source: phase-planning
---

# Phase 5 - Validation Strategy

> Per-phase validation contract for the M18 decision gate, large-repo `search` routing, and additive literal-only cross-language edges.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest + existing repo benchmark/test layout |
| **Config file** | `pyproject.toml` |
| **Quick run command** | See wave-specific commands below |
| **Full suite command** | `make lint && make typecheck && make test` |
| **Estimated runtime** | ~30-300 seconds |

---

## Wave Order

| Wave | Plans | Why |
|------|-------|-----|
| **Wave 1** | `05-01`, `05-03` | Run the M18 gate and the independent M17 literal-edge work in parallel because neither touches the same files and only M16 is gated by the checkpoint. |
| **Wave 2** | `05-02` | Start scale-backend implementation only after `05-01` ratifies the Zoekt/search-first path; otherwise stop and replan. |

---

## Sampling Rate

- **After every task commit:** run the smallest targeted pytest subset for the task's owned files only.
- **After every wave:** run that wave's quick command only; do not pull later-wave suites forward.
- **Before final phase verification:** run `make lint && make typecheck && make test`, tracking unrelated pre-existing failures separately.
- **Max feedback latency:** 300 seconds.

---

## Per-Plan Verification Map

| Plan | Milestone | Requirement | Secure / correct behavior | Expected automated coverage |
|------|-----------|-------------|---------------------------|-----------------------------|
| `05-01` | M18 checkpoint | `ENBL-03` | M18 matrix and memo are completed from executable rubric data, and the memo explicitly records whether the backend serves `search`, `code`, or both plus whether `05-02` is unblocked or must be replanned | `tests/benchmarks/code_intel/test_scale_decision_eval.py` |
| `05-02` | M16 closeout | `SCAL-01` | large-repo text search routes through the selected backend on the existing `search` stack, exposes additive backend metadata, preserves fallback, and leaves `engine.py` / `mcp_server.py` untouched | `tests/infra/code_intel/zoekt/test_zoekt_routing.py`, `tests/gateway/test_p0_mcp_surfaces.py`, `tests/benchmarks/code_intel/test_zoekt_bench.py` |
| `05-03` | M17 closeout | `SCAL-02` | literal-only static cross-language edges are stored and surfaced additively on `code op="symbol"` and `code op="usages"` with confidence tagging and no Phase 6 scope bleed | `tests/infra/code_intel/cross_lang/test_edges.py`, `tests/infra/code_intel/cross_lang/test_resolvers.py`, `tests/core/test_code_context.py`, `tests/benchmarks/code_intel/test_cross_lang_bench.py` |

---

## Wave-Specific Quick Commands

| Wave | Command |
|------|---------|
| **Wave 1 / 05-01** | `uv run pytest tests/benchmarks/code_intel/test_scale_decision_eval.py -q` |
| **Wave 1 / 05-03** | `uv run pytest tests/infra/code_intel/cross_lang/test_edges.py tests/infra/code_intel/cross_lang/test_resolvers.py tests/core/test_code_context.py tests/benchmarks/code_intel/test_cross_lang_bench.py -k "cross_lang or ctypes or cffi or import_module or subprocess" -q` |
| **Wave 2 / 05-02** | `uv run pytest tests/infra/code_intel/zoekt/test_zoekt_routing.py tests/gateway/test_p0_mcp_surfaces.py tests/benchmarks/code_intel/test_zoekt_bench.py -k "zoekt or backend or search" -q` |

---

## Benchmark Expectations

| Plan | Benchmark expectation |
|------|-----------------------|
| `05-01` | The evaluation harness must emit a deterministic scorecard and a default routing decision that clearly distinguishes `search`, `code`, and `both`. |
| `05-02` | Warm large-repo query benchmark shows at least **10x** faster latency versus the current ripgrep-backed flow, and the shipped public result includes additive `backend` plus `index_age_seconds`. |
| `05-03` | Fixture benchmark proves one resolved literal edge and one unresolved low-confidence edge without blowing the response budget or replacing existing local references. |

---

## Shared Execution Requirements

- [ ] `05-01` must record the explicit `search`/`code`/`both` routing choice before `05-02` starts.
- [ ] Keep M16 on the existing `search` stack first; do not replace `SymbolIntelStore` or widen `engine.py` / `mcp_server.py`.
- [ ] Keep M17 additive on `code op="symbol"` and `code op="usages"` only.
- [ ] Keep Phase 5 cross-language support limited to literal-only static edges.
- [ ] Do not absorb Phase 6 `scope="external"` or multi-repo behavior into any Phase 5 plan.
- [ ] Treat broad repo failures outside these targeted suites as informational unless directly caused by Phase 5 changes.

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Ratify the M18 decision gate | `ENBL-03` | The harness proves evidence, but a maintainer must acknowledge whether `05-02` is valid or needs replanning | Read the completed appendix in `docs/plans/active/code-intel/M18-bvi-checkpoint.md` and select the checkpoint option recorded in `05-01`. |
| Confirm hotspot containment after M16 implementation | `SCAL-01` | Targeted tests prove behavior, not file-placement discipline | Review the final diff and confirm `engine.py` and `mcp_server.py` were not used for Zoekt routing; only the search stack plus `infra/code_intel/zoekt/` changed. |
| Exercise a repeated large-repo search after Wave 2 | `SCAL-01` | Benchmarks prove speed, but operators still need to inspect payload usefulness | Run the shipped `search` path twice against a large fixture repo and confirm the response includes `backend="zoekt"` plus `index_age_seconds`, while a smaller repo falls back cleanly. |
| Exercise `code op="symbol"` and `code op="usages"` on a known literal cross-language fixture | `SCAL-02` | Automated tests prove correctness, but humans should confirm the additive payload is understandable | Query a fixture symbol with known ctypes/cffi, subprocess, or dynamic-import edges and confirm `cross_lang_refs`, `edge_kind`, and `confidence` are visible without replacing the normal symbol/usages output. |

---

## Wave Trace Evidence

- `05-01` closes with a recorded trace tied to `docs/plans/active/code-intel/M18-bvi-checkpoint.md`.
- `05-02` closes with a recorded trace tied to `docs/plans/active/code-intel/M16-zoekt-scale.md`.
- `05-03` closes with a recorded trace tied to `docs/plans/active/code-intel/M17-cross-lang.md`.

---

## Source Coverage Audit

| Source Type | Item | Covered By | Status |
|-------------|------|------------|--------|
| GOAL | Atelier makes the scale-backend choice explicitly before extending large-repo search and supported cross-language edges | `05-01`, `05-02`, `05-03` | covered |
| REQ | `ENBL-03` documented build-vs-integrate decision record before large-repo backend work | `05-01` | covered |
| REQ | `SCAL-01` validated large-repo backend routing | `05-01`, `05-02` | covered |
| REQ | `SCAL-02` supported cross-language reference edges with confidence scoring | `05-03` | covered |
| RESEARCH | M18 must decide whether the scale backend serves `search`, `code`, or both before implementation | `05-01` | covered |
| RESEARCH | M16 defaults to text-search backend routing first, not a naive `SymbolIntelStore` replacement | `05-01`, `05-02` | covered |
| RESEARCH | Backend lifecycle must live outside ephemeral `CodeContextEngine` instances | `05-01`, `05-02` | covered |
| RESEARCH | Keep `engine.py` and `mcp_server.py` hotspot scope explicit and narrow | `05-02` manual hotspot review, `05-03` plan constraints | covered |
| RESEARCH | M17 is limited to literal-only static cross-language edges | `05-03` | covered |
| RESEARCH | Cross-language stays additive on `code op="symbol"` and `code op="usages"` | `05-03` | covered |
| RESEARCH | Do not absorb Phase 6 `scope="external"` or multi-repo scope into Phase 5 | `05-03`, shared execution requirements | covered |
| RESEARCH | Prefer targeted validation because broad repo tests are noisy | all plans via quick commands | covered |
| CONTEXT | No Phase 5 `CONTEXT.md` file was provided; use ROADMAP + RESEARCH defaults only | all plans | covered |

---

## Validation Sign-Off

- [x] Wave 1 M18 gate completed and decision recorded
- [x] Wave 1 M17 literal-edge work completed with targeted tests and benchmark smoke
- [x] Wave 2 M16 search-backend routing completed after the M18 gate
- [x] Manual hotspot and payload checks recorded

**Approval:** approved after real-runtime verification, exact M16 validation-row pass, and recorded human/UAT sign-off
