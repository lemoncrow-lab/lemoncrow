# Enterprise tier — unfiltered benefit list

_For sales packaging. Internal. Not a public promise sheet._

**Status legend:** `SHIPPED` = in the product today · `ADD-ON` = small build, days · `BUILD` = real project, weeks · `SELL` = commercial/process, no code.

Rule for calls: quote `SHIPPED` freely. `ADD-ON` may be promised as part of a
signed deal. `BUILD` is a roadmap conversation, never a close-the-deal promise.

---

## 1. Cost control and ROI proof

| Benefit | Status |
| --- | --- |
| Per-session and aggregate savings with counterfactual pricing (`lc savings`, `lc dashboard`) | SHIPPED |
| Weekly spend-trend and opportunity summary (`lc insights`) | SHIPPED |
| Team usage rollup per member (`lc team usage`) | SHIPPED |
| Org spend dashboard split by team / repo / host / model | ADD-ON |
| Chargeback / showback export (CSV + API) for finance | ADD-ON |
| Budget caps per seat, team, or repo with hard stop or soft warn | ADD-ON |
| Spend alerts to Slack / webhook / email on threshold breach | ADD-ON |
| Model routing policy — cheap model for search and boilerplate, frontier for hard tasks | ADD-ON |
| Cache-affinity / prefix-cache planning for warmer provider caches | SHIPPED |
| Quarterly ROI report generated from their own telemetry | SELL |

## 2. Governance, policy, and audit

| Benefit | Status |
| --- | --- |
| Governance policy file with per-data-class retention (`governance.yaml`, `lc governance show/apply`) | SHIPPED |
| Redaction rules on stored data (API keys, secrets, custom regex) | SHIPPED |
| Signed audit bundle export and verification (`lc audit export`, `lc audit verify`) | SHIPPED |
| Team audit log of workspace actions (`lc team audit`) | SHIPPED |
| Role-based permissions on shared memory (admin / member / viewer) | SHIPPED |
| Google OIDC sign-in | SHIPPED |
| Tool allow/deny policy enforced by the runtime (block raw shell, block web fetch, force bounded output) | ADD-ON |
| Model allowlist — org decides which providers/models are usable | ADD-ON |
| Mandatory verification gate — agent cannot declare done without tests/lint passing | ADD-ON |
| Org policy pinned in the repo and validated in CI (drift = failed build) | ADD-ON |
| Prompt/output DLP scanning before it leaves the machine | ADD-ON |
| SAML / Okta / Entra SSO + SCIM provisioning | BUILD |
| Hosted admin console (policy, seats, spend, audit in a browser) | BUILD |
| SOC 2 Type II | SELL |

**Enforcement reality — say this out loud on calls.** A runtime a developer
installs cannot govern a developer who does not install it. Enforcement paths we
can offer: (a) ship it in the managed dev image / dotfiles, (b) CI check that
fails PRs produced without the policy, (c) route agent traffic through the
LemonCrow gateway so the policy sits on the network path, not the laptop.
Gateway routing is the only true hard enforcement — that is an upsell, not a
footnote.

## 3. Shared team context

| Benefit | Status |
| --- | --- |
| Shared team context across repositories | SHIPPED |
| Team workspace lifecycle — init, invite, join, roles | SHIPPED |
| Source-linked memory with commit provenance | SHIPPED |
| Lesson promotion with PR bot — verified learnings reviewed like code | SHIPPED |
| Review knowledge base / reusable procedures | SHIPPED |
| Staleness detection and memory arbitration | SHIPPED |
| Cross-vendor memory — same context across Claude Code, Codex, Gemini hosts | SHIPPED |
| Onboarding accelerator: new hire inherits the team's working set on day one | SELL (positioning of the above) |
| Jira / Confluence / GitHub issue pull into the task working set | BUILD |
| Encrypted cross-machine workspace sync | BUILD |
| Hosted LemonGraph viewer for the org's code universe | BUILD |

## 4. Scale

| Benefit | Status |
| --- | --- |
| Very large repos with no index or symbol caps | SHIPPED |
| Zoekt trigram backend for monorepos | SHIPPED |
| Proven scale: Linux kernel core, 1.24M symbols / 4.5M lines, cold index in 179s | SHIPPED |
| PostgreSQL + pgvector backend for team-scale storage | SHIPPED |
| CI-warmed shared index artifact — devs pull a prebuilt index instead of indexing locally | ADD-ON |
| Cloud-side indexing for repos too large to index on a laptop | BUILD |

## 5. Deployment and security posture

| Benefit | Status |
| --- | --- |
| Fully local — parsing and indexing never leave the machine; no code upload | SHIPPED |
| Model calls still go to the provider the customer already approved | SHIPPED |
| Apache-2.0, readable source, engine included — no black box in the review | SHIPPED |
| Anonymous telemetry off with one command (`lc telemetry remote off`) | SHIPPED |
| Self-hosted service + gateway (OpenAI-compatible `/v1/chat/completions`) | SHIPPED |
| Air-gapped install bundle with offline license | ADD-ON |
| VPC / customer-cloud deployment guide + terraform | BUILD |
| Security questionnaire pack, DPA, pen-test summary | SELL |

## 6. Quality and adoption analytics

| Benefit | Status |
| --- | --- |
| Outcome capture for agent runs | SHIPPED |
| Session replay — show where an agent wandered without rerunning a model | SHIPPED |
| Benchmark gate on their own repo (`lc benchmarks`) | SHIPPED |
| Adoption dashboard: seats active, sessions, tokens saved, tasks completed | ADD-ON |
| Quality metrics: verification pass rate, rework rate, turns per task | ADD-ON |
| Before/after pilot report on the customer's own repo, run by us | SELL |

## 7. Commercial and service

| Benefit | Status |
| --- | --- |
| Priority support with SLA | SELL |
| Named onboarding engineer, install + policy setup session | SELL |
| Custom agents, personas, and skills built for their stack | SELL (build is fast) |
| Private benchmark run on their codebase before purchase | SELL |
| Shared Slack / Teams channel | SELL |
| Annual contract, invoicing, procurement paperwork | SELL |
| Influence on roadmap / design-partner status for early customers | SELL |

---

## Packaging suggestion (for Guido to price)

**Team** — shared context, RBAC, team usage rollup, priority support.
Seat-based. Land here.

**Enterprise** — everything in Team plus governance policy, audit export,
retention, SSO, no-caps large-repo, self-host/air-gap, org spend dashboard and
budget caps, SLA. Custom priced, annual.

**Enforcement upsell** — gateway-routed deployment: policy on the network path,
not the laptop. Sell to platform/security, not to the dev team.

## Objection notes

- _"We already have cloud credits."_ → Then sell correctness and speed, not
  cost: +12.0pp resolved, 37.7% fewer turns, 23.7% faster on SWE-bench Verified.
- _"Does our code leave?"_ → No. Indexing is local, source is Apache-2.0 and
  readable, model calls go to the provider they already use.
- _"How do we stop devs bypassing it?"_ → See enforcement reality above. Do not
  overclaim laptop-side enforcement.
- _"Can you build X for us?"_ → Anything in `ADD-ON` is days. Say that.
