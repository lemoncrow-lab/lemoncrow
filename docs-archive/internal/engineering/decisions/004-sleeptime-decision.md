# ADR 004: Sleeptime Summarization Boundary

## Status

Accepted for V3.

## Context

Template group-by summaries were useful during early development, but they look like production reasoning while discarding important context. V3 introduces optional internal LLM support through local Ollama and optional Letta memory recall.

## Decision

Production sleeptime compression must use a real summarization path:

- Prefer the internal Ollama summarizer when available.
- Use Letta summarization when Letta is the configured memory backend and a Letta summarization path is available.
- If neither path is available, raise `SleeptimeUnavailable` and skip archival sleeptime summaries.
- Deterministic group-by summaries remain available only as test helpers.

## Consequences

Context compression can fail closed instead of storing misleading summaries. Offline tests remain deterministic through explicit helper functions and mocks.
