# Agent Cost-Performance Runtime

Atelier V2 optimizes coding-agent cost and quality by controlling context before routing models.
Routing libraries can choose a provider or model, but they do not know whether a coding step can be
done cheaply without reducing final task success. Atelier owns that policy.

The target is near-premium-model coding performance at lower total cost:

1. Send fewer tokens.
2. Cache stable context.
3. Retrieve repo facts instead of pasting files.
4. Use cheap models only when the step and verification policy allow it.
5. Escalate with compressed evidence when quality risk rises.
6. Store reusable procedures from observed successes and failures.

## Architecture

```text
Agent host / CLI wrapper
        |
        v
Agent gateway
        |
        v
Context compiler
  - pinned policy and memory
  - recent raw state
  - masked old observations
  - retrieved repo evidence
        |
        v
Repo intelligence
  - symbol index
  - file summaries
  - dependency and test links
  - changed-file context
        |
        v
Memory runtime
  - core memory blocks
  - archival recall
  - run memory frames
        |
        v
Procedure memory bank
  - ReasonBlocks
  - failure clusters
  - promoted lessons
        |
        v
Quality-aware router
        |
        v
Model execution
        |
        v
Verifier and escalation gate
        |
        v
Trace, budget, and eval store
```

## Request Model

The gateway normalizes host input into a small routing object:

```ts
type AgentRequest = &#123;
  id: string;
  session_id?: string;
  user_goal: string;
  repo_root: string;
  task_type:
    | "debug"
    | "feature"
    | "refactor"
    | "test"
    | "explain"
    | "review"
    | "docs"
    | "ops";
  risk_level: "low" | "medium" | "high";
  changed_files: string[];
  context_budget: &#123;
    max_input_tokens: number;
    premium_call_budget: number;
    cache_policy: "prefer_cache" | "neutral" | "disable";
  &#125;;
&#125;;
```

The router may add provider-specific fields later, but this object is deliberately provider-neutral.
Provider prices, model names, rate limits, and cache multipliers must be loaded from configuration
or provider metadata. They must not be hard-coded in routing policy.

