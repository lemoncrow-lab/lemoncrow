# LemonCrow Intelligent Runtime — integration and proof plan

> **Status:** deterministic implementation complete on feat/intelligent-runtime-proof-loop; external comparative execution and publication remain
>
> **Date:** 2026-09-23
>
> **Grounded against:** local `main`, originally audited at `45de956a9940fd6564e388feb98a30a272d645aa`, reconciled through `fee7bd83e0e965d9dff63a17eba07fd59669e603`, implementation worktree created from `9a30ae44e42ab76b37be53786a8a8347a043b8cb`, and the completed feature commit rebased onto current `main` at `0a8a7bab6438288e5ce71522c4e6d5c85d69fe7d`.
>
> **Implementation isolation:** all source changes live in `.lc-worktrees/intelligent-runtime-proof-loop` on `feat/intelligent-runtime-proof-loop`; the main checkout is not used for implementation.
>
> **Primary decision:** do not create another runtime, search stack, agent framework, or orchestration product. LemonCrow already contains the necessary runtime, retrieval, routing, recovery, tracing, replay, and benchmark primitives. The remaining work is to connect those primitives through one evidence/decision contract, move transport-specific intelligence back behind the runtime boundary, graduate existing shadow/V2 policies through controlled acceptance gates, and publish the evidence required to support an "intelligent runtime for coding agents" claim.

> **Implementation checkpoint (worktree `feat/intelligent-runtime-proof-loop`, original base `9a30ae44e`, rebased onto `0a8a7bab6`):**
>
> | Phase | State | Implemented evidence |
> | --- | --- | --- |
> | IR-0 | done | grounded plan carried into isolated worktree |
> | IR-1 | done | shared bounded/redacted `RuntimeDecisionEvent` + structural sinks for RunLedger and optimization traces |
> | IR-2 | done | transport-neutral `SearchFeedbackPolicy`; MCP supplies session identity only |
> | IR-3 | implemented / shadow | observation-only `EvidenceState` over existing `CodeContextEngine` output; public `code_search` response unchanged |
> | IR-4 | implemented / unqualified | bounded deterministic `HYDRATE_SOURCE` / `EXPAND_RELATIONS` proposals; production `enforce` requests remain shadow. One explicit `experiment` mode can execute one bounded round **only when separately armed for benchmark qualification** |
> | IR-5 | implemented | `EvidenceState` feeds the existing quality router; dark/absent retrieval cannot by itself buy a premium model; normal risk/verifier escalation remains intact |
> | IR-6 | implemented / evidence pending | CodeBench A0/A1/A2/A3 attribution, stable policy fingerprints, host-control tiers, comparison gates, competitor metadata, and per-run observed-policy artifacts. A3 is explicitly marked an **unqualified benchmark-only experiment**; no qualifying paid run has been executed yet |
> | IR-7 | implemented / qualified | bounded content-free recovery facts now cover session reopen, revision retry, write replay, blob fill and reuse rejection; pre-commit disconnect and post-commit lost-ACK writes both replay the exact same `client_seq` and assert one committed mutation. The full public client reliability group is 96/96 green and the real enterprise-server thin-client E2E suite is 22/22 green in both `local_fs` modes |
> | IR-8 | harness + frozen protocol complete / comparative evidence pending | A1/A3 qualification requires manifest identity **and observed runtime execution**. The frozen `benchmarks/codebench/protocols/intelligent-runtime-v1.json` matrix is fingerprinted as `10612030a7e2f2d7bc5f7f25ba5687b93bc1739be8b89cc03ec9dac02fe1974c`: 7 pinned repos/prompts, Claude qualification, Codex generalization, and an isolated CodeGraph external arm = 308 primary agent rows + 273 pinned pairwise judge comparisons. No paid/public comparative proof run has been launched from this worktree |
>
> **Current focused validation:** latest targeted runs are 117/117 core/runtime/routing/benchmark/protocol-contract, 174/174 MCP/search integration, 93/93 CodeBench + competitor + evidence + protocol + gates, 46/46 benchmark-CLI/native-server/runtime-attribution, 96/96 thin-client reliability, 22/22 real enterprise-server E2E, and 4/4 owned-output-governor. These groups have intentional overlap, so they are not summed into a misleading grand total. The real zero-spend protocol preflight resolves as 308 primary agent rows + 273 pairwise judge comparisons and passes the frozen host-version checks; synthetic complete runs pass `--verify-run`, while stale gates, dirty checkouts, missing artifacts, incomplete rows, or manifest drift fail. `git diff --check`, Ruff across all touched Python files, and targeted mypy across 17 new/changed decision/evidence/routing/benchmark/client/server modules are clean. The hosted E2E suite is executed from the locked `enterprise/server` project environment; its two stale `r-` assertions were corrected to the current bare 32-hex Review-ID storage contract before the 22/22 pass.
>
## 1. Executive decision

The earlier assumption that LemonCrow still needed a transport-neutral tool runtime, learned ranking, restart recovery, or a basic adaptive retrieval system is obsolete on current `main`.

Those capabilities already exist in meaningful form:

- `gateway/tools/call_runtime.py` is the transport-neutral tool-call execution path used by stdio MCP and HTTP MCP.
- `LemonCrowRuntimeCore` remains the capability/runtime orchestrator used by product/runtime surfaces.
- `CodeContextEngine` already combines exact lookup, FTS, Zoekt, semantic retrieval, call/reference relationships, hybrid fusion, token budgeting, source hydration, cache invalidation, and a learned LambdaMART reranker.
- search results already carry session-aware `found | missed | absent | dark` semantics and a no-progress breaker.
- the owned runtime already has closed-loop model/phase routing, output governance, provider-cache economics, bounded local retrieval, hybrid MCP exposure, and verified cross-session reuse.
- `OptimizationTraceRecorder`, `RunLedger`, session replay, outcome capture, context-budget telemetry, benchmark manifests, evidence artifacts, and benchmark gates already exist.
- the thin client already survives stale revisions and server-side session loss, and its monotonic client sequence makes retried writes after a lost acknowledgement safe.
- the public benchmark suite already has strong end-to-end evidence on SWE-bench Verified/Pro/Lite, multi-language exploration, Telegraphic Q&A, and Terminal-Bench.

Therefore the next architecture should be an **integration and proof program**, not a capability land-grab.

The product direction also remains unchanged:

> Review, usage visibility, and model freedom remain the simple user-facing products. The intelligent runtime is the substrate that makes those products and third-party coding agents better.

This plan does **not** reopen the September decision to build a broad agent platform.

## 2. Codebase ground truth

