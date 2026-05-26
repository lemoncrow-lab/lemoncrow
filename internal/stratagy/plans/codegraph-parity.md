# CodeGraph Parity Plan

## Goal

Close the highest-value code-intelligence gaps between Atelier and CodeGraph
without replacing Atelier as the primary runtime.

## Non-goal

- Do not replace Atelier orchestration, memory, tracing, routing, or host integration with CodeGraph.
- Do not chase feature parity where the ROI is low or the feature conflicts with Atelier's existing tool model.
- Do not add a second public MCP surface when an existing Atelier tool/op can carry the feature.

## Current position

Atelier already covers the core code-intel primitives:

- symbol search
- node/definition lookup
- callers
- callees
- impact analysis
- grouped explore/context flows
- indexed and planned multi-backend code-intel architecture

CodeGraph is still ahead in three areas that materially affect model uptake:

1. Tool ergonomics and directness
2. Always-fresh indexing and stale-data signaling
3. Specialized graph coverage for routes and cross-language mobile flows

## Gap matrix

| Area | CodeGraph | Atelier today | Status | Priority |
|---|---|---|---|---|
| Symbol lookup / callers / callees / impact | Strong | Strong | Near-parity | Low |
| One-call context for exploration | `codegraph_context` / `codegraph_explore` | `context`, `explore`, `node`, `symbols`, `callers`, `callees` | Partial parity, worse ergonomics | High |
| Model chooses code-intel tools directly | Strong host steering | Improving, but still inconsistent outside Claude/Codex | Partial | High |
| Fresh index / watcher / catch-up | Strong | Partial SCIP artifact refresh, weaker end-user signaling | Partial | High |
| Staleness / pending-sync banner | Explicit | Not surfaced as a strong agent-facing contract | Missing/weak | High |
| Framework-aware routes | Broad multi-framework | Limited / not a first-class public capability | Missing | High |
| Cross-language mobile bridging | Swift/ObjC/RN/Expo focused | Partial cross-language plan and implementation, narrower scope | Partial | Medium-high |
| Large-repo search backend story | Strong | Planned/partial via Zoekt architecture | Partial | Medium |
| Installer + host uptake | Strong simple story | Broad host coverage, but weaker enforcement and tool choice | Partial | High |

## Product decision

Keep Atelier as the top-level system.

If an external backend is used, treat it as a provider behind Atelier's code
tools, not as a separate user-facing system the model must choose manually.

That means:

- Atelier remains the only workflow/runtime layer.
- Code-intel should converge behind Atelier `code` / `explore` / `context`.
- Optional third-party backends are implementation details.

## Workstreams

## W1. Tool-surface simplification

Problem:
Atelier exposes many capable tools, but the model still hesitates or falls back
to native tools because the direct path is not obvious enough.

Deliverable:

- Define a smaller "hero path" for code exploration:
  - `context`
  - `explore`
  - `node`
  - `callers`
  - `callees`
  - `impact`
- Reduce prompt/docs emphasis on lower-level branching when a higher-level tool works.
- Ensure host-specific instructions always map native search/read/edit/shell back to Atelier first.

Acceptance criteria:

- In host prompts, coding tasks start with `context`.
- For architecture/exploration questions, `explore` is the default recommendation.
- Native-tool substitution guidance is explicit for every supported host.

## W2. Host enforcement and uptake

Problem:
Even when Atelier is installed, many hosts still leave tool choice mostly up to model behavior.

Deliverable:

- Eager-load or equivalent always-on registration wherever the host supports it.
- Install stronger host-native prompt/rule surfaces by default.
- Add startup/session checks that warn when Atelier tools are visible but not hydrated or not selected.

Acceptance criteria:

- Every installer documents actual enforcement level honestly.
- Every host with a rule/prompt/plugin surface installs Atelier-first guidance by default.
- Sessions can detect and report "Atelier installed but not active" states.

## W3. Freshness and staleness contract

Problem:
CodeGraph's always-fresh story is simple and visible. Atelier has pieces of this, but the user-visible contract is weaker.

Deliverable:

- Normalize file/index freshness behavior across code-intel backends.
- Surface a first-class pending-sync or stale-index signal in agent-facing responses.
- Add reconnect catch-up semantics when index state is older than workspace state.

Acceptance criteria:

- Agent sees a compact freshness banner when relevant results may be stale.
- There is one documented "trust contract" for index freshness.
- `context` / `explore` / symbol-level responses can expose freshness metadata when needed.

## W4. Framework-aware routes

Problem:
CodeGraph has a clear advantage for request-path and route-to-handler exploration.

Deliverable:

- Add route extraction as a first-class indexed capability behind Atelier code tools.
- Start with the highest-value frameworks:
  - FastAPI
  - Django
  - Flask
  - Express
  - Rails
  - Spring
- Expose route lookups and route-linked callers in the existing code-intel flow.

Acceptance criteria:

- Asking "what handles `/foo`?" works through Atelier.
- Handler exploration can show inbound route bindings.
- Route edges participate in `explore` and `impact`.

## W5. Cross-language edge expansion

Problem:
Atelier's cross-language story is real but narrower than CodeGraph's advertised mobile/mixed-language coverage.

Deliverable:

- Extend current cross-language support beyond the existing partial set.
- Prioritize the highest-value mobile boundaries:
  - Swift <-> Objective-C
  - React Native bridge
  - Expo module bindings
  - native event emitters to JS listeners

Acceptance criteria:

- Cross-language edges are queryable through the existing symbol/explore path.
- Confidence levels are explicit.
- Unsupported boundaries fail honestly rather than implying full coverage.

## W6. Benchmark and uptake measurement

Problem:
Without measuring actual model behavior, parity work turns into feature accumulation.

Deliverable:

- Reproduce a benchmark set similar to CodeGraph's architecture-question evaluation.
- Track:
  - tool calls
  - token usage
  - time
  - number of native fallbacks
  - whether Atelier-first tools were selected

Acceptance criteria:

- There is a stable benchmark harness for exploration tasks.
- We can compare Atelier-only, Atelier+improved prompts, and optional backend variants.
- Product decisions are based on measured model uptake, not only capability lists.

## Proposed sequencing

1. W1 Tool-surface simplification
2. W2 Host enforcement and uptake
3. W3 Freshness and staleness contract
4. W4 Framework-aware routes
5. W5 Cross-language edge expansion
6. W6 Benchmark and uptake measurement throughout

Reason:
The biggest current loss is not only missing graph features. It is that the
model often does not choose the best Atelier path even when a good path exists.

## Build vs integrate

Default position:
build inside Atelier first.

Exception:
If CodeGraph already solves one of W3/W4/W5 materially better and can be used
as an internal provider with low operational complexity, evaluate integration as
an implementation shortcut.

Rules:

- No direct user dependency on a second top-level tool brand.
- No duplicate MCP tool families exposed to the model.
- Any external provider must map into Atelier semantics and traces.

## Open questions

1. Should route intelligence be a new `code` op or folded into `explore` results first?
2. Should freshness be surfaced only when stale, or always with a confidence/status field?
3. Is React Native / Expo coverage important enough to prioritize ahead of route extraction?
4. Should optional CodeGraph integration be evaluated before building full route extraction internally?

## Exit condition

This plan is complete when:

- Atelier-first tool selection is reliable across hosts
- route-aware exploration exists for the top frameworks
- freshness/staleness signaling is explicit
- cross-language coverage closes the highest-value gaps
- benchmarked exploration behavior is competitive with CodeGraph on real tasks
