# ADR-002: Quality-Aware Routing For Coding Agents

## Status

Accepted.

ADR-001 established the V2 memory, ReasonBlock, and context-savings architecture. This ADR adds the
missing routing decision: routing must preserve coding-agent quality, not merely minimize provider
price.

## Context

Provider routers and model gateways can choose among model deployments by price, latency, rate
limits, or fallback availability. That is useful infrastructure, but it does not answer the coding
question that matters: can this specific agent step be done cheaply without reducing final task
success?

Coding agents fail through bad patches, incomplete repo evidence, unverified assumptions, oversized
diffs, repeated failed attempts, and skipped tests. A low-cost model call that creates any of those
outcomes is not a saving. It is deferred cost.

The research direction is clear enough to adopt a policy. Context control should come before
routing. Observation masking, selective summarization, prompt caching, repo-aware retrieval, and
procedure memory reduce the need for premium calls. Routing then decides which remaining steps need
premium judgment.

## Decision

Atelier will implement routing as a quality-aware runtime layer. The router classifies individual
steps, assigns risk, consults memory/procedure confidence, chooses a model tier or deterministic
tool path, and records the decision for evaluation.

The router must be verification-gated. It can start with a cheap or mid-tier model only when the
task scope, risk, available memory, and verifier coverage make that route defensible. It must
escalate to a premium model when risk rises, evidence conflicts, tests fail repeatedly, or the
verifier cannot establish success.

Atelier may integrate with provider-routing tools later, but those tools are execution backends.
The coding-specific policy lives in Atelier because it depends on ReasonBlocks, run ledgers,
rubrics, repo retrieval, and trace outcomes.

Provider prices and model identifiers are configuration data. They must not be embedded in policy
logic. This keeps the router stable when vendors change prices, model names, tokenization, or cache
semantics.

## Alternatives Considered

### Use Provider Routing Only

Rejected. Provider routers can optimize price, latency, and failover, but they cannot determine
whether a risky migration, auth patch, or ambiguous failing test needs premium reasoning.

### Always Start Cheap And Escalate On Failure

Rejected. Some domains are too expensive to fail in the first place. Security, auth, payments,
publishing, data migrations, and compliance changes should start premium unless a domain-specific
policy explicitly permits a cheaper route.

### Always Use Premium For Code Edits

Rejected. Many coding steps are classification, summarization, retrieval, formatting, commit
message drafting, or mechanical edits with deterministic verification. Keeping those on premium
models wastes budget and reduces throughput.

## Consequences

Routing decisions become observable trace data. Every routed step must record the selected tier,
reason, risk level, confidence, and verifier outcome.

Verification becomes part of cost accounting. A task only counts as cheap success if the verifier
passes and the human or benchmark accepts the result.

The runtime needs new configuration and eval surfaces: model tiers, route policies, escalation
thresholds, protected file patterns, and cost-quality reports.

The router must never store hidden chain-of-thought. It stores decisions, evidence pointers, test
results, and concise explanations.

## References

- [Agent Cost-Performance Runtime](../../../architecture/cost-performance-runtime.md)
- [Atelier V2 - Implementation Plan](../../../architecture/IMPLEMENTATION_PLAN_V2.md)
- [Anthropic prompt caching](https://platform.claude.com/docs/en/docs/build-with-claude/prompt-caching)
- [JetBrains Research: smarter context management for agents](https://blog.jetbrains.com/research/2025/12/efficient-context-management/)
- [Google Research: ReasoningBank](https://research.google/blog/reasoningbank-enabling-agents-to-learn-from-experience/)
