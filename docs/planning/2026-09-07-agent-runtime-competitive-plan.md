# LemonCrow Product and Execution Plan

> **Status:** rewritten for current product stage
>
> **Date:** 2026-09-07
>
> **Stage assumption:** very low user count, no proven commercialization, core must remain fully open source
>
> **Primary decision:** stop expanding LemonCrow as a broad agent runtime platform until one sharp user-facing use case has repeated pull

## 1. Executive decision

LemonCrow has already built more infrastructure than its current number of users justifies.

The near-term problem is not missing capability. The problem is that the existing capability set has not been collapsed into one obvious reason to install and keep LemonCrow.

The product should therefore narrow from:

> a general runtime engineering platform for AI coding agents

into:

> **LemonCrow makes AI-written code easier for a human to understand, review, and operate — across whatever model or coding agent they use.**

The sharpest initial user problem is:

> **I have agents writing a lot more code. Help me understand what they did.**

Review is the first high-value workflow inside that problem, not the whole product definition.

LemonCrow should show:

- what changed;
- what symbols and contracts changed;
- what callers, consumers, and neighboring code may be affected;
- what the agent actually inspected;
- what it did not inspect;
- what tests and checks actually ran;
- what model/host produced the change;
- what the run cost or consumed;
- where human review attention should go.

It should **not** try to replace the human reviewer with another AI reviewer.

The second product surface should be:

> **unified AI usage visibility across hosted and self-hosted coding models.**

The third should be:

> **easy bring-your-own-model and self-hosted model support.**

Everything else should either support those three surfaces or remain frozen until users pull it forward.

---

## 2. Why this plan is different from the previous plan

The previous runtime plan was technically coherent but too ambitious for LemonCrow's present stage. It prioritized capabilities such as portable handoff, generic execution control, worker pools, event wakeups, and cross-host orchestration.

Those may eventually be useful, but they are not the current bottleneck.

User feedback points elsewhere:

1. **Code review is becoming the bottleneck.** AI increases code output faster than humans can review it.
2. **Reviewers do not necessarily want another AI to perform the review.** They want help understanding a change faster and with less cognitive load.
3. **Cost visibility matters more broadly than cost optimization.** Some developers do not care about saving 30–50%; enterprises still need to understand where money and tokens go.
4. **Some users want model freedom rather than another hosted-agent dependency.** OpenCode, Ollama, vLLM, llama.cpp, internal gateways, and other self-hosted/open-model paths matter.
5. **Convenience wins over perfect control.** LemonCrow should not require users to maintain their own complex harness.
6. **Opaque harness behavior is frustrating.** When something goes wrong, users want to know whether the failure came from the model, host, tools, policy, or LemonCrow.

The product should therefore use the sophisticated runtime as substrate rather than expose the sophistication as the product.

---

## 3. Product thesis

### 3.1 The core problem

AI coding agents can now produce changes faster than humans can confidently understand them.

That creates a new imbalance:

```text
more agent output
      ↓
more changed code
      ↓
more review load
      ↓
less reviewer attention per change
      ↓
higher risk + lower confidence
```

Most tools respond by adding more AI review.

LemonCrow should take the opposite approach:

```text
agent writes code
      ↓
LemonCrow reconstructs change + code impact + execution evidence
      ↓
human sees the smallest useful review surface
      ↓
human makes the decision
```

### 3.2 The natural LemonCrow role

LemonCrow already sits at the intersection of:

```text
CODEBASE
  symbols
  callers
  contracts
  history
  tests
     │
     │
     ▼
LEMONCROW
     ▲
     │
     │
AI EXECUTION
  host
  model
  tools
  context
  edits
  tests
  cost
```

That makes a human review intelligence layer a natural extension.

LemonCrow does not need to own the editor, coding agent, model, Git host, or CI system.

It can understand the change better than a raw diff because it already understands both the repository and the agent execution that produced the change.

---

## 4. The three public product surfaces

For the next stage, LemonCrow should behave as if it has only three products.

### 4.1 `lc review`

**Purpose:** make an AI-generated change faster and safer for a human to review.

Not an autonomous reviewer.

