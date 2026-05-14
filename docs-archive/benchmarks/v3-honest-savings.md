# V3 Honest Context-Savings Replay

WP-50 replaces the V2 constant-subtraction benchmark with a deterministic replay harness over recorded host transcripts.

## Methodology

The replay corpus lives in `benchmarks/swe/replay_corpus/` and contains 50 synthetic software-engineering transcripts across bug fixes, refactors, migrations, endpoints, tests, documentation, search, summaries, multi-file edits, and large-file inspections.

For each transcript, the harness counts input tokens for two recorded paths:

- Baseline: host-native output copied into model context.
- Atelier: focused tool output after one context-saving lever is applied.

The harness uses `tiktoken` with `cl100k_base`, writes run metadata into `benchmark_run`, and writes per-prompt rows into `benchmark_prompt_result` with lever attribution.

## Latest Run

Generated with `make bench-savings-honest` on 2026-05-05. Latest persisted run id: `bench-019df7de-dc87-7829-b9f2-99efaddb98c0`.

| Metric                         |  Value |
| ------------------------------ | -----: |
| Replay prompts                 |     50 |
| Median baseline input tokens   |     14 |
| Median Atelier input tokens    |     12 |
| Measured input-token reduction | 13.27% |

Per-lever token attribution from the same run:

| Lever                 | Tokens saved |
| --------------------- | -----------: |
| `batch_edit`          |           24 |
| `search_read`         |           17 |
| `memory_recall`       |           15 |
| `smart_read`          |           12 |
| `ast_truncation`      |            9 |
| `compact_tool_output` |            7 |
| `sql_inspect`         |            7 |
| `repo_map`            |            6 |

The per-prompt table is published as [v3-honest-savings-results.csv](v3-honest-savings-results.csv).

## Reproduce

```bash
make bench-savings-honest
```

This also writes [v3-honest-savings-results.csv](v3-honest-savings-results.csv) for inspection.

## Interpretation

The benchmark is synthetic and deterministic. It is suitable for comparing code paths and preventing accidental measurement regressions. It is not provider billing data and does not claim real user productivity gains.

The important acceptance properties are:

- At least 50 replay prompts are present.
- Baseline and optimized token counts are computed from transcript text, not hard-coded percentages.
- Aggregate and per-prompt rows are persisted for auditability.
- Lever attribution remains visible per prompt.
