# Atelier plans & pricing

Atelier is **open-core** and **local-first**: the engine is source-available
([FSL-1.1-ALv2](../LICENSE)) and the SDKs, integrations, and docs are
[Apache-2.0](../LICENSE-APACHE) — all of it runs on your machine. **Free** is a genuinely
useful coding-agent runtime on its own. **Pro** unlocks the leverage — fast search
and indexing across large repos, cross-session memory, the savings engine, and
model routing. **Enterprise** adds very-large-repo scale, shared team context,
and governance.

> **Prices below are suggestions.** They are not hard-coded in the client — you
> set the real amounts in your Stripe Payment Links. The client only checks
> *which plan/features* a license grants, never the price.

## At a glance

| Capability                                                  | Free | Pro | Enterprise |
| ----------------------------------------------------------- | :--: | :-: | :--------: |
| Code-nav tools (`read`/`grep`/`search`/`edit`/…)     |  ✅  | ✅  |     ✅     |
| Host packaging, agents, skills, `init`; benchmarks          |  ✅  | ✅  |     ✅     |
| Repo map + context engine (small repos)                     |  ✅  | ✅  |     ✅     |
| Multi-repo indexing · source projection / minification VFS  |  ✅  | ✅  |     ✅     |
| Headline savings number ("you'd save $X")                   |  ✅  | ✅  |     ✅     |
| Zoekt fast search · large-repo indexing                      |  —   | ✅  |     ✅     |
| Session recall (all past sessions) · cross-vendor memory     |  —   | ✅  |     ✅     |
| Reasoning library (procedures, lessons, knowledge base)     |  —   | ✅  |     ✅     |
| Savings engine: apply + full breakdown + compression/budget |  —   | ✅  |     ✅     |
| Model routing (proxy daemon · cross-vendor · quality)        |  —   | ✅  |     ✅     |
| Multi-worktree swarm                                         |  —   | ✅  |     ✅     |
| Very large repos, no index caps · shared team context       |  —   | —   |     ✅     |
| Governance · audit export · retention · SSO                  |  —   | —   |     ✅     |

Feature keys: `src/atelier/core/capabilities/licensing/features.py`
(`PRO_FEATURES`; `ENTERPRISE_FEATURES` is the Enterprise-only subset).

## Free — $0

**For:** anyone trying Atelier, open-source work, and developers who want a
grounded coding-agent runtime without paying.

You get the full local runtime that makes any agent better: the code-navigation
MCP tools, host packaging for every supported agent, indexing across as many
repositories as you work in, the source-projection/minification VFS for
compact reads, benchmarks, and a project snapshot. The
context engine and repo map work on normal-size repos. You also see the
**headline savings number** — how much Atelier *would* save you — which is the
hook to upgrade.

No account, no key, no network call. Free is the default state of every install.

## Pro — for individual developers

**For:** a developer who wants Atelier's leverage on real, large codebases.

Everything in Free, **plus:**

- **Search & indexing at scale** — Zoekt-backed fast search and the native
  context engine + ANN symbol index for large repos.
- **Memory** — semantic **recall over all your past sessions**, and **unified
  cross-vendor memory** across Claude, Codex, and Gemini.
- **Reasoning library** — reusable procedures, promoted lessons, and the review
  knowledge base.
- **Savings engine** — apply optimization policies, the full savings
  breakdown/dashboard, context compression/dedup, prefix-cache planning,
  scoped-context pruning, and the per-session budget optimizer.
- **Model routing** — the local routing proxy daemon, cross-vendor routing, and
  quality-gated routing.
- **Orchestration** — multi-worktree swarm runs.

| Billing | Suggested price | Notes                               |
| ------- | --------------- | ----------------------------------- |
| Monthly | **$19 / mo**    | Stripe subscription; cancel anytime |
| Annual  | **$190 / yr**   | ~2 months free vs monthly           |

One person, up to three active CLI devices. Replacing a device is immediate:
remove an existing device from the account page, then sign in on the new one.

## Enterprise — contact us

**For:** teams and organizations with very large repos, shared-context needs, or
compliance requirements.

Everything in Pro, **plus:**

- **Very large repositories** with no index or symbol caps.
- **Shared team context** across repositories (unified memory shared across the
  team, not just one machine).
- **Governance** — policy enforcement, audit export, retention/redaction, and SSO.

**Pricing: custom — [contact us](https://atelier.ws/enterprise).** Enterprise
licenses carry the `enterprise` plan, which unlocks the Enterprise-only keys on
top of the full Pro set.

## Billing terms

Pro is a Stripe subscription attached to your account. A successful payment
sets the account's plan to `pro`; the CLI picks it up on the next plan check.
On cancellation or refund the webhook drops the plan and the install gracefully
falls back to **Free**.

## How to buy & activate

1. **Buy** — open the Pro purchase link (upsells point at it; override with
   `ATELIER_PRO_URL`). Pay through the Stripe Payment Link with the email you
   sign in with.
2. **Sign in** —

   ```bash
   atelier login          # browser OAuth; the account's plan unlocks Pro
   atelier login --status # show email, plan, and device slots
   atelier logout         # revert to Free (local anonymous trial)
   ```

   In CI or containers, set `ATELIER_AUTH_TOKEN=<session token>` instead.

## FAQ

**Does Pro require internet?** Only for sign-in and the periodic plan check:
the plan from `/api/auth/me` is cached locally for 24 hours, so normal
operation makes at most one auth call a day.

**What happens when my subscription lapses?** Nothing breaks: the gated
surfaces re-lock and the install behaves like Free again. Your code, memory,
and config are untouched.

**Can I use one account on multiple machines?** Yes, on up to three active CLI
devices for Pro. When all slots are in use, remove one from the account page
and sign in on the new machine.

**Refunds?** A Stripe refund drops the plan on the next webhook.

**Why is Free so capable?** The honest moat is the closed auth/payments backend
(the plan is server-anchored and checks fail closed) + being the maintainer —
not DRM on local code. Free should be good enough to trust. See
[`docs/licensing.md`](./licensing.md) for the technical design.