Not a second AI opinion by default.

It should organize deterministic and source-linked evidence around the patch.

Target experience:

```text
$ lc review HEAD~1

Checkout authentication refactor
12 files · +384 -211

REVIEW ORDER

1. src/auth/session.py
   Core behavior changed
   - SessionManager.refresh() signature changed
   - 6 known callers
   - 2 callers outside this patch
   - high-centrality symbol

2. src/api/login.py
   Behavior affected through SessionManager
   No direct implementation change

3. tests/test_session.py
   Covers refresh path
   PASS

ATTENTION

⚠ refresh() gained required parameter `context`
  untouched call sites:
    jobs/session_cleanup.py:82
    workers/token_refresh.py:118

⚠ contract literal changed: "expired" → "invalid"
  old consumer still exists:
    frontend/session.ts:144

EXECUTION EVIDENCE

Generated with: Claude Code
Model: Claude Opus
Agent inspected: 9 files
Agent did not inspect:
  jobs/session_cleanup.py
  workers/token_refresh.py

Focused tests      PASS
Full suite         NOT_RUN
Typecheck          PASS

Human review       REQUIRED
```

#### Important design rule

The default review packet should contain **facts and relationships**, not a generated AI verdict.

Optional AI assistance may exist behind explicit actions such as:

```text
lc review explain src/auth/session.py
lc review ask "why is this caller affected?"
```

But the main output should remain inspectable without trusting another model.

---

### 4.2 `lc usage`

**Purpose:** show where AI coding usage is going across tools, models, projects, and sessions.

Cost optimization is secondary.

Target experience:

```text
$ lc usage --since 7d

AI usage · Sep 1–7

HOST / SOURCE              COST       TOKENS      SESSIONS
Claude Code              $184.22      42.1m          83
Codex                     $96.41      18.7m          31
Cursor                    $41.88      11.3m          26
Ollama / qwen3-coder       local       9.8m          19

Billed total             $322.51

BY MODEL
Claude Opus              $143.02
Claude Sonnet             $41.20
GPT-5.6                    $96.41
qwen3-coder                 local

BY PROJECT
checkout-service         $108.34
mobile                    $77.81
infra                     $63.12
...

LARGEST RUNS
...
```

Then:

```text
$ lc usage explain run-821

Why this run consumed so much

58% repeated fresh input context
21% model output
13% subagents
 8% tool-result context

Cost provenance:
provider billed
```

Only after visibility is correct should LemonCrow offer:

```text
Potential optimization: ...
```

#### Cost provenance must be explicit

Never force every model into a fake USD amount.

Every usage record should distinguish:

```text
provider_billed
api_estimated
enterprise_allocated
self_hosted_estimated
self_hosted_unpriced
unknown
```

For local models, tokens and runtime can be shown even when cost is unknown.

---

### 4.3 `lc model`

**Purpose:** make model freedom easy.

LemonCrow already supports large parts of the underlying machinery through provider discovery, LiteLLM, Ollama, OpenAI-compatible transports, routing, and OpenCode integrations.

Do not build a model server.

Productize what already exists.

Target experience:

```text
$ lc model add http://localhost:8000/v1

Found models:
✓ Qwen3-Coder-30B
✓ Devstral
✓ DeepSeek-Coder

Testing Qwen3-Coder-30B...

✓ chat completion
✓ tool calling
✓ structured JSON
✓ 128k reported context
~ vision unavailable
✓ 41 tok/s observed

Added as local/qwen3-coder-30b
```

Supported paths should include, where technically compatible:

- Ollama;
- vLLM;
- llama.cpp OpenAI-compatible server;
- LM Studio;
- OpenCode-compatible endpoints;
- company/internal OpenAI-compatible gateways;
- hosted providers already supported by LemonCrow.

The key promise is:

> **LemonCrow does not care where the model comes from.**

---

## 5. Capabilities already built but not fully realized

The best next features are mostly combinations of existing capabilities.

