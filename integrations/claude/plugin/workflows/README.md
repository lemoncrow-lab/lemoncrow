# Claude plugin workflows

This directory contains packaged Claude Code dynamic workflows that ship with the
Atelier Claude plugin.

- **Minimum Claude Code version:** `v2.1.154`
- **Feature state:** dynamic workflows are in research preview
- **Discovery:** Claude loads workflow scripts at startup; each `.js` file becomes
  a workflow command and also appears in `/workflows`
- **Verification:** `bash scripts/verify_claude.sh` checks that the packaged
  workflow assets are present before relying on runtime discovery

## Included workflows

### `code-audit.js`

Multi-lens repository audit workflow:

1. security review
2. performance review
3. test/verification review
4. adversarial consolidation into one report

Recommended permission/tool posture for long-running workflow runs:

- allow read/search/git-diff surfaces up front
- keep file edits disabled unless the workflow is explicitly meant to mutate code
- allow MCP/tools needed for repo inspection so the run does not stall on prompts

The packaged workflow contract is intentionally modest: Atelier verifies the
bundled source assets and discovery guidance it owns, while Claude Code runtime
presentation remains a research-preview host surface.

### `gate-benchmark.js`

Benchmark gate workflow:

1. collect evidence from existing benchmark/reporting surfaces
2. prefer repeated paired results when available
3. reject one-off evidence as final proof when rigor is missing
4. return exactly one verdict: `PASS`, `FAIL`, or `INSUFFICIENT_DATA`

Today this workflow is intentionally built on the current repo reality:
`benchmarks/codebench/run.py --report`, the real A/B calibration tests, and
`uv run python -m benchmarks.flowlib.report ...` are valid evidence
surfaces, while deleted `benchmarks/ab/` infrastructure is not.

## Measured cross-check fixture

`fixtures/code-audit-review-fixture.json` is the regression fixture for the
adversarial review pattern. It records a single-pass reviewer output and the
final cross-check output for the same scenario, then lets tests prove that the
cross-check improves precision without reducing recall.
