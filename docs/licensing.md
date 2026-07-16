# Licensing & feature entitlements

LemonCrow is open-core and local-first. The official distribution uses a free
account to establish a signed identity; Pro and Enterprise unlock advanced
capabilities. Savings value is not the paid boundary for an authenticated
account.

> Customer-facing plans and prices: [Plans & Pricing](./pricing.md). This page
> documents the technical entitlement design.

- **Client (Apache-2.0):** `src/lemoncrow/core/capabilities/licensing/` stores
  the OAuth session and answers whether a feature is unlocked.
- **Auth server:** the landing site's `/api/auth/*` functions and Stripe
  webhook establish the account plan and issue signed plan/cap verdicts.

## How entitlement works

`lc account login` runs browser OAuth and stores a session token at
`~/.lemoncrow/auth_token` (mode `0600`; `LEMONCROW_AUTH_TOKEN` can supply
one in CI). Plan state from `/api/auth/me` is cached locally for six hours.

Anonymous evaluation is device-bound and includes up to **$50 in measured
savings over a rolling 30-day window**. At that point LemonCrow goes dormant and
the host's built-in tools remain available. Creating or signing into a Free
account removes this savings cap from the core runtime. A valid signed verdict
is still required; missing, expired, copied, or malformed credentials fail
closed when signing is configured.

## Plans: Free / Pro / Enterprise

| Capability | Anonymous evaluation | Free account | Pro | Enterprise |
| --- | :---: | :---: | :---: | :---: |
| Grounded MCP tools, host packaging, agents, skills, hooks | ✅ | ✅ | ✅ | ✅ |
| Normal-size repo map and context engine | ✅ | ✅ | ✅ | ✅ |
| Local session replay, recall, and multi-worktree swarm | ✅ | ✅ | ✅ | ✅ |
| Savings before dormancy | $50 / rolling 30 days | Uncapped | Uncapped | Uncapped |
| Large-repo search and indexing | — | — | ✅ | ✅ |
| Cross-vendor memory and reasoning library | — | — | ✅ | ✅ |
| Optimization, compression, scoped pruning, and budget planning | — | — | ✅ | ✅ |
| Shared team context, governance, audit, SSO | — | — | — | ✅ |

Lite is no longer sold publicly. Existing Lite subscriptions remain recognized
and retain their historical grants; they are uncapped like every verified
account.

Feature keys live in
`src/lemoncrow/core/capabilities/licensing/features.py`. `session_recall`
and `swarm` are explicit Free grants. Pro gates remain on advanced surfaces
such as larger-repo indexing, knowledge extraction, deep memory, context
compression, and savings optimization.

## Account commands

```bash
lc account login        # create or sign in to a free account
lc init                 # activate the official install
lc account status       # show account and authentication state
lc account subscription # show subscription details
lc account cap          # show anonymous-cap or uncapped status
lc account logout       # remove the local session and return to anonymous evaluation
```

Execution and repository data stay local. Signed entitlement and cap verdicts
are cached for brief offline use, but expire after eight hours; after a longer
offline period the runtime fails closed until it can refresh a signed verdict.
Paid-feature checks separately fail closed to Free when no fresh verified plan
exists.

## The entitlement contract

```python
from lemoncrow.core.capabilities import licensing

licensing.is_pro()
licensing.has_feature("session_recall")  # True on Free
licensing.has_feature("optimizer")       # True only when the plan grants it
licensing.require("optimizer")
```

When adding a feature, register its key, decide whether it belongs in
`FREE_FEATURES`, `PRO_FEATURES`, or `ENTERPRISE_FEATURES`, and gate the
activation point—not a read-only preview.

## Open-core layout

Everything lives in one repository. Only paths listed in
`release/public-paths.txt` are included in the public mirror.

| Public | Private |
| --- | --- |
| Runtime, MCP server, SDK, CLI, integrations, docs, tests, benchmarks | Auth/payment services, internal planning, deployment and release machinery |
| Licensing client | Stripe and signing secrets |