| Existing capability | Current role | Under-realized product value | Priority |
| --- | --- | --- | --- |
| Code graph / symbol index | agent retrieval | semantic human review | very high |
| Edit-impact analysis | agent self-correction | reviewer attention engine | very high |
| Callers/callees/usages | context retrieval | impact explanation | very high |
| Git history | agent context | explain why changed code matters | high |
| Session import/replay | memory/analytics | change provenance | high |
| Tool/run traces | agent observability | show what agent actually did | high |
| Test/validation evidence | proof gates | review evidence | high |
| Cost/token telemetry | savings | universal usage visibility | very high |
| Provider/model discovery | routing | BYO/self-hosted model UX | high |
| Context audit | optimization | context doctor | medium |
| Memory | session continuity | engineering context recall | medium |
| Worktrees | swarm isolation | safe execution primitive | medium when needed |
| Lessons | agent reuse | project convention learning | later |
| Swarm | multi-agent solving | reusable internal primitives only | freeze |
| Governance | enterprise controls | future enterprise capability | freeze |
| Team admin | enterprise state | future enterprise capability | freeze |
| Audit export | compliance | future enterprise capability | freeze |
| Routines | autonomy | future automation platform | defer |
| Worker pools | distributed execution | future infrastructure platform | defer |

---

## 6. The strongest under-realized asset: change-impact analysis

`src/lemoncrow/pro/capabilities/tool_supervision/edit_impact.py` already contains important pieces of a review engine.

It can reason about several classes of change.

### 6.1 Contract literal changes

Example:

```text
"pending" → "queued"
```

LemonCrow can detect untouched files that still consume the old contract value.

That should become a reviewer-facing signal.

### 6.2 Removed/renamed module symbols

Example:

```text
remove SessionStore.load()
```

LemonCrow can search for untouched references that may now be broken.

### 6.3 Signature changes

Example:

```text
refresh(user)
→
refresh(user, context)
```

LemonCrow can identify callers that may not provide the newly required argument.

### 6.4 Broader graph impact

The existing code map, indexed relations, call graph, centrality, cross-language edges, and symbol search can enrich this further.

The product opportunity is not another internal `FIXME` emitted to an agent.

It is:

```text
Reviewer attention
──────────────────
This patch changes a high-centrality function.
8 direct callers exist.
3 are outside the patch.
1 is in a different language boundary.
2 relevant callers were not inspected by the generating agent.
```

This is a differentiated capability because it combines static repository intelligence with agent provenance.

---

## 7. The second under-realized asset: execution provenance

LemonCrow already imports or observes sessions from multiple hosts and reconstructs substantial execution history.

Do not immediately turn that into seamless cross-host control.

Use it to answer a simpler and more valuable question:

> **How did this change get produced?**

A review packet should be able to show:

```text
Generated by: Claude Code
Model: Claude Opus
Session: 82fd...
Original task:
  "Fix intermittent session expiry"

Agent inspected:
  session.py
  redis_store.py
  expiration.py

Agent changed:
  session.py
  login.py
  test_session.py

Tests executed:
  pytest tests/session
  43 passed

Potentially affected but not inspected:
  worker/session_cleanup.py
```

This is useful without another LLM call.

It also gives LemonCrow a strong explanation story when users distrust opaque harnesses.

---

## 8. The third under-realized asset: context audit

`audit context` already compares configured MCP servers and skills against actual historical usage.

That is already close to a real product feature.

Do not rebuild it from scratch.

Eventually simplify it into:

```text
$ lc context doctor

SOURCE                  LOADED       USED        ACTION
GitHub MCP              3,120 tok      81%       keep
Postgres MCP            4,800 tok       2%       lazy-load
Kubernetes skill        2,100 tok       0%       disable by default

Observed over 38 sessions.
```

This should remain a supporting feature rather than the main reason to install LemonCrow.

---

## 9. Product architecture: substrate vs product

One of the most important discipline changes is to stop treating technically difficult infrastructure as a product merely because it was difficult to build.

### 9.1 Product surfaces

```text
lc review
lc usage
lc model
```

These should be easy to explain and demo.

### 9.2 Substrate

The following remain important, but mostly underneath:

