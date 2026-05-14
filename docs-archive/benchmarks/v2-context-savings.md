# Atelier V2 Context-Savings Benchmark

> Correction: this historical WP-19 report is deprecated as measurement evidence. The old YAML suite is kept as a smoke harness only; its constants must not be used for public savings claims. Use the V3 honest replay benchmark for reproducible context-savings numbers.

## Current Status

The V2 suite originally modeled savings by subtracting per-lever constants from fixed prompt budgets. That was useful for testing the runner path, but it did not replay host transcripts and should not be presented as observed savings.

The retained code path now answers narrower questions:

- Can the historical YAML fixture still be parsed?
- Does the old result serializer still produce JSON?
- Can trace continuity tests run without network access?

## Replacement

Run the WP-50 replay benchmark instead:

```bash
make bench-savings-honest
```

Methodology and results live in [v3-honest-savings.md](v3-honest-savings.md).

## Reproduction For Historical Smoke Only

```bash
LOCAL=1 uv run pytest tests/infra/test_context_savings_smoke.py -v
```

The smoke tests intentionally do not assert a savings percentage.