This section is deliberately explicit so implementation work starts from the current tree rather than from an abstract architecture sketch.

### 2.1 Runtime layers that already exist

| Concern | Current implementation | State | Design implication |
| --- | --- | --- | --- |
| capability orchestration | `src/lemoncrow/core/runtime/engine.py::LemonCrowRuntimeCore` | active | keep as the core capability/runtime orchestrator |
| tool-call orchestration | `src/lemoncrow/gateway/tools/call_runtime.py` | active, transport-neutral | do not create a second `ToolCallRuntime` |
| tool lifecycle/accounting | `gateway/tools/lifecycle.py`, `execution.py`, `routing.py`, `model_routing.py` | extracted from MCP | continue moving policy out of adapters rather than adding another execution path |
| product runtime facade | `gateway/adapters/runtime.py::ContextRuntime/RuntimeSession` | active | preserve compatibility; clarify ownership rather than rewrite it |
| owned agent loop | `pro/capabilities/owned_agent_session/*`, `gateway/cli/runtime.py` | active | this is the only lane where LemonCrow can fully enforce provider/model/cache/output policy |

There are two legitimate meanings of "runtime" in the code today:

1. **Capability runtime:** `LemonCrowRuntimeCore`, which coordinates capabilities, traces, evals, storage, context, routing, verification, etc.
2. **Invocation runtime:** `gateway/tools/call_runtime.py`, which executes one prepared tool call independently of the transport.

They should be documented as layers of one runtime rather than replaced by a third central object.

### 2.2 Retrieval intelligence that already exists

`src/lemoncrow/pro/capabilities/code_context/engine.py` is already a mature retrieval engine, not a simple top-k search wrapper.

Current behavior includes:

- exact-name and exact-path fast paths;
- lexical/FTS retrieval;
- Zoekt retrieval for large repositories;
- optional semantic retrieval;
- exact/anchor-Zoekt/line-FTS HEF channels launched in parallel with the baseline path;
- hybrid fusion of independent recall channels;
- soft `paths=` scope that reserves only the first two positions instead of destroying whole-repository recall;
- symbol/reference/caller/callee/call-graph expansion;
- centrality, churn and context signals;
- skeletonized and exact source projections;
- token-budget packing and recoverable overflow;
- content/source fingerprints and retrieval caching;
- autosync/index freshness;
- deleted/history search;
- post-ranking source hydration;
- a packaged/per-workspace LambdaMART reranker;
- conservative rank-1 preservation and rerank margin policy;
- self-supervised training collection mode;
- A/B model override hooks.

The public `code_search` contract already projects this rich internal candidate set into a small agent-facing answer:

- useful source is treated as already read;
- exact hits can ship bounded top source inline;
- uncertain ranked hits stay as exact pointers rather than dumping speculative source;
- related symbols and candidate files survive without forcing grep/read loops;
- whole-file fallback covers files that have no symbol index.

Any "intelligent retrieval" work must reuse this engine.

### 2.3 Search stopping intelligence already exists

`pro/capabilities/code_context/search_verdict.py` already distinguishes:

- `found`: evidence exists;
- `dark`: a required retrieval channel was unavailable, so an empty is untrustworthy;
- `missed`: the first useful phrasing missed;
- `absent`: distinct reformulations of the same concept were empty and live channels ran.

It also has bounded per-session empty-history and a no-progress breaker.

The architectural problem is not the verdict algorithm. The problem is that its **per-session wiring currently lives in the legacy MCP composition root** (`gateway/adapters/mcp_server.py::_apply_search_verdict`). This means a generally useful runtime decision is still attached to one adapter.

That is a concrete extraction target.

### 2.4 Routing intelligence already exists

Several complementary routing layers are implemented:

- `pro/capabilities/quality_router/*`
  - risk-aware tier selection;
  - evidence confidence;
  - context pressure;
  - protected-file and high-risk-domain escalation;
  - verifier requirements;
  - deterministic tier for safe low-risk phases;
  - forced escalation on repeated failure or verifier gaps.
- `pro/capabilities/model_routing/router.py`
  - task/tool/output/session/error scoring;
  - model recommendation and route tiers.
- `pro/capabilities/cross_vendor_routing/router.py`
  - cross-vendor candidate selection.
- `pro/capabilities/owned_agent_session/runtime_policy.py`
  - reasoning effort;
  - output limits;
  - cache policy;
  - tool exposure;
  - route switching;
  - deterministic history compaction.
- routing calibration/outcome storage in the optimization layer.

The missing piece is **not another router**. It is a shared evidence contract and controlled proof that these decisions beat the current fixed/runtime policy.

### 2.5 Bounded local multi-turn retrieval already exists

`pro/capabilities/optimization/local_retrieval.py` implements the "local frontier-token firewall":

- eligibility gating;
- bounded corpus;
- 1–5 local retrieval turns;
- deterministic refinement by default;
- optional local-only planner;
- planner can emit only next-query/finish JSON;
- exact source spans with file SHA-256;
- confidence threshold and deterministic fallback;
- local cache keyed by workspace fingerprint;
- `off | shadow | enforce` modes.

The current savings roadmap correctly marks this **V2 implemented / retrieval A/B pending**.

This experiment must not simply be generalized to every host. Its current corpus/search implementation is separate from the much stronger `CodeContextEngine`. The correct next step is to measure it, then decide whether the planner concept should operate over `CodeContextEngine` rather than proliferating another retrieval stack.

### 2.6 Runtime decision and outcome evidence already exists

The project already has multiple complementary evidence systems:

- `pro/capabilities/optimization/runtime_decisions.py`
  - redacted `off | shadow | enforce` decision traces;
  - proposed vs actual policy;
  - provider calls;
  - route switches;
  - tool outcomes;
  - verification;
  - token/cost totals;
  - accepted/error outcome.
- `infra/runtime/run_ledger.py`
  - tool/command/file/test/workflow events;
  - checkpoints;
  - git anchors;
  - durable merge-safe persistence.
- `core/capabilities/session_replay.py`
  - inefficient search/read episodes;
  - batch opportunities;
  - replay/savings analysis across hosts.
- `core/capabilities/session_replay_live.py`
  - live enrichment against real code search/read/tool behavior.
- `pro/runtime/outcome_capture.py`
  - route/compact outcomes and lesson candidates.
- context-budget telemetry and usage attribution.
- Review provenance and host trace-confidence surfaces.

The gap is that the runtime does not yet expose **one stable decision/outcome vocabulary** shared by retrieval, routing, recovery and verification. Evidence is rich but fragmented.