```text
code graph
semantic search
exact reads
memory
compaction
routing
session replay
trace ledger
proof gates
worktrees
host adapters
cost tracker
provider discovery
```

Users should not need to understand all of them to understand LemonCrow.

### 9.3 Experimental/frozen substrate

Keep the code, maintain correctness, but do not spend roadmap time unless demanded:

```text
swarm
multi-wave candidate evaluation
lesson PR automation
team administration
governance UI
audit bundles
event routines
worker pools
large cross-host control plane
```

---

## 10. What is natural to LemonCrow

A feature is natural when it reuses LemonCrow's existing advantage: understanding code + understanding agent execution + remaining host/model neutral.

### Natural extensions

#### Human semantic review

Extremely natural.

Uses code graph, diff, impact analysis, traces, tests, history.

#### Change provenance

Extremely natural.

Uses imported sessions, host bridges, tool traces, model metadata.

#### Usage visibility

Extremely natural.

Uses cost tracker, token rows, session reports, host imports, telemetry.

#### BYO/self-hosted models

Natural.

Uses provider abstraction, LiteLLM/OpenAI-compatible endpoints, Ollama, routing.

#### Context doctor

Natural.

Uses context auditing, session history, tool usage, savings/context telemetry.

#### Objective verification evidence

Natural.

Uses proof gates and actual test/static-check outputs.

Important distinction:

```text
verification evidence ≠ AI reviewer verdict
```

#### Lightweight resume context

Natural if bounded.

A compact continuation artifact is useful.

A giant universal orchestration standard is not yet justified.

---

## 11. What is not natural now

### 11.1 Another coding agent

Do not compete with Claude Code, Codex, Cursor, Antigravity, OpenCode, or Copilot by building another general-purpose agent experience.

### 11.2 Another IDE

Do not build one.

### 11.3 Cloud sandbox infrastructure

Not now.

It creates a new operational product involving credentials, queues, workers, network isolation, images, secrets, and reliability.

### 11.4 Git hosting / PR hosting

GitHub/GitLab remain sources of truth.

LemonCrow can render a review packet without becoming the Git host.

### 11.5 Generic CI

Consume existing CI evidence; do not replace CI.

### 11.6 General organizational RAG

Do not become a generic Slack/Confluence/Jira knowledge warehouse.

Engineering artifacts may be referenced when needed, but the differentiation is code + change + execution intelligence.

### 11.7 AI review that creates another wall of generated prose

This conflicts directly with the user feedback.

If review is already a cognitive bottleneck, another verbose AI review often makes the bottleneck worse.

### 11.8 Fully autonomous software development platform

Too broad and too crowded for the current stage.

### 11.9 Self-hosted worker orchestration platform

Do not build Kubernetes for coding agents before users demand it.

---

## 12. What to freeze immediately

Unless a user or pilot explicitly requires them, stop feature development on:

- swarm UX and new swarm modes;
- additional agent personas;
- distributed worker pools;
- event-driven routines;
- cloud execution infrastructure;
- generalized portable orchestration;
- enterprise team-management polish;
- governance dashboards;
- audit/compliance packaging;
- generic external knowledge connectors;
- new host integrations purely for host-count marketing;
- new optimization heuristics whose only value is another percentage of savings.

Maintain bugs and critical compatibility only.

---

## 13. What to keep but simplify

### 13.1 Handoff

Do not build an ambitious cross-host runtime protocol yet.

Start with:

```text
lc resume-context <session>
```

Output:

```text
goal
important decisions
files changed
test state
important symbols
unresolved work
```

Bounded, source-linked, small.

If users repeatedly move sessions between hosts, expand later.

### 13.2 Verification

Do not market:

```text
AI says APPROVED
```

Instead show:

```text
Focused tests      PASS
Full suite         NOT_RUN
Typecheck          PASS
Lint               PASS
Migration check    UNKNOWN
Human review       REQUIRED
```

### 13.3 Savings

Keep the optimization engine.

Do not make it the primary product narrative.

Expose savings as one analysis inside `lc usage`, for users who care.

### 13.4 Memory

Keep it.

Do not lead with "five kinds of memory" as a user reason to install the product.