The canonical Pydantic fields live in
[IMPLEMENTATION_PLAN_V2_DATA_MODEL.md](IMPLEMENTATION_PLAN_V2_DATA_MODEL.md#5-new-models--routing-and-verification).
Implementation packets must use that schema rather than extending this TypeScript sketch.

## Context Compiler

The context compiler is the highest-leverage component. It classifies every candidate context block:

| Class       | Include policy                            | Examples                                                       |
| ----------- | ----------------------------------------- | -------------------------------------------------------------- |
| Pinned      | Always include and make cache-friendly    | system rules, repo conventions, active task objective          |
| Recent      | Include raw while still decision-relevant | latest user request, current diff summary, latest failing test |
| Noisy       | Mask or compress                          | repeated logs, old tool output, large stack traces             |
| Retrievable | Do not include until requested            | full files, old traces, archival memory, dependency callers    |

This follows the core result from JetBrains Research: observation masking and summarization can cut
long-horizon coding-agent costs substantially, and masking can be more reliable than summarizing
everything. Atelier therefore masks first, summarizes selectively, and retrieves facts on demand.

## Repo Intelligence

Repo retrieval must stop agents from performing open-ended grep/read loops. The default policy is:

1. Use changed files, failing tests, and task keywords to identify candidate scopes.
2. Return symbol and file summaries first.
3. Return full files only after a narrow request or high relevance score.
4. Include related tests for touched implementation files.
5. Include callers/callees only when they affect verification or blast radius.

This layer builds on Atelier's `semantic_file_memory`, `smart_read`, `smart_search`, and the V2
`search_read` work packet.

## Memory And Procedures

Atelier keeps fact memory and procedure memory separate:

| Layer                 | Stores                                                      | Runtime use         |
| --------------------- | ----------------------------------------------------------- | ------------------- |
| Memory runtime        | project facts, preferences, active state, archival passages | recall what is true |
| Procedure memory bank | ReasonBlocks, failure patterns, prevention rules            | decide what to do   |

ReasoningBank-style lessons belong in the procedure bank, not in raw hidden reasoning. A useful
lesson is structured and observable:

```ts
type ProcedureMemory = &#123;
  title: string;
  task_type: string;
  repo_scope: string;
  situation: string;
  successful_strategy?: string;
  failure_pattern?: string;
  prevention_rule?: string;
  evidence: &#123;
    commit?: string;
    test?: string;
    trace_id?: string;
  &#125;;
  confidence: number;
  last_used_at: string;
&#125;;
```

## Quality-Aware Routing

The router classifies the step, not just the user request.

| Step                                  | Cheap model allowed?           | Premium model default? |
| ------------------------------------- | ------------------------------ | ---------------------- |
| Task classification                   | Yes                            | Rarely                 |
| Context compression                   | Yes                            | Rarely                 |
| Repo search planning                  | Usually deterministic or cheap | Rarely                 |
| Mechanical edit                       | Sometimes                      | When risk is high      |
| Ambiguous debugging                   | Sometimes, with tight budget   | Yes                    |
| Security/auth/payments/data migration | No by default                  | Yes                    |
| Final architecture decision           | No by default                  | Yes                    |
| Commit message or summary             | Yes                            | Rarely                 |
| Lesson extraction                     | Cheap or mid                   | Rarely                 |

Start cheap when all of these hold:

- risk is low or medium;
- the expected diff is small;
- relevant ReasonBlocks or procedure memories exist;
- verification is deterministic and cheap;
- the task is mechanical or well-scoped.

Start premium when any of these hold:

- the domain is security, auth, payments, migrations, publishing, or compliance;
- the bug is ambiguous or cross-cutting;
- no relevant procedure memory exists;
- the expected blast radius spans many files;
- the user explicitly asks for best effort.

Escalate when any of these occur:

- tests fail twice with the same signature;
- the patch touches high-risk files unexpectedly;
- the model explanation contradicts retrieved repo evidence;
- the diff is much larger than the plan;
- the verifier cannot establish success;
- the router confidence drops below the configured threshold.

Escalation must send compressed failure evidence, not the full failed conversation.

## Host Enforcement Contract

Routing produces an auditable decision. It does not imply every host can force that decision in the
same way. Atelier therefore records a route execution contract per host:

| Mode                | Control level                                                                | Example                                     |
| ------------------- | ---------------------------------------------------------------------------- | ------------------------------------------- |
| `advisory`          | Atelier returns the decision; the host/user chooses whether to follow it.    | Copilot chat with MCP tools                 |
| `hook_enforced`     | Host hooks can warn, block, or inject required context around native events. | Claude Code with Atelier hooks enabled      |
| `wrapper_enforced`  | An Atelier wrapper gates task start, model flags, or completion checks.      | `atelier-codex` preflight / proof wrapper   |
| `provider_enforced` | Atelier owns the model call path.                                            | Future provider adapter only — **disabled** |

> **RouteDecision vs RouteExecutionContract** — A `RouteDecision` is a _decision artifact_:
> it records the chosen tier, confidence, reason, and required verifiers for a single routing step.
> A `RouteExecutionContract` is a _host descriptor_: it states whether the host can _enforce_ that
> decision (block start, require verification, force model flags) or can only _advise_ on it.
> Call `atelier route contract(host)` or `atelier route contract --host <host>` to retrieve the
> contract for a specific host. The `provider_enforced` mode is always disabled in the contract
> until a future provider execution packet explicitly enables it.

The same runtime should run across Claude Code, Codex, Copilot, opencode, and Gemini, but the host
matrix must state the actual enforcement and trace confidence for each host. A route decision counts
as enforced only when the matching host contract says it can be enforced. Otherwise it is advisory
evidence.

## Verification Gate

Cost savings only count when quality is preserved. The verifier judges outcomes:

- targeted tests pass;
- lint and typecheck pass when relevant;
- diff scope matches the plan;
- forbidden files are untouched;
- high-risk domain rubrics pass;
- no repeated-failure rule is violated.

The router cannot mark a task successful without verifier evidence. A cheap model that produces a
plausible answer but fails verification is a failed route, not a saved call.

## Final Proof Gate

The release claim requires one combined proof report. Token reduction alone is insufficient. The
proof gate consumes context-savings benchmarks, routing evals, host enforcement contracts, trace
confidence reports, route decisions, verification envelopes, and traces.

Minimum pass criteria:

- `context_reduction_pct >= 50.0`;
- `cost_per_accepted_patch < premium_only_baseline_cost_per_accepted_patch`;
- `accepted_patch_rate >= premium_only_baseline_accepted_patch_rate - 0.03`;
- `routing_regression_rate <= 0.02`;
- failed cheap attempts count against total cost and regression rate;
- high-risk rubric gates pass;
- every benchmark case links to run, trace, route decision, verification envelope, and context
  budget evidence.

The proof report must include per-host enforcement level and trace confidence. If a host is only
`advisory`, the report may show savings for that host, but must not claim enforced routing.

## Persistence

Routing is auditable. Atelier persists:

- `route_decision`: one row per routed step, including tier, confidence, reason, protected-file
  match, verifier requirements, escalation trigger, and evidence pointers;
- `route_execution_contract`: one row or generated snapshot per host, including enforcement mode,
  supported tiers, unsupported controls, and fallback behavior;
- `verification_envelope`: one row per verifier result, including observed validations, rubric
  status, outcome, and compressed evidence for escalation;
- `context_budget`: per-turn token and savings counters from the existing context-budget recorder.

These tables are defined in
[IMPLEMENTATION_PLAN_V2_DATA_MODEL.md](IMPLEMENTATION_PLAN_V2_DATA_MODEL.md#6-sqlite--postgres-schema).
They store decisions and evidence references only. They do not store hidden reasoning.

## Metrics

Atelier records these per run:

- total cost;
- input, output, cache-read, and cache-write tokens;
- model used per step;
- premium call rate;
- cheap success rate;
- escalation success rate;
- verifier pass/fail reason;
- tokens saved by masking, compression, caching, retrieval, and routing;
- memory hit rate;
- procedure memory hit rate;
- human accepted/rejected outcome when available.

The core derived metric is `cost_per_accepted_patch`, not raw token reduction.

## Implementation Map

The routing extension is implemented by Phase F in the V2 work-packet index:

- WP-25: routing policy configuration;
- WP-26: router runtime integration and provider abstraction hooks;
- WP-27: verification and escalation gates;
- WP-28: routing evals and cost-quality benchmark reporting.

The final contract/proof layer is implemented by Phase G:

- WP-29: host capability and enforcement contract;
- WP-30: host trace parity and confidence levels;
- WP-31: routing execution adapters and enforcement modes;
- WP-32: final cost-quality proof gate.

## References

- [Anthropic pricing](https://docs.anthropic.com/en/docs/about-claude/pricing)
- [Anthropic prompt caching](https://platform.claude.com/docs/en/docs/build-with-claude/prompt-caching)
- [JetBrains Research: smarter context management for agents](https://blog.jetbrains.com/research/2025/12/efficient-context-management/)
- [Letta GitHub repository](https://github.com/letta-ai/letta)
- [Google Research: ReasoningBank](https://research.google/blog/reasoningbank-enabling-agents-to-learn-from-experience/)