### 2.7 Thin-client durability already exists

The hosted/local thin client already implements important runtime-grade semantics:

- typed `server_unreachable` with `retryable=True`;
- handshake/protocol negotiation;
- authenticated session/view state;
- view revisions and read-after-edit consistency;
- workspace fingerprint refresh;
- server-validated result reuse;
- blob repair;
- stale-revision repair;
- transparent one-shot bootstrap/retry on `SESSION_UNKNOWN` after a server restart;
- monotonic client sequence numbers;
- exact replay handling for retried writes after lost acknowledgements.

Do not create a second session token/recovery protocol.

Future reliability work should prove these semantics across failure injection and close any uncovered lane-specific gap, not redesign the protocol.

### 2.8 Benchmark and acceptance infrastructure already exists

LemonCrow already has:

- Codebench;
- Harbor/Terminal-Bench integration;
- SWE-bench Verified/Lite/Pro;
- multi-language exploration;
- MCP tool benchmarks;
- retrieval MRR/evaluation utilities;
- self-optimization benchmark scaffolding;
- benchmark manifests;
- benchmark evidence artifacts with commit/dirty state;
- paired non-inferiority gates with Wilson interval diagnostics;
- mandatory pairwise quality gates;
- quality-adjusted savings;
- reproducible raw-result links.

Current public results already demonstrate substantial runtime value. For example, the current benchmark ledger records:

- SWE-bench Verified: 92.8% vs 80.8% resolved, 29.5% lower cost, 44.9% fewer total tokens, 37.7% fewer turns, 23.7% lower wall-clock time;
- SWE-bench Pro: 90% vs 88% resolved, 21.5% lower cost;
- Terminal-Bench 2.1: equal resolved rate at 78.9%, 98.6% fewer fresh input tokens, 30.2% fewer total tokens, 16.0% lower normalized cost;
- seven-repository exploration: 67% lower aggregate cost, with the documented Excalidraw outlier.

The missing benchmark is **runtime attribution and policy ablation**: which runtime intelligence decisions caused incremental improvement over current LemonCrow, on which hosts/tasks, and at what cost/latency.

### 2.9 Host capability is intentionally asymmetric

The host capability matrix matters to this design.

LemonCrow cannot honestly claim equal control everywhere:

- MCP-only hosts get deterministic tool/runtime intelligence but do not give LemonCrow ownership of provider/model calls.
- hosts with hooks/events provide better session attribution, lifecycle and verification signals.
- managed LemonCode/Pi lanes give LemonCrow full control of route, provider/model, toolset, cache, compaction, verification and stopping.

The design must encode this asymmetry instead of pretending model routing is enforced inside Claude Code/Cursor/Copilot when the host does not expose that control.

### 2.10 Public/private boundary

The advanced single-developer runtime is deliberately public:

- code intelligence;
- retrieval;
- MCP;
- memory;
- optimization;
- model routing;
- verification;
- swarm/worktree primitives;
- local Review;
- local server;
- thin client.

Enterprise owns organization/service concerns:

- identity/RBAC/SSO/SCIM;
- collaboration;
- quota/entitlement;
- organization policy;
- compliance/retention;
- fleet deployment and operation.

Therefore the intelligence work in this plan belongs in the public runtime unless it is specifically fleet/organization policy.

## 3. Architecture diagnosis

LemonCrow is not missing intelligence so much as it is missing a **single closed proof loop around the intelligence it already has**.

Today the picture is approximately:

```text
                         HOST
                          |
              +-----------+-----------+
              |                       |
          MCP / HTTP              managed lc code
              |                       |
              v                       v
      ToolCallRuntime            owned agent loop
              |                       |
      +-------+--------+        route/cache/output/
      |       |        |        verify/stop policy
      v       v        v
 search    read/edit   bash
   |
   v
CodeContextEngine
   |
   +-- hybrid recall / HEF / Zoekt / FTS / semantic
   +-- graph/reference expansion
   +-- ranking + LambdaMART
   +-- budget/source shaping

Separate evidence systems:
  search history/verdict      -> currently MCP composition root
  runtime decision JSONL      -> owned-runtime optimization lane
  RunLedger                   -> general run history
  outcome capture             -> route/compact outcomes
  context-budget telemetry    -> usage/cost
  benchmark evidence/gates    -> offline acceptance
```

Each subsystem is reasonable in isolation. The problem is that no stable contract says:

> "This was the evidence state; this was the bounded runtime decision; this action was taken (or only shadowed); this was the observed outcome; this is the benchmark gate that allows the policy to graduate."

That is the gap.

## 4. Operational definition of "intelligent runtime"

Do not define intelligence as "uses an LLM internally" or "has many routing heuristics."

For LemonCrow, runtime intelligence should mean four observable capabilities.

### 4.1 Evidence selection

The runtime finds the minimum sufficient repository evidence for the task while preserving exact provenance.

Observable outcomes:

- correct target appears early;
- fewer blind reads/greps;
- fewer model-facing search turns;
- fewer irrelevant context tokens;
- exact source/range recovery remains available.

### 4.2 Budget allocation

The runtime spends compute/context only where it changes expected task outcome.

Observable decisions include:

- inline source vs pointer;
- skeleton vs full source;
- one-shot retrieval vs bounded expansion;
- deterministic vs model-backed phase;
- cheap/mid/premium model tier where LemonCrow owns the loop;
- output/reasoning budget;
- cache policy;
- verifier requirements.

### 4.3 Recovery and self-correction

The runtime notices when its current strategy is unreliable or unproductive and changes strategy without duplicating side effects.

Observable cases include:

- dark retrieval channel;
- repeated empty reformulations;
- stale workspace revision;
- lost acknowledgement;
- server session loss;
- repeated command/tool failure;
- verifier gap;
- truncated provider output;
- stale cached evidence.

### 4.4 Measured learning

A policy is not "smart" because it sounds plausible. It becomes part of the intelligent runtime only after:

1. it runs in shadow mode where possible;
2. proposed/actual decisions are recorded;
3. outcomes are linked to those decisions;
4. a frozen replay or controlled A/B shows benefit;
5. a non-inferiority/quality gate passes;
6. a rollback remains available.

This is already the philosophy of the savings optimization roadmap. This plan makes it the runtime-wide rule.

## 5. Target architecture: an intelligence contract, not a third runtime

Add a small shared contract around existing owners.

