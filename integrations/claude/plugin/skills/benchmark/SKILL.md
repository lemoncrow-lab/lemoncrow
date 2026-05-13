---
description: "Use when: running Atelier benchmarks, evals, savings benches, or comparing runtime behavior."
allowed-tools: "Bash(atelier benchmark *)"
---

Run the Atelier benchmark flow.

1. Default to dry-run behavior and run `atelier benchmark run --json` unless the user requested specific benchmark arguments.
2. If the user asks to apply or mutate targets, ask for confirmation before running the mutating command.
3. Render pass/fail totals and list failing case ids.

Do not run destructive benchmark modes without explicit confirmation.
