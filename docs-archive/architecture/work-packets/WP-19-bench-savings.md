---
id: WP-19
title: Extend `benchmark-runtime` with `--measure-context-savings` and 11-prompt suite
phase: E
pillar: 3
owner_agent: atelier:code
depends_on: [WP-14, WP-21, WP-22, WP-23, WP-24]
status: done
---

# WP-19 — Benchmark harness for context savings

## Why

This packet **proves** the >50 % claim with a CI-asserted, deterministic benchmark. Without it the
V2 plan is marketing.

## Files touched

- `benchmarks/swe/savings_bench.py` — new
- `benchmarks/swe/prompts_11.yaml` — new (the 11-prompt suite)
- `benchmarks/swe/run_swe_bench.py` — edit (add `--measure-context-savings` flag)
- `Makefile` — edit (add `bench-savings` target)
- `tests/infra/test_context_savings_50pct.py` — new (CI gate)
- `docs/benchmarks/v2-context-savings.md` — new (results + methodology)

## How to execute

1. **Suite design.** 11 prompts that span: bug fix, refactor, schema migration, new endpoint, test
   write, doc edit, multi-file edit, search, large-file outline, repeated read, summarize. Each
   prompt has a deterministic mocked LLM response (no network, no API key) so the benchmark is
   reproducible.

2. **Methodology.**
   - Run **A**: `ATELIER_DISABLE_ALL=1` — vanilla path, no Atelier capabilities
   - Run **B**: defaults — all V2 levers active
   - Both runs use the same mocked LLM, same prompts, same seed
   - For each prompt record: turns, total input tokens, total cache-read tokens, total output tokens
   - Compute `reduction_pct = (A.input - B.input) / A.input * 100`

3. **CI gate** in `tests/infra/test_context_savings_50pct.py`:

   ```python
   def test_context_savings_at_least_50_percent_on_11_prompt_suite(tmp_path):
       result = run_savings_bench(tmp_path)
       assert result.reduction_pct >= 50.0, (
           f"context savings regressed: &#123;result.reduction_pct:.1f&#125;% < 50%"
       )
   ```

4. **Results doc.** Publish a markdown summary including per-lever attribution and per-prompt
   table, like the existing `docs/benchmarks/phase7-2026-04-29.md`.

5. Make target:

   ```make
   bench-savings:
   	LOCAL=1 uv run python -m benchmarks.swe.savings_bench --json
   ```

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/infra/test_context_savings_50pct.py -v
make bench-savings | tee /tmp/savings.json
python -c "import json; r=json.load(open('/tmp/savings.json')); assert r['reduction_pct'] >= 50.0"
make verify
```

## Definition of done

- [ ] 11-prompt suite is deterministic and runs without network
- [ ] CI gate enforces ≥ 50 % reduction
- [ ] Results doc published with per-lever and per-prompt attribution
- [ ] `make bench-savings` runs in < 90 s on a laptop
- [ ] `INDEX.md` updated; trace recorded
