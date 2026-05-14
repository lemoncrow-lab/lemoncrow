# Beseam Benchmark

Beseam is the internal reasoning benchmark originally developed alongside Atelier. It focuses on multi-step enterprise workflows where safety, rollbacks, and procedural compliance are just as important as functional correctness.

## Running the Benchmark

```bash
atelier benchmark run --suite beseam --agent claude-code
```

## Focus Areas
Unlike SWE-bench which focuses on isolated issue resolution in Python codebases, Beseam tests:
1. **Infrastructure Operations**: e.g., Safely scaling a Kubernetes deployment.
2. **Data Mutations**: e.g., Updating Shopify metafields with proper snapshots.
3. **External API Compliance**: e.g., Adhering to strict third-party rate limits.

## Why Beseam?
Beseam proves that an agent can follow organizational *procedures* (ReasonBlocks) rather than just writing syntactically valid code. It heavily utilizes Atelier's Rubric Gates.
