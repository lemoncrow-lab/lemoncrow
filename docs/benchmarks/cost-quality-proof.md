# Cost-Quality Proof Gate — Methodology

**Work Packet:** WP-32  
**Phase:** G (Host Contract And Proof Gate)

## Overview

The cost-quality proof gate is the final release gate for Atelier V2. It
combines evidence from five prior work packets into one auditable report that
proves near-premium coding outcomes at lower total cost.

The key claim is not "fewer tokens used." It is:

> **Near-premium patch acceptance rate, at lower total cost, with a trace
> that proves what happened — separated by host-native versus Atelier-attributed
> capability.**

## Evidence sources

| Source                   | Work Packet | What it contributes                                                        |
| ------------------------ | ----------- | -------------------------------------------------------------------------- |
| Context savings report   | WP-19       | `context_reduction_pct` (must be ≥ 50%)                                    |
| Routing eval summary     | WP-28       | `cost_per_accepted_patch`, `routing_regression_rate`, `cheap_success_rate` |
| Host capability contract | WP-29       | Per-host feature boundary labels                                           |
| Trace confidence report  | WP-30       | Per-host trace confidence level                                            |
| Route execution contract | WP-31       | Per-host enforcement mode (advisory / wrapper_enforced)                    |

## Gate thresholds

All thresholds must pass for the report to have `status: pass`.

| Threshold                 | Limit                          | Rationale                                                    |
| ------------------------- | ------------------------------ | ------------------------------------------------------------ |
| `context_reduction_pct`   | ≥ 50.0%                        | WP-19 baseline — must reduce context by at least half        |
| `cost_per_accepted_patch` | < premium-only baseline        | Atelier routing must beat a pure-premium strategy            |
| `accepted_patch_rate`     | ≥ premium-only baseline − 0.03 | Routing must not sacrifice acceptance rate                   |
| `routing_regression_rate` | ≤ 2.0%                         | Cheap routing failures must not cause observable regressions |
| `cheap_success_rate`      | ≥ configured min (default 60%) | Cheap tier must handle the majority of tasks it is routed to |
| trace evidence            | every case has `trace_id`      | Every claim must link to observable evidence                 |

## Implementation boundary

Each benchmark case is labelled with a boundary:

- **Host-native:** The host (Claude Code, Codex CLI, etc.) owns this capability.
  Atelier cannot claim savings or enforcement from it.
- **Atelier augmentation:** Atelier provides this via MCP tools, wrapper, or hooks.
  Savings and enforcement here are legitimately Atelier-attributed.
- **Future-only:** Not available at runtime. Must not be claimed.

| Feature                   | Boundary             |
| ------------------------- | -------------------- |
| `context_compression`     | Atelier augmentation |
| `routing_decision`        | Atelier augmentation |
| `verification`            | Atelier augmentation |
| `trace_capture`           | Atelier augmentation |
| `model_selection`         | Host-native          |
| `edit_application`        | Host-native          |
| `compaction`              | Host-native          |
| `agent_orchestration`     | Host-native          |
| `provider_model_override` | Future-only          |

## Failure accounting

Failed cheap attempts count against **all** metrics. They cannot be elided
into a "savings" column:

- Their `cost_usd` contributes to `total_cost`.
- When `accepted_count == 0` for a set of cases, `cost_per_accepted_patch`
  equals `total_cost` (worst case — not zero).
- If `regression=True`, they count against `routing_regression_rate`.

## Proof report

The proof gate writes two files to `.atelier/proof/`:

- `proof-report.json` — machine-readable, all fields.
- `proof-report.md` — human-readable summary with per-host table, metrics table,
  and per-benchmark case evidence links.

### Minimum required fields in `proof-report.json`

```json
&#123;
  "session_id": "...",
  "status": "pass | fail",
  "failed_thresholds": [],
  "context_reduction_pct": 55.0,
  "cost_per_accepted_patch": 0.0125,
  "accepted_patch_rate": 0.80,
  "routing_regression_rate": 0.0,
  "cheap_success_rate": 0.667,
  "host_enforcement_matrix": [...],
  "feature_boundary_labels": &#123;...&#125;,
  "benchmark_cases": [...]
&#125;
```

## Running the proof gate

```bash
# Run the full deterministic proof gate (tests + report generation):
make proof-cost-quality

# Run proof gate tests only:
LOCAL=1 uv run pytest tests/core/test_cost_quality_proof_gate.py \
                     tests/gateway/test_cli_proof_gate.py -v

# Generate the proof report manually:
LOCAL=1 uv run atelier proof run --session-id <id> --json

# Show the last saved proof report:
LOCAL=1 uv run atelier proof report --json

# Assert the report exists and has a valid status:
test -s ~/.atelier/proof/proof-report.json
python -c "import json, os; r=json.load(open(os.path.expanduser('~/.atelier/proof/proof-report.json'))); assert r['status'] in ('pass', 'fail')"
```

## Design notes

1. **Fewer tokens alone cannot pass the gate.** The `context_reduction_pct`
   threshold is necessary but not sufficient. The gate also checks
   `cost_per_accepted_patch`, `accepted_patch_rate`, and `routing_regression_rate`.

2. **Host-native enforcement limits.** `copilot` and `gemini` are `advisory`
   mode — Atelier cannot block tasks or require verification on these hosts.
   The proof report documents this explicitly so users are not misled.

3. **Trace evidence required.** Every benchmark case must carry a `trace_id`.
   Cases without a `trace_id` cause `missing_trace_evidence` to appear in
   `failed_thresholds`, failing the gate.

4. **Deterministic, no network.** The proof gate uses the seeded 11-prompt
   benchmark suite from WP-19 and the deterministic routing eval cases from
   WP-28. No API keys or network access are required.
