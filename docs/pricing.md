# LemonCrow plans & pricing

LemonCrow is source-available and local-first: the engine runs on your machine.
The official distribution requires a free LemonCrow account at lemon init; code,
local session data, and configuration remain local.

Prices are configured in Stripe Payment Links. The client checks plan
entitlements, not prices.

## At a glance

| Capability | Free | Pro beta |
| --- | :---: | :---: |
| Code-navigation tools, host packaging, agents, skills, and benchmarks | ✅ | ✅ |
| Normal-size repo map and context engine | ✅ | ✅ |
| Local savings estimate and session replay | ✅ | ✅ |
| Large-repo search and indexing | — | ✅ |
| Session recall and cross-vendor memory | — | ✅ |
| Reasoning library | — | ✅ |
| Savings engine, context compression, and budget optimization | — | ✅ |
| Model routing | — | ✅ |
| Multi-worktree swarm | — | ✅ |

## Free — $0

For developers trying LemonCrow and using its local runtime day to day.

Free includes the local code-navigation tools, supported host packaging,
normal-size repository context, benchmarks, and local savings estimates. Create
a free account with lemon login, then activate the official install with
lemon init.

Remote telemetry is on by default to help us improve the product. Opt out
anytime with `lemon telemetry remote off`.

## Pro beta — for individual developers

For developers who use the existing gated capabilities regularly.

Everything in Free, plus large-repository search and indexing, session recall,
reasoning library, savings optimization, model routing, and multi-worktree swarm.

| Billing | Price | Notes |
| --- | --- | --- |
| Monthly | **$5 / mo** | Stripe subscription; cancel anytime |
| Annual | **$49 / yr** | Two months free versus monthly |

The official client checks the account plan. The local runtime remains
source-available; the subscription supports the maintained product and unlocks
the official Pro distribution.

## Billing terms

A successful Stripe payment sets the account plan to pro. On cancellation or
refund, gated surfaces return to Free and local data remains untouched.

## How to activate

    lemon login          # create or sign in to a free account
    lemon init           # activate the official install
    lemon login --status # show account and plan

In CI or containers, set LEMONCROW_AUTH_TOKEN to a session token.

## FAQ

**Does Free require internet?** Only to create an account and activate the
official installation. The local runtime itself works on your machine.

**Does Pro require internet?** The plan check is cached locally for 24 hours.

**What happens when Pro lapses?** Nothing breaks: gated surfaces return to Free.
Code, memory, and configuration remain untouched.

**Why charge for source-available code?** Local feature checks can be patched.
Pro is an official maintained distribution and a way to support the product—not DRM.