```text
                      Coding host
                          |
             +------------+------------+
             |                         |
        MCP / HTTP                 managed host
             |                         |
             +------------+------------+
                          |
                          v
                  ToolCallRuntime
                    (existing)
                          |
              +-----------+-----------+
              |           |           |
              v           v           v
          retrieval    execution    lifecycle
              |           |           |
              v           v           v
       CodeContextEngine  kit      recovery/accounting
          (existing)    (existing)     (existing)
              |                       |
              +-----------+-----------+
                          |
                          v
                RuntimeDecisionSink
                  small shared contract
                          |
          +---------------+----------------+
          |               |                |
          v               v                v
      RunLedger    optimization trace   benchmark evidence
      (existing)      (existing)           (existing)
          |
          v
       outcomes
          |
          +-------> offline replay / calibration / gates
```

No new god object owns retrieval, model routing, lifecycle, storage and transport.

Existing subsystems continue to make the decisions they understand. They emit a common **decision fact**.

## 6. Runtime decision contract

Introduce a minimal provider/transport-neutral decision model in the public core layer.

Suggested shape:

```python
class RuntimeDecisionEvent:
    id: str
    kind: str
    phase: str

    policy: str
    policy_version: str
    mode: Literal["observe", "shadow", "enforce"]

    session_id: str | None
    workspace_revision: str | int | None

    evidence_refs: tuple[str, ...]
    confidence: float | None
    reason_codes: tuple[str, ...]

    proposed: Mapping[str, JsonValue]
    actual: Mapping[str, JsonValue]

    budget: Mapping[str, JsonValue]
    metrics: Mapping[str, JsonValue]
```

The schema must contain **structured facts, not hidden model reasoning**.

Examples of `kind`:

```text
retrieval.channel_selection
retrieval.expand
retrieval.stop
retrieval.verdict
retrieval.rerank
context.projection
context.prune
route.model
route.phase
route.cache
route.tool_exposure
verification.require
verification.result
recovery.session_reopen
recovery.revision_retry
recovery.write_replay
output.extend
loop.break
```

Reason codes should be stable enums/strings such as:

```text
exact_match
low_rank_margin
unresolved_symbol
channel_dark
reformulation_exhausted
context_pressure
high_risk
protected_file
repeated_failure
verifier_gap
stale_revision
session_unknown
provider_truncated
```

### Privacy rule

Decision events must default to:

- IDs/fingerprints;
- ranges;
- counts;
- scores;
- policy names;
- model/provider IDs;
- cost/timing/token data;
- source references where the existing ledger is allowed to store them.

Do not create a second store of prompts, source blobs, commands, secrets or arbitrary tool payloads.

### Compatibility rule

Do not delete or rename `OptimizationTraceRecorder` or `RunLedger` in the first implementation.

Create a tiny sink/protocol and adapters:

```text
RuntimeDecisionSink
  ├── no-op
  ├── RunLedger adapter
  └── OptimizationTraceRecorder adapter
```

The first phase is additive. Consolidation can happen later only if the data proves the schemas truly overlap.

## 7. Retrieval evidence state

The next retrieval improvement should be a transport-neutral **evidence-state evaluator**, not another search engine.

Suggested model:

```python
class EvidenceState:
    status: Literal[
        "decisive",
        "useful",
        "ambiguous",
        "dark",
        "absent",
    ]

    confidence: float
    reason_codes: tuple[str, ...]

    top_paths: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    unresolved_refs: tuple[str, ...]

    channels_requested: tuple[str, ...]
    channels_live: tuple[str, ...]

    tokens_used: int
    elapsed_ms: int
```

Inputs must be derived from facts `CodeContextEngine` already produces or can cheaply expose:

- exact match;
- query intent/shape;
- channel health;
- rank scores/margins;
- independent-channel agreement;
- symbol/reference resolution;
- unresolved related symbols;
- source sections actually hydrated;
- scope/path match;
- search verdict history;
- token budget consumption;
- cache/index freshness.

Do **not** define confidence as one unexplained magic score. Keep reason codes so the policy can be audited and benchmarked.

## 8. Bounded evidence-resolution loop

Only after the evidence state is available should LemonCrow test a bounded internal retrieval loop.

The objective is not "search more."

The objective is:

> avoid making the frontier coding agent spend additional model turns on search -> read -> search when LemonCrow can deterministically resolve the ambiguity inside one tool call.

### 8.1 Current path remains round 0

Every call starts with the current `CodeContextEngine.tool_explore` / `code_search` path unchanged.

If the result is decisive/useful, return immediately.

This guarantees zero added planner work on the common case.

### 8.2 Eligible expansion

Expansion is considered only for explicit reason codes, for example:

- `low_rank_margin`;
- `unresolved_symbol`;
- `independent_channels_disagree`;
- `missed_first_phrasing`;
- a scoped seed resolves but neighboring implementation/test evidence is missing.

A `dark` channel should normally produce an honest degraded result rather than burning cycles trying to compensate invisibly.

### 8.3 Allowed actions

V1 actions should be deterministic and operate on the existing engine:

```text
STOP
EXPAND_RELATIONS
FOLLOW_UNRESOLVED_SYMBOL
REFORMULATE_LITERAL
EXPAND_NEIGHBOR_FILE
HYDRATE_SOURCE
```

Do not add general autonomous planning.

### 8.4 Hard bounds

The loop must have:

- a small round limit;
- a shared token budget;
- a shared latency deadline;
- no mutation;
- no provider/cloud call;
- no hidden fallback that returns generated prose as repository evidence.

Initial limits should be chosen from replay data, not marketing intuition. The current owned local-retrieval bounds provide a safe upper envelope, not a default for the general `code_search` path.

### 8.5 Optional local planner

The existing local-only planner remains an experiment.

If deterministic expansion leaves enough ambiguous tasks to justify it, add a planner adapter that chooses only from the allowed actions above and executes against `CodeContextEngine`.

Do not generalize the current bounded-corpus scanner as a second canonical retrieval engine.

The planner remains:

- local-only unless a separate product decision changes that;
- unable to answer the user task;
- unable to create patches;
- shadow-first;
- bounded;
- fail-open to deterministic retrieval.

## 9. Move search-session intelligence out of MCP

The pure verdict algorithm is already correctly separated.

The remaining stateful wiring in `mcp_server.py::_apply_search_verdict` should move behind a transport-neutral boundary.

Target responsibilities:

```text
SearchSessionState
  - bounded empty/reformulation history
  - unproductive streak
  - session-scoped reset

SearchFeedbackPolicy
  - compute found/missed/absent/dark
  - emit loop-break decision
  - attach model-facing verdict fields

adapter
  - supplies stable session identity only
```

The adapter should not own the policy.

Acceptance requirement:

- stdio MCP and HTTP MCP produce the same verdict for the same session/search sequence;
- direct runtime/unit tests do not need to instantiate `mcp_server.py`;
- no behavior change before any new evidence-resolution policy is enabled.

## 10. Join retrieval evidence to existing routing

Do not build a new routing algorithm.

Teach the existing quality/owned routing paths to consume the same `EvidenceState` where they already accept evidence summaries.

Examples:

```text
decisive + low risk
  -> deterministic/cheap phase remains eligible

ambiguous + repeated failure
  -> premium/repair lane may be justified

dark retrieval
  -> do not pretend low confidence means "buy a bigger model"
  -> surface degraded evidence / repair the unavailable channel

high risk + verifier gap
  -> require verification regardless of retrieval confidence
```

This is important: a more expensive model is **not** a universal remedy for poor repository evidence.

### Host control tiers

Encode what LemonCrow can actually control.

#### Tier S — substrate control

Available to every host using LemonCrow tools:

- retrieval;
- source shaping;
- output bounding;
- exact reads;
- safe edits;
- verification facts;
- search verdict/no-progress feedback;
- cache/index freshness.

#### Tier H — lifecycle/hook control

Available where the host exposes hooks/events:

- stronger session attribution;
- stop/finalize hooks;
- host usage capture where available;
- live provenance;
- richer no-progress and verification feedback.

#### Tier O — owned loop

Available to managed LemonCode/Pi/other LemonCrow-owned execution:

- provider/model selection;
- reasoning effort;
- output budget;
- cache lane;
- tool profile;
- phase switching;
- deterministic completion/verification;
- cost cap.

Benchmark reports and traces must name the control tier. Never report an owned-loop optimization as though it was enforced inside an MCP-only host.

## 11. Decision-to-outcome linkage

Every enforceable runtime decision needs a measurable outcome.

The minimum link is:

```text
decision_id
  -> run/session
  -> relevant tool/provider calls
  -> verification/result
  -> accepted/error/failure outcome
  -> cost/tokens/latency
```

For retrieval decisions, add retrieval-specific outcome signals where available:

- which candidate the agent actually read next;
- which file/symbol was edited;
- whether the top evidence intersected the eventual patch;
- additional searches before first edit;
- redundant reads;
- task correctness;
- verification result.

This creates self-supervised evidence without requiring a model judge for every search.

The learned reranker can then train/calibrate on actual downstream behavior while benchmark gates protect correctness.

## 12. Replay and learning policy

Runtime policy must improve **offline first**.

### 12.1 Replay inputs

Use existing:

- RunLedger;
- session replay;
- decision traces;
- benchmark trajectories;
- source/revision fingerprints.

### 12.2 Candidate policy replay

A candidate policy should be able to answer:

```text
For these historical evidence states:
- what would I have done?
- how often would I differ from the current policy?
- which differences are evaluable from existing evidence?
- which require a fresh controlled run?
```

Replay must not claim causal savings when the alternative action's downstream model trajectory is unknowable.

Use replay for:

- policy eligibility;
- false-positive discovery;
- rank/retrieval metrics;
- deterministic cost/token projections;
- selecting cases for a real A/B.

Use controlled runs for:

- correctness;
- accepted-change cost;
- provider/model routing;
- agent behavior changes.

### 12.3 No live self-modifying policy

Do not let production traces silently change enforcement weights.

Training/calibration produces a versioned candidate artifact.

Promotion is explicit and benchmark-gated.

Rollback is one config/model/policy version.

## 13. Benchmark design

Do not create a new benchmark framework.

Extend the current Codebench/Harbor/benchmark-manifest/gate system with runtime-policy attribution.

### 13.1 Required arms

For an eligible experiment:

```text
A0  host-native baseline
A1  current LemonCrow control
A2  LemonCrow + candidate policy in shadow
A3  LemonCrow + candidate policy enforced
A4  optional local-planner variant, only when evaluating that lever
```

A2 is important even though behavior should match A1: it proves instrumentation itself is not perturbing results and gives proposal frequency before enforcement.

### 13.2 Freeze the real independent variables

Manifest additions should record at least:

- host/driver;
- host control tier;
- provider/model;
- repository/task revision;
- LemonCrow commit;
- dirty state;
- runtime policy ID/version/hash;
- retrieval policy ID/version/hash;
- reranker model fingerprint;
- optimization mode;
- context-compressor arm;
- tool/persona profile;
- cache tier;
- repetition count;
- timeout;
- concurrency.

### 13.3 Primary metrics

Never collapse the claim into one arbitrary score.

Report a Pareto surface:

**Quality**
- resolved/correct;
- pairwise judged quality where required;
- verifier/validation pass;
- accepted-change rate.

**Evidence**
- MRR;
- recall@1;
- exact-target recall;
- candidate-to-eventual-edit overlap;
- searches before first useful read/edit;
- redundant reads.

**Efficiency**
- fresh input;
- cache write/read;
- output tokens;
- total tokens;
- provider calls;
- tool calls;
- turns;
- cost.

**Latency**
- retrieval p50/p95;
- time to first useful evidence;
- end-to-end wall time.

**Reliability**
- recovery success;
- duplicated side effects;
- stale-answer rate;
- retry count;
- unhandled runtime failures.

### 13.4 Existing suites to reuse

Use the suites that already establish different failure modes:

- retrieval MRR/gold corpus;
- seven-repository multi-language exploration;
- SWE-bench Verified;
- SWE-bench Pro;
- SWE-bench Lite as a regression sentinel because it has already shown a small quality loss in one cut;
- Terminal-Bench;
- MCP tool benchmarks;
- the self-optimization harness.

Do not run every expensive suite after every edit.

The implementation loop should use:
1. targeted unit/integration tests;
2. cheap retrieval/replay gates;
3. small frozen smoke corpus;
4. full paid end-to-end suites only at phase acceptance.

### 13.5 Reliability/fault suite

Add deterministic integration coverage for:

- server process/session loss during a long thin-client session;
- stale view revision;
- lost ACK followed by retried write;
- duplicate request ID/sequence;
- stale index/cache;
- Zoekt unavailable;
- semantic channel unavailable;
- blob missing and repaired;
- workspace changed out of band;
- result-cache validator stale;
- verifier/tool failure;
- provider truncation in owned mode.

The requirement for mutation recovery is **zero duplicated mutations**.

Current qualification matrix in `feat/intelligent-runtime-proof-loop`:

