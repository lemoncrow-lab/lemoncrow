---
id: WP-50
title: Publish V3 honest benchmark; replace the 81 % story
phase: J
boundary: Cleanup
owner_agent: atelier:code
depends_on: [WP-34, WP-39, WP-47]
supersedes: [WP-19]
status: done
---

# WP-50 — Honest benchmark publication

## Why

WP-34 retracted the 81 % savings headline and gated the README against unmeasured claims.
WP-50 _replaces_ it with a real measurement of how much Atelier's MCP tools change a host's
token budget on a fixed corpus of tasks.

**Atelier does not run the benchmark by executing tasks itself.** Atelier is a tool/data
provider; it does not call LLMs. The benchmark is run by a _host harness_ — a small Python
script that simulates a host CLI's tool dispatch — using a deterministic mocked LLM over a
recorded transcript corpus. Atelier's MCP tools are dispatched by the harness exactly as a
real host would dispatch them.

Methodology:

- Record a corpus of ≥ 50 transcripts of host-driven tasks (bug fix, refactor, schema
  migration, multi-file edit, search, summarize, doc edit, etc.). Each transcript is the
  _recorded sequence of tool calls and responses_ that a real host CLI made when solving
  that task. The corpus is committed to the repo; no production data is used.
- Re-play each transcript under two configurations:
  - **Baseline (Run A):** every Atelier tool call is replaced with the host-native equivalent
    (`grep` instead of `search`, sequential `Edit` instead of
    `edit`, full file reads instead of AST outline). The mocked LLM consumes
    the resulting tool outputs and produces the same final response.
  - **V3 (Run B):** every Atelier tool call goes through Atelier's MCP server normally.
- Compare total input tokens consumed by the mocked LLM, total tool round-trips, and final
  output. Compute `reduction_pct = (A.input_tokens - B.input_tokens) / A.input_tokens * 100`.
- Publish the result and a per-lever attribution.

## Files touched

- **NEW:** `benchmarks/swe/replay_corpus/` — directory of ≥ 50 recorded host-task transcripts
  (JSONL). Each transcript has: task description, ordered tool-call/response pairs (with both
  Atelier and host-native variants), final output. Synthetic but realistic; no PII.
- **NEW:** `benchmarks/swe/savings_replay.py` — the V3 replay harness:
  - Loads the replay corpus.
  - For each transcript, runs Run A (host-native tools only) and Run B (Atelier MCP tools
    where the recorded transcript used them) using a deterministic mocked LLM.
  - Counts input tokens (via `tiktoken` or equivalent) on each side.
  - Writes a `BenchmarkRun` row + per-prompt rows to the benchmark DB.
  - Outputs JSON summary: median reduction %, per-lever attribution, per-prompt table.
- **EDIT:** `Makefile` — add `bench-savings-honest` target.
- **NEW:** `tests/infra/test_savings_replay.py` — CI gate:
  - Asserts the harness completes without error on the full corpus.
  - Asserts the recorded `BenchmarkRun` row has `reduction_pct` (any value, no threshold).
  - Asserts no regression vs. the previous `BenchmarkRun` of the same suite — `reduction_pct`
    must not drop by more than 5 % from the prior baseline.
- **NEW:** `docs/benchmarks/v3-honest-savings.md` — the published results doc:
  - Methodology in detail (including the explicit statement: "Atelier does not call LLMs;
    the harness does, with a mocked deterministic provider, simulating a host CLI's tool
    dispatch loop").
  - Per-lever attribution from the replay (real, not hand-written).
  - Per-prompt table.
  - Replay results published as a CSV alongside the doc.
  - Honest discussion of confounders and limitations.
- **EDIT:** `README.md` — replace the (already-retracted-by-WP-34) headline with the
  measured number from the latest `BenchmarkRun`. Footnote links to the doc and CSV.
- **EDIT:** `docs/benchmarks/v2-context-savings.md` — extend the WP-34 banner with the
  WP-50 link.

## How to execute

1. **Build the replay corpus first.** This is the hardest part. A corpus that:
   - Is large enough to be representative (≥ 50 tasks across the categories above).
   - Is deterministic — the mocked LLM produces the same response for the same prompt.
   - Includes both host-native-tool and Atelier-tool variants for each tool call so Run A
     and Run B are directly comparable.
   - Carries no PII or production data — synthetic seeds only.
   - Is committed to the repo; the corpus version is bumped when the corpus changes.

2. **Implement the harness.** Critical contract: **the harness simulates a host CLI's tool
   dispatch.** It calls Atelier MCP tools the same way Claude Code or Codex would. The
   harness reads the recorded transcript, replays the LLM responses, dispatches the tool
   calls, and counts tokens. Atelier itself never makes an LLM call.

3. **Persist results in `BenchmarkRun`.** Per data-model § 3. Per-prompt rows live in a
   `benchmark_prompt_result` table (FK to BenchmarkRun.id).

4. **Publish honestly.**
   - The doc shows real numbers from a real run.
   - If the measured reduction is 38 % rather than 81 %, that's the headline.
   - If a "lever" shows zero or negative attribution, document it (and consider proposing a
     follow-up packet to deprecate it).

5. **Set the no-regression gate.** Future PRs cannot land if `reduction_pct` drops by more
   than 5 % vs. the prior measured baseline. This replaces V2's fictional `≥ 50 %`
   assertion with an honest "no regression" guardrail.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier

LOCAL=1 uv run pytest tests/infra/test_savings_replay.py -v

make bench-savings-honest | tee /tmp/savings.json
python -c "
import json
r = json.load(open('/tmp/savings.json'))
assert 'reduction_pct' in r and isinstance(r['reduction_pct'], (int, float))
print('measured reduction:', r['reduction_pct'])
"

make verify
```

## Definition of done

- [ ] Replay corpus committed (≥ 50 host-task transcripts, deterministic, no PII).
- [ ] `bench-savings-honest` Makefile target produces a `BenchmarkRun` row.
- [ ] `docs/benchmarks/v3-honest-savings.md` published with real numbers, per-lever
      attribution, methodology, limitations.
- [ ] Doc explicitly notes that the harness simulates a host CLI's loop and that Atelier
      itself does not call any LLM.
- [ ] README headline replaced with the measured number; links to doc and CSV.
- [ ] No-regression CI gate active (5 % drop budget).
- [ ] WP-34's banner on V2 benchmark docs extended with a link to this packet's published
      results.
- [ ] `make verify` green.
- [ ] V3 INDEX updated. Trace recorded with the measured `reduction_pct`.
