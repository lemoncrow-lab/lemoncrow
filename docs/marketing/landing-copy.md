# lemoncrow.com — company landing page

_Audience: developers and engineering leaders adopting AI agents. LemonCrow is a company with two connected products: Review and Runtime. Enterprise is the deployment/governance expansion, not a third unrelated architecture._

## Nav

`Review · Runtime · Enterprise · Benchmarks · Docs · GitHub`

## Hero

# Get more from every agent.

AI agents are becoming part of how companies build and operate. LemonCrow improves the infrastructure around them so more of every run turns into useful work.

**Primary CTA:** Explore LemonCrow

Keep the company hero intentionally sparse. Do not enumerate Review, Runtime, Enterprise, workloads, hosts, benchmark numbers, or product architecture above the fold. Those belong below or on the dedicated product pages.

### Visual direction

The company homepage should not read like a sequence of text cards. Use the product's instrument-panel visual language to explain the story visually:

- hero: asymmetric layout with a live agent-loop/system diagram beside the company motto;
- infrastructure section: workload-to-agent-infrastructure diagram rather than a list of use cases;
- company principle: show the model as one component surrounded by context, execution, evidence, and human judgment;
- products: each product card gets a distinct real or diagrammatic preview (real Review Reader, Runtime flow, Enterprise topology);
- keep diagrams restrained and technical: thin borders, real labels, sparse motion, no decorative gradient blobs or fake dashboards.

## Product pages

The company homepage stays concise. Detailed product storytelling lives on dedicated pages:

- `/review` — real Review Reader, durable review state, impact/provenance/verification/comments, local install.
- `/runtime` — full efficiency proof, benchmark visualization, runtime mechanics, session replay, cross-agent workload expansion, enterprise control direction.

## Two products

### LemonCrow Review

# Understand what your coding agents changed.

Use the real Review Reader screenshot. Show impact, provenance, recorded verification evidence, and durable human review state. Primary command: `lc review --open`.

**Differentiator:** Changed work reopens. Unchanged judgments survive.

### LemonCrow Runtime

# Run agents for less, with control.

Reduce unnecessary model calls, context waste, cache misses, and execution waste. Coding-agent runtime efficiency is shipped and measured today. Broader enterprise governance builds on the same runtime and must be labeled as under active development where not yet shipped.

**Proof:** ~30% lower cost · ~25% faster on the matched SWE-bench Verified runtime evaluation.

## Shared thesis

# Agents moved the bottleneck.

Agents create two new costs at once:

- **Human attention:** more code and output to understand after the run.
- **Agent execution:** more turns, repeated context, tool noise, retries, and provider spend during the run.

Review addresses the first. Runtime addresses the second.

## Review differentiator

# The agent changed it. Do not review everything again.

Keep this section extremely simple:

`unchanged reviewed → stays reviewed · changed → reopens · new → enters the queue`

Review marks bind to content. Comments move only when re-anchoring is unambiguous. LemonCrow never silently carries a stale human judgment forward.

## Runtime proof

# Same model. Same work. Less time and cost.

**~30% lower cost · ~25% faster · without sacrificing task quality**

Exact flagship values:

- 29.5% lower cost.
- 23.7% less wall-clock time.
- 37.7% fewer turns.
- same model, tasks, containers, turn limits, and verification harness.

Do not make universal accuracy improvement the claim. Published suites include gains, a tie, and a small regression. Link to `BENCHMARKS.md`.

## Shared platform

# One intelligence layer underneath both products.

Four concise capabilities:

1. Understand code — graph, symbols, callers, dependencies, exact ranges.
2. Control context — ranked retrieval, bounded output, memory, cache-aware execution.
3. Observe execution — sessions, provenance, verification, actual provider usage.
4. Enforce policy — budgets, routing, credentials, loop controls, enterprise policy as the runtime matures.

Review is not a detached diff viewer. Runtime is not a blind proxy.

## Enterprise

# From one developer to a governed agent fleet.

Three independently valuable commercial offers share the same architecture:

- **Enterprise Server:** remote code intelligence, review, indexing, provenance, governance.
- **Enterprise Runtime:** Four-Seams model traffic control, cost policy, credential boundary, measurement, chargeback.
- **Managed Workspaces:** remote source/build/agent environments where the laptop can be only a terminal or remote IDE client.

Launch targets: customer VPC / on-prem and LemonCrow-managed dedicated tenant. Existing SSH, Coder, Kubernetes, Dev Containers, and remote IDE infrastructure should be reusable. Shared multi-tenant execution is later.

Be explicit on the homepage that the broader enterprise developer plane is under active development.

## Trust

# Keep local work local; deploy enterprise where your policy requires.

- local code graph and review state can stay on the developer machine;
- model calls use the configured provider;
- aggregate telemetry is opt-out and excludes source/prompts;
- local runtime is open source;
- enterprise architecture supports customer-controlled data planes and dedicated managed deployment.

## Final CTA

# What do you need from your agents?

**Review:** Understand what they changed.

**Runtime:** Reduce what they cost.

Enterprise link below for remote intelligence, governed workspaces, and private deployment.

## Homepage exclusions

- no live savings dashboard in hero;
- no fake Review UI: use the real screenshot;
- no full benchmark scatter plot on the company homepage;
- no session-replay section in the primary homepage flow;
- no deep runtime architecture before the two products are legible;
- no implication that Phase 11+ enterprise capabilities are already GA.