| Fault / recovery contract | Executable evidence | Current result |
| --- | --- | --- |
| server/session loss | `client/tests/test_degradation.py` session-unknown reopen + failed-reopen cases | transparent one-shot rebootstrap; typed retryable failure if reopen fails |
| server unreachable | `client/tests/test_degradation.py`, `client/tests/test_transport.py` | client-only tools degrade safely; server tools return typed retryable errors |
| stale committed view revision | `client/tests/test_sync_protocol.py::test_a_stale_revision_is_refreshed_once_rather_than_failing_the_call` | one revision repair + one retry, now recorded as recovery facts |
| future/unacknowledged revision | `client/tests/test_sync_protocol.py::test_a_revision_the_server_has_not_committed_is_refused` | refused; never guesses forward |
| disconnect before overlay commit | `test_overlay_retry_after_precommit_disconnect_commits_exactly_once` | same body/`client_seq` replayed; exactly one revision committed |
| commit succeeded, ACK lost | `test_lost_overlay_ack_replays_same_sequence_without_duplicate_mutation` | exact replay returns committed revision; zero duplicate mutation |
| blob miss | `test_a_blob_miss_is_filled_in_the_same_turn_and_retried_once` | bounded one-fill/one-retry; recovery fact emitted |
| repeated blob miss | `test_a_second_unfilled_miss_is_a_typed_refusal_not_a_loop` | typed refusal, no retry loop |
| result-reuse validation | `client/tests/test_search_dedup.py` + sync protocol reuse tests | server validator is authoritative; stale/missing client ownership falls back to execution |
| workspace changed out of band | sync protocol source-fingerprint tests | view refreshes before model consumes stale speculative result |
| search channel unavailable | `test_search_feedback` / `test_evidence_state` | `dark` is explicit and does not masquerade as absence |
| provider output truncation | `tests/gateway/cli/test_output_governor.py` | bounded extension path remains covered and emits existing optimization trace facts |
| recovery event retention/privacy | `test_recovery_event_history_is_bounded_and_content_free` | 128-event in-memory bound; no arguments/path/source fields |

The public/stub protocol suite is the deterministic authority for these edge cases and is runnable without hosted infrastructure. The real private hosted end-to-end variants in `client/tests/test_end_to_end_server.py` were also selected for qualification, but this checkout has no `enterprise/server/.venv` and the root dev environment does not contain `psycopg`; they are therefore **environment-blocked, not counted as passing**. No dependency was installed merely to make this worktree green.

## 14. Acceptance gates

### 14.1 Instrumentation gate

Before policy work:

- decision schema is stable/versioned;
- no raw prompt/source/secret duplication is introduced;
- shadow mode is behaviorally identical;
- stdio and HTTP tool lanes produce equivalent decisions for equivalent calls;
- existing benchmark/test outputs remain compatible.

### 14.2 Retrieval-policy gate

Candidate retrieval policy can enforce only when:

- frozen exact-target retrieval quality is non-inferior to current LemonCrow;
- no significant regression appears in supported language/repository slices;
- non-eligible queries add zero expansion rounds;
- eligible queries reduce model-facing search/read churn or frontier context enough to justify added local latency;
- end-to-end quality remains within the predeclared non-inferiority bound;
- deterministic fallback remains available.

### 14.3 Routing/optimization gate

The existing V2 savings levers graduate only when their current roadmap gates pass:

- enough shadow samples;
- lower accepted-change cost on eligible tasks;
- no material verified-success regression;
- provider/host compatibility demonstrated;
- rollback control proven.

Do not mark a lever complete merely because the code path exists.

### 14.4 Reliability gate

- transparent recovery tests pass for all supported thin-client failure states;
- mutation retry cannot duplicate an edit/write;
- a restart cannot produce a silent stale read;
- failure/degraded state is typed and observable;
- optimization failure remains fail-open while security policy keeps its independently defined failure semantics.

## 15. Claim gate

The engineering objective is not to manufacture a slogan. It is to make a strong claim auditable.

### 15.1 Claims already well supported

Current evidence strongly supports specific efficiency language such as:

> LemonCrow reduces coding-agent context, turns and cost while preserving or improving task quality on the published benchmark suites.

Any published percentage must continue to cite its exact suite/model/date/methodology.

### 15.2 "Intelligent runtime" gate

Use broad "intelligent runtime" positioning only when all are true:

1. the runtime emits versioned evidence/decision facts across retrieval, routing, verification and recovery;
2. at least one adaptive retrieval policy beats current LemonCrow on a controlled eligible corpus without a material quality regression;
3. at least one owned-loop adaptive policy beats the fixed/current LemonCrow control on accepted-change economics;
4. the result reproduces on more than one task family;
5. host-control tiers are disclosed so advisory and enforced decisions are not conflated;
6. the benchmark artifacts are reproducible and public where the benchmark itself is public.

### 15.3 "Best / most intelligent" gate

A superlative needs direct comparative evidence, not only a baseline win.

Before using it as an unqualified headline:

- run the same frozen task/repository/model environment against current relevant competing runtime/tooling configurations that can legally and reproducibly be benchmarked;
- include current LemonCrow control and candidate LemonCrow;
- publish failures/outliers, not only aggregate wins;
- compare quality, cost, tokens, turns and latency separately;
- do not call a quality loss a win because cost fell;
- do not combine incompatible host/model results into a fake single score;
- rerun the comparison when a material competitor/runtime generation changes.

A safer durable technical formulation is:

> **LemonCrow is the evidence-aware runtime underneath coding agents: it selects repository evidence, controls context and execution budget where the host permits it, verifies outcomes, and proves the trade-off.**

The benchmark tables can then earn stronger campaign language.

## 15A. Implementation status (worktree)

Current implementation branch: feat/intelligent-runtime-proof-loop.

- **IR-1 implemented + focused validation green**
  - shared RuntimeDecisionEvent / RuntimeDecisionSink;
  - no-op sink;
  - structural adapters on RunLedger and OptimizationTraceRecorder;
  - bounded/redacted mapping payloads.
- **IR-2 implemented + focused validation green**
  - transport-neutral SearchFeedbackPolicy;
  - bounded per-session search history moved out of the MCP composition root;
  - existing found | missed | absent | dark / breaker semantics preserved;
  - runtime decision facts emitted fail-open.
- **IR-3 implemented + focused validation green**
  - structured EvidenceState;
  - exact/rank/source/channel/truncation/cache facts;
  - real engine line/end_line references supported;
  - shadow evidence decisions emitted without changing code_search output.
- Focused IR-1..IR-3 validation: Ruff clean, 64 targeted tests passed.
- **IR-4 implemented in shadow + focused validation green**
  - bounded deterministic proposal policy;
  - hard round/token/latency proposal budgets;
  - dark/absent/truncated states stop;
  - enforce requests remain shadow until the acceptance gate exists;
  - zero extra retrieval and zero model-facing behavior change by default.
