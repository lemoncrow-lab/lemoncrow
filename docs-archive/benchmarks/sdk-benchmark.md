# SWE-bench Integration

Atelier includes built-in support for running and evaluating agents against SWE-bench, the standard benchmark for software engineering agents.

## Running SWE-bench with Atelier

By wrapping your agent in the Atelier reasoning runtime, you can measure how much procedural reasoning improves SWE-bench resolve rates.

```bash
# Example: Run SWE-bench Lite with Atelier injecting context
atelier benchmark run --suite sdk-benchmark --python
```

## Metrics Tracked
When running SWE-bench through Atelier, the following metrics are recorded:
- **Resolve Rate**: Percentage of issues successfully resolved.
- **Reasoning Cache Hits**: How many times Atelier successfully injected relevant prior context.
- **Cost Savings**: Token and cost reductions achieved through context caching.
- **Rubric Catches**: How many times Atelier blocked the agent from making a known SWE-bench error.

## Exporting Reports
```bash
atelier benchmark report --format json > swe_bench_results.json
```