Use memory invisibly where it improves review context or task continuity.

---

## 14. Open-source strategy at this stage

Because LemonCrow has low adoption and no proven commercialization, making the product fully open source can be an advantage rather than a concession.

### 14.1 What open source should accomplish now

The priority is:

```text
trust
↓
installation
↓
real usage
↓
feedback
↓
repeatable use case
```

not immediate feature gating.

### 14.2 Local-first should become part of the product value

For review and usage visibility:

- repository code stays local;
- session reconstruction can stay local;
- usage records can stay local;
- self-hosted models remain first-class;
- optional AI explanation can use the user's provider/model.

This gives developers a reason to trust an open-source review intelligence tool.

### 14.3 Do not prematurely design the commercial moat around closed code

At this stage, the moat can come from:

- integration depth;
- code intelligence quality;
- review workflow quality;
- trust;
- accumulated usage;
- project/community adoption;
- enterprise deployment/support later.

Commercialization options can be revisited after pull exists.

Potential future monetization paths that remain compatible with open source include:

- hosted team usage aggregation;
- enterprise SSO/policy/administration;
- managed retention/audit;
- organization-wide review analytics;
- hosted indexing for large fleets;
- support and deployment;
- managed cloud execution only if customers demand it.

Do not build these yet merely because they are monetizable.

---

## 15. Near-term build plan: 6–8 weeks

The next phase should be intentionally small.

### Phase A — Make `lc review` compelling

This is the highest priority.

#### A1. Review packet core

Create a normalized review packet model containing:

```text
base/head SHA
changed files
changed symbols
symbol relationships
contract/signature impact
high-centrality changes
untouched affected sites
test/validation evidence
agent provenance when available
```

Possible module:

```text
src/lemoncrow/core/capabilities/review_packet.py
```

CLI:

```text
lc review [REV]
lc review --json [REV]
```

#### A2. Semantic review order

Order changed files/symbols by reviewer usefulness rather than Git path order.

Signals may include:

- centrality;
- changed public contracts;
- fan-out/caller count;
- untouched impact sites;
- production vs test;
- generated/vendor files;
- change size;
- whether the agent inspected neighboring code.

Keep ranking deterministic and explainable.

#### A3. Promote edit-impact intelligence

Expose existing contract/signature/symbol impact results directly in the packet.

Do not hide them only in agent supervision.

#### A4. Execution provenance

When a matching host session can be identified, add:

- host;
- model;
- task/prompt summary where available;
- files read;
- files changed;
- tests/commands run;
- subagents;
- relevant uninspected impacted paths.

When provenance cannot be matched confidently, say `unknown`.

#### A5. Review presentation

Start with excellent terminal output.

Then add a local HTML report only if it materially improves navigation.

Do not start with a SaaS dashboard.

### Phase B — Make `lc usage` the canonical accounting surface

#### B1. Canonical usage schema

Normalize:

```text
host
provider
model
project
session/run
timestamp
input tokens
output tokens
cache read/write
thinking tokens where known
cost amount
cost provenance
duration
tool/subagent counts
```

#### B2. Import existing sources

Reuse existing session parsers and stores rather than creating another telemetry pipeline.

#### B3. Views

Ship:

```text
lc usage
lc usage --by host
lc usage --by model
lc usage --by project
lc usage --since 30d
lc usage explain <run>
```

#### B4. Separate visibility from optimization

The first screen should never require a counterfactual savings model.

Optimization suggestions may appear below or through:

```text
lc usage optimize
```

### Phase C — Productize self-hosted models

#### C1. Model endpoint setup

Ship a simple generic setup path for OpenAI-compatible endpoints.

#### C2. Capability probe

Probe only what can be measured safely:

```text
basic completion
tool calling
structured output
reported context limit if available
latency / throughput observation
vision if testable
```

Unknown stays unknown.

#### C3. Integrate with existing routing

Do not introduce a parallel model abstraction.

Use the existing provider/routing stack.

### Phase D — Minimal runtime explainability

Do not build a huge control plane.

Add enough to answer:

```text
lc run explain <run-id>
```

Possible categories:

