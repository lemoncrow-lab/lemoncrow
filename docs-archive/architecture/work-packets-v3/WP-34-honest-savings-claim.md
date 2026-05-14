---
id: WP-34
title: Retract or qualify the 81 % savings headline; CI gate against unmeasured claims
phase: Z
boundary: Cleanup
owner_agent: atelier:code
depends_on: []
supersedes: [WP-19]
status: done
---

# WP-34 — Honest savings claim

## Why

`benchmarks/swe/savings_bench.py` does not measure anything. It loads
`benchmarks/swe/prompts_11.yaml`, where `naive_input_tokens` and per-lever savings are
hand-written constants. The README's headline "81 % reduction across major models" multiplies
the same hardcoded ratio against a model price list. The "real-world validation" doc in
`docs/benchmarks/` does the same thing. None of it is a measurement.

This is a credibility liability. A user who reads the V2 plan and tries to verify the headline
will find that the test passes only because the YAML constants are tuned to make the assertion
pass. We need to either:

- (a) **Retract** the headline — change the README to "design target", footnote it, and link to
  the open WP-50 ("publish honest benchmark") — **this packet's path**, OR
- (b) **Replace** the headline with a real measurement — that is WP-50's job.

WP-34 is the _retraction_ and _CI gate_ step. WP-50 is the _replacement_ step. They are split
because the retraction must ship in V3.0 unconditionally; the replacement requires the rest of
Phase H + I to be in place.

## Files touched

- **EDIT:** `README.md` — remove or qualify every percentage claim. Each remaining percentage
  must be either:
  - A V3 design target with an explicit "(design target — see WP-50)" footnote, OR
  - A linked measurement from a `BenchmarkRun` row published after WP-50.
    No bare percentages.
- **EDIT:** `docs/benchmarks/v2-context-savings.md` — add a banner at the top:
  > **2026-05-04 correction:** the "81 %" figure on this page is derived from hand-written
  > YAML constants, not a measurement. See [V3 plan § 0](../IMPLEMENTATION_PLAN_V3.md)
  > and [WP-34](WP-34-honest-savings-claim.md) for the
  > retraction; see [WP-50](WP-50-honest-benchmark-publish.md)
  > for the replacement methodology.
  > Do not delete the page (it is referenced from V2 traces).
- **EDIT:** `docs/benchmarks/phase7-2026-04-29.md` — same banner.
- **NEW:** `tests/docs/test_readme_no_unmeasured_claims.py` — CI gate that scans `README.md`
  and `docs/benchmarks/*.md` for percentage tokens (`/\b\d+(\.\d+)?\s*%/`), and fails for any
  that:
  - Lack a footnote or link in the same paragraph pointing to either a `BenchmarkRun` row or a
    "design target" qualifier, AND
  - Are not inside a fenced code block or quoted historical claim block.
- **NEW:** `tests/docs/test_no_hardcoded_savings_yaml.py` — fails if `benchmarks/swe/prompts_11.yaml`
  contains a `reduction_pct` or equivalent constant. The yaml is allowed to define prompts;
  it may not embed answers.
- **EDIT:** `benchmarks/swe/savings_bench.py` — add a header docstring stating that the file
  is _deprecated for measurement_ until WP-50 lands; the existing assertion remains as a
  smoke test but its threshold is reduced from `≥ 50.0` to `≥ 0.0` (i.e., the test now only
  asserts the runner doesn't crash, not that any savings exist).
- **EDIT:** `tests/infra/test_context_savings_50pct.py` — rename to
  `test_context_savings_smoke.py` and reduce the assertion to `result.reduction_pct >= 0.0`.
  Add a docstring linking to WP-50 for the actual measurement.

## How to execute

1. **Inventory every percentage claim** in the codebase first:

   ```bash
   grep -rn -E '[0-9]+(\.[0-9]+)?\s*%' README.md docs/ | grep -v node_modules
   ```

   Record the list in the PR description.

2. **Categorize each one:**
   - "Measured today" — leave alone (probably none exist).
   - "Design target" — qualify with the footnote.
   - "Fictional" (the 81 %) — remove, or convert to design target.

3. **Edit the README.** The most likely fix is replacing the "81 % reduction across major
   models" line with something like:

   > **Context savings** — Atelier ships deterministic context-savings tools
   > (`search`, `edit`, AST outline-first reads, scoped recall).
   > A measured reduction will be published with V3.0; see
   > [WP-50](WP-50-honest-benchmark-publish.md) for
   > methodology. Until then, treat all percentage figures in this README as design targets.

4. **Edit the existing benchmark docs** with the correction banner. Keep the original numbers
   for trace continuity, but mark them clearly.

5. **Add the CI gates.** The two new tests must run as part of `make verify`. Confirm they
   fail before your edits land (to prove the gate works) and pass after.

6. **Reduce the existing 50 % CI gate** to a smoke test. Do not delete it — that would lose the
   harness scaffolding WP-50 will reuse. Just make it honest about what it asserts.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

# 1. The new doc gates pass.
LOCAL=1 uv run pytest tests/docs/test_readme_no_unmeasured_claims.py \
                     tests/docs/test_no_hardcoded_savings_yaml.py -v

# 2. The renamed smoke test still runs.
LOCAL=1 uv run pytest tests/infra/test_context_savings_smoke.py -v

# 3. Full verify is green.
make verify

# 4. Manual: open README.md, confirm no bare percentages without a footnote.
```

## Definition of done

- [ ] `README.md` contains zero bare percentage claims; every remaining percentage links to a
      measurement or carries a "(design target)" footnote.
- [ ] `docs/benchmarks/v2-context-savings.md` and `docs/benchmarks/phase7-2026-04-29.md` carry
      the 2026-05-04 correction banner.
- [ ] `tests/docs/test_readme_no_unmeasured_claims.py` is wired into `make verify` and passes.
- [ ] `tests/docs/test_no_hardcoded_savings_yaml.py` is wired into `make verify` and passes.
- [ ] `tests/infra/test_context_savings_smoke.py` (renamed from `*_50pct.py`) asserts only the
      smoke condition.
- [ ] `make verify` green.
- [ ] V3 INDEX status updated. Trace recorded with `output_summary` listing every README
      change and the corrected percentage count.
