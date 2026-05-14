---
id: WP-32
title: Final cost-quality proof gate
phase: G
pillar: proof
owner_agent: atelier:code
depends_on: [WP-19, WP-28, WP-29, WP-30, WP-31]
status: done
---

# WP-32 - Cost-Quality Proof Gate

## Why

The final product claim is not "fewer tokens." It is "near-premium coding outcomes at lower total
cost, with a trace that proves what happened." This packet adds the release gate that combines
context savings, routing evals, host enforcement, trace coverage, and verifier outcomes into one
auditable proof report.

## Implementation boundary

- **Host-native:** the benchmark must respect each host's native execution path and enforcement
  limits instead of pretending all hosts can force model, edit, compaction, or trace behavior.
- **Atelier augmentation:** the proof gate combines host contracts, trace confidence, routing
  outcomes, verifier results, and cost metrics into one auditable report.
- **Not in scope:** do not use synthetic savings from features Atelier does not enforce on a given
  host.

## Files touched

- **Create** `src/atelier/core/capabilities/proof_gate/__init__.py`
- **Create** `src/atelier/core/capabilities/proof_gate/capability.py`
- **Edit** `src/atelier/core/capabilities/__init__.py`
- **Edit** `src/atelier/core/runtime/engine.py`
- **Edit** `src/atelier/gateway/adapters/cli.py` — add `proof run` and `proof report`
- **Edit** `src/atelier/gateway/adapters/mcp_server.py` — expose `atelier proof show`
- **Edit** `Makefile` — add `proof-cost-quality`
- **Create** `tests/core/test_cost_quality_proof_gate.py`
- **Create** `tests/gateway/test_cli_proof_gate.py`
- **Create** `docs/benchmarks/cost-quality-proof.md`

## How to execute

1. Proof inputs:
   - WP-19 context savings report;
   - WP-28 routing cost-quality eval summary;
   - WP-29 host capability contract;
   - WP-30 trace confidence report;
   - WP-31 route execution contract;
   - trace IDs, run IDs, route decisions, verification envelopes, and context budgets.
2. Gate thresholds:
   - `context_reduction_pct >= 50.0`
   - `cost_per_accepted_patch < premium_only_baseline_cost_per_accepted_patch`
   - `accepted_patch_rate >= premium_only_baseline_accepted_patch_rate - 0.03`
   - `routing_regression_rate <= 0.02`
   - `cheap_success_rate >= configured_min_cheap_success_rate`
   - high-risk rubric gates pass
   - every benchmark case links to trace evidence
3. Failed cheap attempts must count against total cost and regression rate. They cannot disappear
   into "savings."
4. Output `proof-report.json` and `proof-report.md` with:
   - per-host enforcement matrix snapshot;
   - per-host trace confidence;
   - per-feature boundary label: `Host-native`, `Atelier augmentation`, or `Future-only`;
   - per-benchmark prompt result;
   - route decisions and verifier outcomes;
   - final pass/fail status and failed threshold names.
5. Add a `make proof-cost-quality` target that runs the deterministic suite without network.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/core/test_cost_quality_proof_gate.py \
                     tests/gateway/test_cli_proof_gate.py -v
make proof-cost-quality
test -s .atelier/proof/proof-report.json
python -c "import json; r=json.load(open('.atelier/proof/proof-report.json')); assert r['status'] in ('pass', 'fail')"
```

## Definition of done

- [x] One command produces the final cost-quality proof report
- [x] The report links every claim to trace/run/eval evidence
- [x] The report separates host-native capability from Atelier-attributed savings
- [x] Fewer tokens alone cannot pass the gate
- [x] Failed cheap attempts count against cost-quality metrics
- [x] `docs/benchmarks/cost-quality-proof.md` explains the methodology
- [x] `INDEX.md` updated; trace recorded