- Focused IR-1..IR-4 validation: Ruff clean, 70 targeted tests passed.
- **IR-5 implemented + focused validation green**
  - EvidenceState projects into the existing quality-router summary;
  - dark/absent retrieval cannot trigger premium escalation by evidence weakness alone;
  - independent high-risk/protected/repeated-failure rules still escalate;
  - route.model decisions are emitted into the existing ledger for outcome attribution.
- **IR-6 implemented + focused validation green**
  - CodeBench A1 control / A2 shadow arms differ only by evidence-resolution policy;
  - manifests, evidence and gates carry runtime policy fingerprints and S/H/O control tier;
  - TerminalBench off/on attribution is preserved without inventing owned control.
- Focused IR-1..IR-6 validation: Ruff/diff checks clean, 98 targeted tests passed.
- **IR-7 implemented + recovery qualification green**
  - overlay commits retry a lost acknowledgement exactly once with the same client_seq/body;
  - replay returns the original revision and cannot duplicate the mutation;
  - existing SESSION_UNKNOWN rebootstrap, stale revision repair, blob repair and stale-edit no-op semantics remain green;
  - 96/96 full public client reliability tests passed.
- **IR-8 qualification harness implemented; paid comparative evidence not run yet.**
  - A3 is an explicit benchmark-only candidate with a second experiment marker; production `enforce` still downgrades to shadow.
  - A1/A2/A3 use the same LemonCrow persona, plugin, MCP surface and host/model path; only evidence-resolution policy differs.
  - direct A1/A3 pairwise quality is generated in addition to host-baseline comparisons, so runtime-policy qualification has dedicated control-vs-candidate evidence without multiplying unrelated arm pairs;
  - each server-backed benchmark row captures a content-free `*.runtime-policy.jsonl` after preflight, so bootstrap/check traffic cannot contaminate execution evidence;
  - result rows carry observed policy event / experiment / expansion counts;
  - `runtime-policy-gate.json` is emitted whenever A1 and A3 are present and can qualify enforcement only when the manifest proves A1/A3 identity, quality + cost pass, every A1/A3 row has observed policy diagnostics, every A3 row proves `1-experiment`, A1 executes zero candidate expansions, and at least one A3 row actually exercises a bounded expansion;
  - frozen protocol: `benchmarks/codebench/protocols/intelligent-runtime-v1.json`, fingerprint `10612030a7e2f2d7bc5f7f25ba5687b93bc1739be8b89cc03ec9dac02fe1974c`;
  - every frozen task pins repository SHA **and prompt SHA-256**, so changing either fails before spend;
  - the Claude lanes pin `claude-opus-4-8` on Claude Code `2.1.197`; the Codex generalization lane pins `gpt-6-astra` on Codex CLI `0.155.1`; every pairwise quality judgment pins `claude-opus-4-8` / Claude Code `2.1.197`;
  - `lc benchmark protocol --check-hosts` is zero-spend and currently passes those local host-version pins;
  - the external CodeGraph manifest is pinned to commit `ba3c21e50d9129d2f5f3843ec3728868ae6d47a1` and manifest SHA-256 `ec783c5b228daa422f072b42bddb05ad9edcb40ed0f0efe98a527c5e9ae1ee94`; both indexing and MCP serving execute from the pinned clone, with no globally installed `codegraph` dependency;
  - the frozen matrix is 7 repos across TypeScript, Python, Rust, Java, Go and Swift: Claude A0/A1/A2/A3 at 5 reps, Codex A0/A1/A3 at 3 reps, and Claude baseline/LemonCrow/CodeGraph at 5 reps = **308 primary agent rows + 273 pairwise judge comparisons**;
  - CodeBench quality hard-gates the observed paired non-inferiority margin plus mandatory per-pair judge results; Wilson intervals remain uncertainty diagnostics rather than making a 5/5-vs-5/5 tie impossible to qualify;
  - `lc benchmark protocol --output-root ROOT` validates and renders the matrix with zero model calls / zero competitor install and pins every run to `ROOT/<run-id>` via explicit `--out`;
  - `--verify-run RUN_ID=DIR` writes `protocol-verification.json`, checks exact row cardinality, clean commit provenance and required artifacts, recomputes all applicable gates from raw evidence, and rejects stale stored gates or any frozen-protocol drift;
  - `--verify-all ROOT` verifies all three frozen lanes, requires the pinned host versions, requires every lane to pass its full run verification **and test the same clean LemonCrow commit**, writes each per-run verification plus `ROOT/publication-readiness.json`, and exits non-zero for incomplete/mixed-commit bundles.
- **Remaining IR-8 work:** execute the frozen matrix, publish raw evidence, update `BENCHMARKS.md`, then decide whether the measured scope supports stronger headline language. No production enforcement change follows merely from implementing the harness.

## 16. Concrete implementation phases

Each phase should be independently reviewable and land only after its own gate.

### IR-0 — Documentation and current-state contract

**Goal:** freeze the architecture described here before implementation.

Deliver:

- this plan;
- update runtime architecture documentation to distinguish capability runtime vs invocation runtime when implementation starts;
- document host control tiers;
- no behavior change.

Gate:

- current tests unchanged;
- no new runtime class;
- no new worktree until this document exists in `main`.

### IR-1 — Decision event contract + adapters

Likely touch points:

- new public core/foundation decision contract;
- `infra/runtime/run_ledger.py`;
- `pro/capabilities/optimization/runtime_decisions.py`;
- tool lifecycle/runtime wiring only as needed.

Deliver:

- versioned `RuntimeDecisionEvent`;
- `RuntimeDecisionSink` protocol;
- no-op sink;
- compatibility adapters to existing RunLedger/optimization traces;
- tests for redaction, serialization and bounded fields.

Gate:

- shadow/no-op behavior identical;
- no prompt/source duplication;
- existing traces still readable.

### IR-2 — Extract search feedback from MCP composition root

Likely touch points:

- `pro/capabilities/code_context/search_verdict.py`;
- a small transport-neutral session/search state module;
- `gateway/adapters/mcp_server.py`;
- `gateway/adapters/mcp/tools_search.py`;
- HTTP/stdio parity tests.

Deliver:

- move per-session search-history ownership out of `mcp_server.py`;
- adapters provide identity, not policy;
- emit decision facts for verdict/breaker events.

Gate:

- byte/semantic parity of existing model-facing verdicts;
- stdio/HTTP parity;
- no new retrieval behavior.

