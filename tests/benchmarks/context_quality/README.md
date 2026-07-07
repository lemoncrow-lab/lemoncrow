# Context Quality Benchmarks

Internal evaluation suite for the Context Quality Lift milestones (v0.2).
These benchmarks run against a real Atelier installation and require:
- A git repository indexed by `code op="search"` (for M1)
- A working Atelier MCP server (for M2–M4)
- Python ≥ 3.12 in the atelier venv

## Protocol

All benchmarks follow this structure:
1. **Seed** — set up the evaluation fixture (real git repo, pre-seeded commit chunks, etc.)
2. **Run** — call the Atelier capability under test for each query/task
3. **Grade** — compare result against ground truth; score 1 (correct) or 0 (incorrect)
4. **Report** — print per-query verdict + aggregate pass rate

Benchmarks are NOT in the normal pytest suite (they are `@pytest.mark.slow`).
Run them explicitly:

```bash
uv run pytest tests/benchmarks/context_quality/M1_lineage.py -v -m slow
```

## Benchmark Targets

| Milestone | File | Target | Status |
|-----------|------|--------|--------|
| M1 — Context Lineage | `M1_lineage.py` | ≥7/10 | real engine eval (skips until commit_chunks populated) |
| M2 — Routing Replay | `M2_routing.py` | ≥10% estimated input-cost reduction on 50 recorded session traces | **implemented — recorded trace replay** |
| M3 — Counterexample Loop | `M3_verification.py` | ≥0.90 error-detection rate | **implemented — 1.00 (6/6)** |
| M4 — Scoped Pull Context | `M4_scoped.py` | precision ≥0.6 recall ≥0.85 | **implemented — 0.75 / 1.00** |
| M5 — Autopilot Choreography | `M5_autopilot.py` | bounded injection + dedup + correctness | **implemented — pass** |

**Notes on M2/M3/M5 scope.** M2 now replays recorded `model_recommendation`
traces from `live_savings_events.jsonl` (or an exported `route_decisions.json`
bundle) grouped by session, and measures estimated input-cost reduction from the
recorded baseline tier to the chosen tier. It also reports
`tier_downgrades_vs_baseline` as a proxy signal, but that is not the same as a
measured quality regression. The full agent-loop metrics (M3 self-correction
rate, M5 quality-at-≤10%-tokens) still require a live model and are deferred.
The implemented benchmarks measure the deterministic foundations those metrics
depend on: M3's error-detection rate (real ruff + mypy) and M5's
budget/dedup/decision guards. M4 runs against a real `CodeContextEngine` index
over a controlled multi-domain fixture; a real-repo variant labelled from commit
history is future work.

## Scoring

Each query is graded binary: 1 = correct citation/answer, 0 = wrong/hallucinated.
Pass rate = sum(scores) / len(queries).

**Citation correctness for M1:** A result is scored 1 if the top-ranked commit chunk
returned by `code op="search"` has a `commit_sha` matching the expected SHA **or**
the summary text contains at least 2 of the expected keywords from the ground truth.
Exact SHA match is preferred; keyword fallback handles SHA abbreviation differences.

## Adding New Benchmark Queries

1. Find a real commit in the target repo that fixes a concrete, named bug.
2. Formulate a natural-language query that a developer would ask about that bug.
3. Add an entry to the `QUERIES` list with `sha`, `query`, and `keywords` fields.
4. Run `uv run pytest M1_lineage.py -v -m slow` and verify ≥7/10 pass.

## CI Integration

Benchmarks are excluded from `pytest` default runs (`-m 'not slow'` in pyproject.toml).
Run them in a separate CI job after merging Phase 8:

```bash
ATELIER_LLM_BACKEND=openai uv run pytest tests/benchmarks/context_quality/ -v -m slow
```
