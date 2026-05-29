# Linear-vs-per_agent Benchmark — 2026-05-29

**Run ID:** `2026-05-29`
**Scenarios:** 7 (6 context-sharing + 1 divergent)
**Repetitions:** 3 per cell (42 cells total)
**Provider:** Deterministic offline fake (`benchmarks.linear_vs_per_agent.runner._DeterministicProvider`)

## Outcome

| Metric | Linear (avg) | Per-agent (avg) | Reduction |
| --- | --- | --- | --- |
| Cost (USD) | — | — | **37.11 %** |
| Wall time (ms) | — | — | **39.76 %** |
| Task success rate | 1.00 | 1.00 | equal |

* **`thresholds.cost_pass`:** `true` (≥30 % target ✅)
* **`thresholds.wall_time_pass`:** `true` (≥25 % target ✅)
* **`thresholds.success_at_least_equal`:** `true` ✅

Per-scenario deltas are uniform because the deterministic provider is
identical across scenarios up to `base_cost_factor` scaling — the savings
ratio is invariant under that scaling. Real-provider runs will produce
non-uniform deltas.

## D-17 Savings Decomposition

| Component | Tokens | USD |
| --- | --- | --- |
| Cache-reuse savings | 27 740 | 0.074 898 |
| Minification savings | 6 050 | 0.018 150 |
| **Total** | **33 790** | **0.093 048** |

* Cache savings: linear-arm cache_read tokens charged at the discounted
  rate vs the per_agent baseline's full prefill (engine
  `_run_per_agent` pins `cache_read_tokens=0`, D-14).
* Minification savings: per-scenario `synthetic_minify_delta_tokens` from
  `scenarios.yaml` plus any real `PhaseRunner.cache_stats.minify_deltas`
  (none in this offline run because no `read_tool` is wired in the
  default factory; production wiring lands in the engine's read-tool
  pipeline).

## Divergent Scenario (AUTO → per_agent)

`divergent_subcontexts` carries `expected_mode: per_agent` and
`divergence_signal: true`. It is included in the cell sweep but
**excluded from the headline threshold check** (T-13-03 — the divergent
case is expected to favor per_agent, so including it would falsely
penalize linear). Validates the AUTO fallback path from 13-03 (test
`13-03-04`) at the benchmark level.

## Caveats

1. **Deterministic offline provider.** The benchmark uses a synthetic
   provider so it runs hermetically in CI with no API cost. Token counts
   and pricing coefficients (`_PRICE_IN=3e-6`, `_PRICE_OUT=15e-6`,
   `_PRICE_CACHE_READ=0.3e-6`, USD/token) are indicative; absolute USD
   values are illustrative only. Reduction *ratios* are invariant under
   proportional scaling and remain meaningful.
2. **Wall time is simulated** from token counts via fixed coefficients
   so the offline benchmark produces a deterministic non-zero wall-time
   delta. The `real_wall_time_ms` field in each raw cell captures the
   true `time.monotonic()` elapsed in-process if a later run wishes to
   re-aggregate against wall clock.
3. **Synthetic minify deltas.** Read-context minification (Plan 13-02)
   needs a wired `read_tool` to populate
   `PhaseCacheStats.minify_deltas` from real bodies. Until that wiring
   lands in the engine, the runner attributes each scenario's declared
   `synthetic_minify_delta_tokens` to the linear arm only, which is
   sufficient to exercise the D-17 decomposition.

## Reproducing this run

```bash
DATE=$(date +%Y-%m-%d)
OUT=docs/plans/phase-linear-cache-reuse/results/$DATE
mkdir -p $OUT/raw
uv run python -m benchmarks.linear_vs_per_agent.runner --out $OUT --reps 3
uv run python -c "
import json, pathlib, yaml
from benchmarks.linear_vs_per_agent.reporter import compute_report
out = pathlib.Path('$OUT')
scenarios = yaml.safe_load(pathlib.Path('benchmarks/linear_vs_per_agent/scenarios.yaml').read_text())['scenarios']
meta = {s['id']: s['expected_mode'] for s in scenarios}
r = compute_report(out.name, out / 'raw', scenarios_meta=meta)
(out / 'report.json').write_text(json.dumps(r, indent=2, default=str))
print(json.dumps(r['thresholds'], indent=2))
"
```

## Files

* `report.json` — full aggregate report (cells, deltas, savings, thresholds).
* `raw/*.json` — 42 per-cell payloads (7 scenarios × 2 modes × 3 reps).
* `raw/roots/` — per-arm `ATELIER_ROOT` workspaces (T-13-05 isolation evidence).
* `config.json` — runner CLI snapshot.

## References

* `.planning/phases/13-phase-linear-cache-reuse-agent/13-04-PLAN.md`
* `docs/plans/phase-linear-cache-reuse/02-DESIGN-SPEC.md` §5 (success criteria D-15, D-16, D-17)