```text
model
provider
host/harness
lemoncrow
shell/tool
policy/permission
repository/test
unknown
```

The report should point to evidence, not guess at blame.

---

## 16. Concrete PR sequence

Keep the implementation sequence small and stop after each user-visible milestone for validation.

### PR-1 — Review packet model + raw diff integration

- review packet schema;
- changed files/hunks;
- base/head resolution;
- JSON output;
- no AI dependency.

**Release and test with real users before expanding.**

### PR-2 — Semantic change impact

- wire existing symbol/signature/literal impact analysis;
- caller/callee relationships;
- high-centrality hints;
- explicit evidence paths.

### PR-3 — Human-oriented review ordering

- deterministic review priority;
- production/test/generated grouping;
- explain why each file/symbol is high priority.

### PR-4 — Agent provenance in review packets

- reuse existing session import/replay;
- match run to repo/change when evidence permits;
- show files inspected, commands/tests, host/model;
- `unknown` when correlation is weak.

### PR-5 — Local review HTML view, only if terminal validation shows navigation pain

Do not build this automatically.

Gate it on reviewer feedback.

### PR-6 — Canonical usage read model

- unify existing cost/token/session sources;
- introduce cost provenance;
- preserve self-hosted/unpriced usage.

### PR-7 — `lc usage`

- host/model/project/session views;
- explain one run;
- no savings-first presentation.

### PR-8 — Generic self-hosted endpoint setup

- `lc model add`;
- model discovery;
- Ollama/OpenAI-compatible reuse;
- store config through existing provider system.

### PR-9 — Model capability probe

- capability evidence;
- latency/throughput observation;
- truthful unknowns;
- integrate into routing eligibility.

### PR-10 — Minimal `lc run explain`

- evidence-backed failure/run attribution;
- no generalized control plane.

### PR-11 — Rename/polish context audit as `lc context doctor`

Only after review/usage/model surfaces are working.

Reuse `audit context` internals.

### PR-12 — Lightweight resume context

Only if users are demonstrably losing work across sessions/hosts.

Do not build the larger handoff architecture unless this gets used.

---

## 17. What not to schedule yet

Do not put dates against these until users pull them forward:

- real distributed execution handles;
- generic cross-host cancellation;
- unified agent dashboard;
- persistent routines;
- event subscriptions;
- self-hosted worker pools;
- cloud sandboxes;
- enterprise policy engine expansion;
- organization-wide team administration;
- multi-agent swarm product UX;
- autonomous review bots;
- new agent hosts for breadth alone.

Existing code can remain, but roadmap attention should not.

---

## 18. Validation program

The biggest risk now is building another technically impressive feature that users do not care about.

Every PR group must have a human validation loop.

### 18.1 Review validation

Use real AI-generated changes.

Compare review with and without LemonCrow.

Measure:

```text
time to first understanding
total review time
files opened
hunks inspected
ability to explain the change
ability to identify affected code
reviewer confidence
actual defects found
```

Seed known issues in some test changes.

A faster review that misses more defects is a failure.

### 18.2 Review packet questions

After reviewing, ask:

- What did the change actually do?
- Which file did you need to understand first?
- What did you still have to investigate manually?
- Which LemonCrow signal was useful?
- Which signal was noise?
- Did LemonCrow make you more or less confident?
- Would you use this on another PR tomorrow?

The last question matters more than feature praise.

### 18.3 Usage validation

Show `lc usage` to:

- an individual heavy AI user;
- an engineering manager;
- an AI enablement/platform engineer;
- a developer using local/open models.

Ask what they would do after seeing the report.

If the report does not cause a decision or answer an existing question, it is not yet valuable enough.

### 18.4 Self-hosted model validation

Test with users already running OpenCode/Ollama/vLLM rather than convincing hosted-model users to self-host.

Success means LemonCrow removes setup friction without becoming another model-serving stack.

---

## 19. Success metrics for this stage

Do not optimize for revenue yet if commercialization is not proven.

Track product pull.

### Primary

```text
weekly active repositories
repeat `lc review` users
reviews per active repo
% of installers using LemonCrow again after 7/30 days
```