### IR-3 — EvidenceState in CodeContext

Likely touch points:

- new `pro/capabilities/code_context/evidence_state.py`;
- narrow additions to `engine.py`;
- code-search lean-view plumbing;
- retrieval evals.

Deliver:

- structured evidence state with stable reason codes;
- rank/channel/reference/budget signals;
- decision trace emission in shadow mode;
- no multi-round expansion yet.

Gate:

- current ranked result/output remains unchanged;
- state computation adds negligible overhead;
- frozen retrieval corpus can explain why a query was marked decisive/ambiguous/dark.

### IR-4 — Bounded deterministic evidence resolution

Likely touch points:

- new planner/policy module under `pro/capabilities/code_context/`;
- existing `CodeContextEngine` operations only;
- runtime decision tracing;
- retrieval benchmark arms.

Deliver:

- round-0 current behavior;
- bounded deterministic expansion only for ambiguous eligible states;
- shared token/latency/round budgets;
- identical public `code_search` contract;
- `off | shadow | enforce`.

Gate:

- no work on decisive/non-eligible calls;
- retrieval non-inferiority;
- measurable reduction in downstream search/read churn or context on eligible tasks;
- no material end-to-end quality regression.

### IR-5 — Join evidence to existing routing

Likely touch points:

- `quality_router/capability.py`;
- existing owned runtime policy/calibration;
- no new model router.

Deliver:

- convert `EvidenceState` into existing provider-neutral evidence summaries;
- distinguish bad evidence from hard task/risk;
- record proposed vs actual routing actions;
- maintain host-control-tier semantics.

Gate:

- MCP-only hosts remain advisory/substrate only;
- owned-loop A/B passes the savings roadmap gate;
- no route escalation merely because retrieval is dark.

### IR-6 — Runtime-attribution benchmark arms

Likely touch points:

- `core/capabilities/benchmark_manifest.py`;
- `benchmark_evidence.py`;
- `benchmark_gate.py`;
- Codebench/Harbor arm configuration;
- self-optimization harness.

Deliver:

- policy fingerprints and host-control tier in manifests;
- A0/A1/A2/A3 arm support;
- decision/outcome summary in evidence;
- current-LemonCrow-vs-candidate acceptance report.

Gate:

- instrumentation arm matches current LemonCrow;
- candidate gate is automatic and reproducible;
- full run records commit + dirty state + policy identities.

### IR-7 — Reliability qualification

Deliver:

- deterministic chaos/recovery matrix for the thin-client/runtime contracts;
- recovery decision events;
- exact duplicate-mutation assertions;
- documented degradation matrix.

Gate:

- all supported restart/stale/lost-ACK paths meet their contract;
- no hidden/manual reconnect requirement for the scenarios the protocol claims to recover.

### IR-8 — Public comparative proof

Do this only after IR-1 through IR-7 are accepted.

Deliver:

- **done:** frozen comparative protocol, including immutable task-repo revisions and prompt hashes;
- **done:** reproducible external comparator contract, currently CodeGraph pinned by source commit + manifest SHA-256 and executed from clone-local binaries only;
- **done:** zero-spend protocol + host-version validation, deterministic `ROOT/<run-id>` output layout, per-run raw-artifact/gate verification, and aggregate `publication-readiness.json` requiring one clean LemonCrow commit across every frozen lane;
- **pending external execution:** multi-host/multi-language results from the frozen 308-agent-row / 273-judge-comparison matrix;
- **pending external execution:** raw evidence links;
- **pending publication:** updated `BENCHMARKS.md`;
- **pending evidence:** only then update headline positioning.

Gate:

- claim wording exactly matches demonstrated scope;
- known regressions/outliers are disclosed;
- benchmark remains reproducible from committed configuration.

## 17. What not to build

This plan explicitly rejects the following as prerequisites for the claim:

- another LemonCrow proprietary coding agent;
- a new multi-agent/swarm framework;
- another general DAG/orchestration framework;
- a third "runtime" façade;
- a second canonical repository search/index;
- cloud LLM judging on every retrieval call;
- an online self-modifying policy;
- another trace store;
- a second session/reconnect token protocol;
- Enterprise-only intelligence required for local runtime quality;
- a giant context dump marketed as intelligence;
- host-specific copies of retrieval/routing logic.

## 18. Product-roadmap compatibility

This work does not supersede the review-first roadmap.

The relationship is:

```text
PUBLIC PRODUCT SURFACES
  lc review
  lc usage
  lc model
       |
       v
PUBLIC INTELLIGENT RUNTIME
  code intelligence
  evidence selection
  context shaping
  verification
  memory/replay
  routing where controllable
  recovery
       |
       v
HOSTS
  Claude / Codex / Cursor / Copilot / OpenCode / LemonCode / Pi / ...
```

Review remains a strong product wedge because it combines repository intelligence with execution provenance.

The runtime claim explains why the same substrate can improve many agents without asking users to adopt another agent.

## 19. Worktree and change discipline

Implementation must not begin in the main checkout.

After this document exists and implementation is explicitly started:

1. refresh `main` and record the exact base commit;
2. create one isolated worktree, suggested branch:
   `feat/intelligent-runtime-proof-loop`;
3. carry this plan into the worktree without modifying unrelated main work;
4. implement one IR phase at a time;
5. test the phase once after the coherent change set rather than repeatedly running the full suite after every small edit;
6. run expensive paid benchmarks only at the acceptance gates;
7. do not commit unless explicitly requested.

Existing concurrent/dirty work always wins over this plan; never reset, stash, clean, rebase or overwrite it blindly.

## 20. Definition of done

The program is complete when LemonCrow can demonstrate the following loop from committed, reproducible evidence:

```text
task arrives
    |
    v
runtime selects repository evidence
    |
    +-- decisive ----------> act
    |
    +-- ambiguous ---------> bounded deterministic expansion
    |
    +-- dark --------------> honest degraded state
    |
    v
runtime allocates context / route / verification budget
where the host gives it control
    |
    v
agent acts
    |
    v
runtime verifies and records outcome
    |
    v
decision + outcome feed offline replay/eval
    |
    v
candidate policy must pass a quality/cost/latency gate
before enforcement
```

At that point the "intelligent" part of LemonCrow is no longer a collection of good features or a marketing interpretation.

It is a measurable property of the runtime:

- it chooses;
- it explains the structured reason for the choice;
- it observes the outcome;
- it recovers when the environment changes;
- it improves only behind evidence gates;
- and the same substrate makes existing coding agents better without requiring LemonCrow to replace them.
