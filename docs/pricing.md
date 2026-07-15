# LemonCrow plans & pricing

LemonCrow is source-available and local-first: the engine runs on your machine.
The official distribution requires a free LemonCrow account at lc init; code,
local session data, and configuration remain local.

Prices are configured in Stripe Payment Links. The client checks plan
entitlements, not prices.

## At a glance

| Capability | Free | Lite | Pro | Enterprise |
| --- | :---: | :---: | :---: | :---: |
| Code-navigation tools, host packaging, agents, skills, and benchmarks | ✅ | ✅ | ✅ | ✅ |
| Normal-size repo map and context engine | ✅ | ✅ | ✅ | ✅ |
| Local savings estimate and session replay | ✅ | ✅ | ✅ | ✅ |
| Monthly savings before the engine goes dormant | up to $20 | up to $200 | unlimited | unlimited |
| Large-repo search and indexing | — | — | ✅ | ✅ |
| Session recall and cross-vendor memory | — | — | ✅ | ✅ |
| Reasoning library | — | — | ✅ | ✅ |
| Savings engine, context compression, and budget optimization | — | — | ✅ | ✅ |
| Model routing | — | — | ✅ | ✅ |
| Multi-worktree swarm | — | — | ✅ | ✅ |
| Shared team context, governance, audit, and SSO | — | — | — | ✅ |

## Free — $0

For developers trying LemonCrow and using its local runtime day to day.

Free includes the local code-navigation tools, supported host packaging,
normal-size repository context, benchmarks, and local savings estimates. Create
a free account with `lc account login`, then activate the official install with
lc init.

The local savings engine keeps working until it has saved you **$20 in a
rolling 30-day window**. After that the plugin goes *dormant*: it stops
force-loading its tools and the agent falls back to your host's built-in tools.
Nothing breaks and no local data is touched — the cap resets when the window
rolls over, or upgrade to keep the engine active.

Remote telemetry is on by default to help us improve the product. Opt out
anytime with `lc telemetry remote off`.

## Lite — for light daily users

For developers who want the savings to keep running but do not need the
large-repo or memory features yet.

Everything in Free, with the monthly savings cap raised to **$200 in a rolling
30-day window** before the engine goes dormant. Lite does not unlock the gated
Pro capabilities — it simply lets the free engine keep saving for far longer.

| Billing | Price | Notes |
| --- | --- | --- |
| Monthly | **$5 / mo** | Stripe subscription; cancel anytime |
| Annual | **$50 / yr** | Two months free versus monthly |

## Pro — for individual developers

For developers who use the gated capabilities regularly.

Everything in Lite, **uncapped** (the engine never goes dormant), plus
large-repository search and indexing, session recall, reasoning library,
savings optimization, model routing, and multi-worktree swarm.

| Billing | Price | Notes |
| --- | --- | --- |
| Monthly | **$20 / mo** | Stripe subscription; cancel anytime |
| Annual | **$200 / yr** | Two months free versus monthly |

The official client checks the account plan. The local runtime remains
source-available; the subscription supports the maintained product and unlocks
the official Pro distribution.

## Enterprise — for teams

For teams that need scale and governance.

Everything in Pro, plus very large repositories with no index or symbol caps,
shared team context across repositories, and governance: audit export,
retention, and SSO. Contact us for team pricing.

## Billing terms

A successful Stripe payment sets the account plan (lite / pro / enterprise). On
cancellation or refund, gated surfaces and the savings cap return to Free and
local data remains untouched.

## How to activate

    lc account login  # create or sign in to a free account
    lc init           # activate the official install
    lc account status # show account and plan

In CI or containers, set LEMONCROW_AUTH_TOKEN to a session token.

## FAQ

**Does Free require internet?** Only to create an account and activate the
official installation. The local runtime itself works on your machine.

**What is the savings cap?** Free saves up to $20 and Lite up to $200 per
rolling 30-day window; past that the engine goes dormant (falls back to host
defaults) until the window rolls over or you upgrade. Pro and Enterprise are
uncapped.

**Does a paid plan require internet?** The plan check is cached locally for a
few hours.

**What happens when a plan lapses?** Nothing breaks: gated surfaces return to
Free. Code, memory, and configuration remain untouched.

**Why charge for source-available code?** Local feature checks can be patched.
A paid plan is an official maintained distribution and a way to support the
product — not DRM.
