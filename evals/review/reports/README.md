# Benchmark reports

`lc dev eval review benchmark` writes its Markdown and JSON reports here, one pair per run, named
`<corpus>-<driver>-<time>.md` and `.json`. Git ignores them: a report is a measurement of one run on one machine, and a
live one holds real model output, which is never committed. Publish a number by following the checklist in
[docs/reference/review-benchmark.md](../../../docs/reference/review-benchmark.md), not by committing a report.

A customer's corpus writes its reports outside this checkout (`--out-dir`).