### Review-specific

```text
median review packet generation time
median human review time change
reviewer-reported usefulness
defect detection non-regression
% of review packets with useful impact evidence
```

### Usage-specific

```text
% of active users opening `lc usage`
number of hosts/models represented
% of usage with known cost provenance
repeat usage views
```

### Model-specific

```text
self-hosted endpoints configured
successful capability probes
runs using user-supplied models
```

### Avoid vanity metrics

Do not treat these as primary success metrics:

- number of supported hosts;
- number of agent personas;
- number of workflow types;
- total benchmark suite size;
- theoretical savings percentages;
- number of enterprise controls implemented.

---

## 20. Landing-page implication

The product story should become dramatically simpler.

Avoid leading with:

> 30–50% cheaper AI coding

or:

> runtime engineering platform for AI agents

A better direction is:

> **I have agents writing a lot more code. Help me understand what they did.**

Product explanation:

> **LemonCrow makes AI-written code easier for a human to understand, review, and operate — across whatever model or coding agent they use.**

Supporting copy:

> LemonCrow shows what changed, what it affects, what was tested, what the coding agent actually did, and what resources it consumed — without asking you to trust another AI reviewer.

Then secondary proof:

```text
Understand changes
See agent provenance
Track AI usage
Bring your own model
Local-first and open source
```

Cost optimization can be shown later as an additional benefit.

---

## 21. Decision framework for every new feature

Before implementing a new idea, answer these questions.

### Question 1

Does it make `lc review`, `lc usage`, or `lc model` materially better?

If no, default to **do not build**.

### Question 2

Can it be created mostly from capabilities LemonCrow already has?

If yes, strong preference.

### Question 3

Has at least one real user demonstrated the pain without being prompted into the idea?

If no, prototype or defer.

### Question 4

Does it create a new operational business?

Examples:

```text
cloud VM orchestration
hosted Git
CI
secret management
browser automation
```

If yes, defer unless it is essential to repeated demand.

### Question 5

Does it require LemonCrow to beat Claude/Codex/Cursor at their own host UX?

If yes, likely wrong direction.

### Question 6

Can the feature remain useful with AI generation turned off?

For review and observability, this is often a positive sign because it means LemonCrow is adding grounded intelligence rather than another probabilistic opinion.

---

## 22. Long-term optional evolution

If the narrow wedge gets adoption, a coherent expansion path still exists.

### Stage 1 — Review intelligence

```text
semantic diff
impact
provenance
test evidence
```

### Stage 2 — Engineering AI observability

```text
usage
context
cost
failure attribution
```

### Stage 3 — Model freedom

```text
hosted
self-hosted
internal gateways
routing by capability/policy
```

### Stage 4 — Controlled execution

Only when demanded:

```text
worktree isolation
reliable cancellation
portable continuation
```

### Stage 5 — Team / enterprise

Only after repeated team usage:

```text
shared policy
team usage
review analytics
SSO
audit
```

### Stage 6 — Autonomous infrastructure

Only if customers explicitly pull for it:

```text
event-driven routines
sandboxes
worker pools
remote execution
```

The later architecture work is not discarded. It is simply sequenced after product validation rather than before it.

---

## 23. Final product boundary

LemonCrow should not try to own software development end-to-end.

It should own the intelligence around AI-assisted changes.

The product-level problem statement is:

> **I have agents writing a lot more code. Help me understand what they did.**

The product explanation is:

> **LemonCrow makes AI-written code easier for a human to understand, review, and operate — across whatever model or coding agent they use.**

A concise boundary is:

```text
Hosts/models write code.
Git stores code.
CI runs checks.
Humans approve changes.

LemonCrow explains the change, its impact, its provenance, and its AI resource usage.
```

That is both narrower and more defensible than building another broad coding-agent platform.

The near-term goal is therefore not to prove that LemonCrow can orchestrate everything.

It is to make one behavior repeatable:

> **An engineer receives an AI-generated change, runs LemonCrow, understands the change faster, notices risks they otherwise would have searched for manually, and chooses to use LemonCrow again on the next change.**

Until that happens repeatedly, build less.