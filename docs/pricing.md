# LemonCrow plans & pricing

LemonCrow is open-core and local-first: code, indexes, session data, and
configuration stay on your machine. The official ongoing Free plan requires a
free LemonCrow account.

## Access model

- **Anonymous evaluation:** use the local core until LemonCrow has measured $50
  in savings over a rolling 30-day window. It then goes dormant without
  touching your code or data.
- **Free account:** uncapped core runtime for one developer, including local
  session recall and multi-worktree swarm.
- **Pro:** advanced individual capabilities for large codebases, cross-vendor
  workflows, and optimization.
- **Enterprise:** shared context, governance, and organizational scale.

Lite is no longer offered. Existing Lite subscriptions remain recognized for
backward compatibility.

## At a glance

| Capability | Free | Pro | Enterprise |
| --- | :---: | :---: | :---: |
| Grounded tools, agents, skills, hooks, and verification | ✅ | ✅ | ✅ |
| Normal-size repo map and context engine | ✅ | ✅ | ✅ |
| Local session replay and recall | ✅ | ✅ | ✅ |
| Local multi-worktree swarm | ✅ | ✅ | ✅ |
| Savings dormancy after account sign-in | Never | Never | Never |
| Larger-repo search and semantic indexing | — | ✅ | ✅ |
| Cross-vendor memory and reasoning library | — | ✅ | ✅ |
| Context compression, scoped pruning, and savings optimization | — | ✅ | ✅ |
| Full savings history, budget and prefix-cache planning | — | ✅ | ✅ |
| Shared team context, governance, audit, and SSO | — | — | ✅ |

## Free — $0

For individual developers, including personal and commercial work.

Create an account with `lc account login`, then run `lc init`. Free includes
the grounded tool loop, normal-size repository context, local session recall,
local swarm runs, verification hooks, replay, and uncapped measured savings.
There is no value-based dormancy after sign-in.

Remote product telemetry uses aggregate operational data and can be disabled
with `lc telemetry remote off`.

## Pro — for advanced individual use

Pro adds the capabilities that have a distinct power-user cost or maintenance
surface: larger-repository search and semantic indexing, cross-vendor memory,
reusable reasoning and knowledge, savings-policy optimization, context
compression, scoped pruning, and budget and prefix-cache planning.

| Billing | Price | Notes |
| --- | --- | --- |
| Monthly | **$20 / mo** | Stripe subscription; cancel anytime |
| Annual | **$200 / yr** | Two months free versus monthly |

Regional CHF and EUR prices are shown on the pricing page.

## Enterprise — for teams

Enterprise adds very large repositories without index or symbol caps, shared
team context across repositories, role-based access, governance, audit export,
retention, SSO, and priority support. Contact contact@lemoncrow.com for
pricing.

## Billing and activation

A successful Stripe payment sets the account to Pro. Cancellation or refund
returns advanced surfaces to Free; local code, indexes, memory, and
configuration remain untouched.

```bash
lc account login  # create or sign in to a free account
lc init           # activate the official install
lc account status # show account and plan
```

In CI or containers, set `LEMONCROW_AUTH_TOKEN` to a session token.

## FAQ

**Why keep anonymous access capped?** It gives a developer room to evaluate the
runtime without turning anonymous use into an unbounded production channel. A
free account removes the cap.

**Does Free require internet?** Internet is required to create/sign into the
account and periodically refresh the signed identity. Runtime work, parsing,
indexing, recall, and swarm execution are local.

**Why pay for Pro if Free is uncapped?** Pro is feature-gated, not value-metered.
It is for larger repos, cross-vendor memory, optimization, compression, budget
planning, and reusable knowledge.

**What happens when Pro lapses?** Advanced surfaces return to Free. Code, local
memory, and configuration remain untouched.
