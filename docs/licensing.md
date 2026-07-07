# Licensing & Pro features (open-core)

Atelier is open-core: the entire runtime is Apache-2.0 and runs locally. A small
set of **paid ("Pro") control surfaces** are gated behind the signed-in
account's plan. The split is designed so the Free tier is genuinely useful (and
already delivers most of the token savings) while the incremental optimizer and
the full savings dashboard are paid.

> Looking for the customer-facing plan breakdown and prices? See
> [**Plans & Pricing**](./pricing.md). This document is the technical design.

- **Client (Apache-2.0):** `src/atelier/core/capabilities/licensing/` — holds
  the OAuth session and answers "is this feature unlocked?".
- **Auth server (proprietary):** the landing site's `/api/auth/*` functions
  plus the Stripe webhook. Google OAuth signs the user in, Stripe payments set
  the account's plan, and `/api/auth/me` reports `{email, plan, device_id}` to
  the CLI.

## How entitlement works

`atelier login` runs a browser OAuth flow against the auth server and stores a
session token at `~/.atelier/auth_token` (mode `0600`; override with the
`ATELIER_AUTH_TOKEN` env var — handy for CI). Entitlement checks read the plan
from `/api/auth/me`, cached on disk for 6 hours (`~/.atelier/auth_user.json`),
so normal operation makes a handful of auth calls a day. If the server is
unreachable and no fresh cache exists, gated surfaces stay locked and the check
retries hourly; Free surfaces are never affected.

`atelier logout` deletes the session and reverts to Free. There are no offline
license keys, no device-bound leases, no local crypto, and no dev backdoor —
the account's plan is the single source of truth, and every check fails closed
to Free.

**Honest threat model.** The runtime is open-source, so any client-side
check can be edited out by a determined user — gating is friction for honest
installs, not DRM. The server-anchored plan means every unmodified install
converges on the truth within hours; the real moat is the closed auth/payments
backend, updates, and being the maintainer.

## Free vs Pro vs Enterprise

| Capability                                                  | Free | Pro | Ent |
| ----------------------------------------------------------- | :--: | :-: | :-: |
| Code-nav MCP tools (`read`/`grep`/`search`/`edit`/…) |  ✅  | ✅  | ✅  |
| Host packaging, agents, skills, `init`; benchmarks          |  ✅  | ✅  | ✅  |
| Repo map + context engine (small repos)                     |  ✅  | ✅  | ✅  |
| Headline savings number                                     |  ✅  | ✅  | ✅  |
| Zoekt fast search; large-repo indexing; projection VFS      |  —   | ✅  | ✅  |
| Session recall + cross-vendor memory                        |  —   | ✅  | ✅  |
| Reasoning library (procedures, lessons, knowledge)          |  —   | ✅  | ✅  |
| Savings engine: apply + full breakdown + compression/budget |  —   | ✅  | ✅  |
| Model routing (daemon, cross-vendor, quality)               |  —   | ✅  | ✅  |
| Multi-repo; multi-worktree swarm                            |  —   | ✅  | ✅  |
| Very large repos (no caps); shared team context             |  —   | —   | ✅  |
| Governance, audit export, retention, SSO                    |  —   | —   | ✅  |

The feature keys are in `src/atelier/core/capabilities/licensing/features.py`
(`PRO_FEATURES`, with `ENTERPRISE_FEATURES` the Enterprise-only subset). For the
customer-facing plans and prices see [Plans & Pricing](./pricing.md).

## Signing in

```bash
atelier login          # browser OAuth; stores the session token
atelier login --status # show email, plan, and device slots
atelier logout         # revert to Free (local anonymous trial)
```

A Pro account supports up to **three active CLI devices**; the auth server
tracks the slots. `ATELIER_PRO_URL` overrides the "buy" link shown in
upsells — point it straight at your Stripe Payment Link.

## The entitlement contract

Every gate calls one tiny API:

```python
from atelier.core.capabilities import licensing

licensing.is_pro()                    # bool
licensing.has_feature("optimizer")    # non-Pro keys are always True
licensing.require("optimizer")        # raises FeatureLocked if the plan doesn't grant it
```

Both check the signed-in account's **plan** — nothing else. Use
`require_pro()` (`gateway/cli/commands/_shared.py`) for CLI surfaces; it wraps
`has_feature` with the upsell message.

## Gating a new feature

1. **Core:** add the key + description to `PRO_FEATURES` in `features.py`.
2. **Seam:** at the point that *activates* the capability, branch on
   `licensing.has_feature("<key>")` (or call `licensing.require("<key>")` /
   `require_pro()` for a hard gate with an upsell). Prefer gating the
   **write/apply/activate** action, not read-only previews — let users measure
   the value before they pay for it.

Current gates (reference): `atelier optimize apply`, `atelier savings --deep`,
and the `require_pro` CLI gates (recall, router, zoekt, knowledge, swarm,
memory).

## Open-core layout (what's public vs private)

Everything lives in **one repo**. Only paths listed in
`release/public-paths.txt` are included in the public mirror
(`scripts/mirror.py`). Everything else is private by default — a new directory
never leaks unless it's explicitly allowlisted.

| Public (included via `public-paths.txt`)           | Private (excluded by default)                                      |
| -------------------------------------------------- | ------------------------------------------------------------------ |
| Whole runtime, MCP server, SDK, CLI                | `internal/`, `docs-internal/`, `.planning/` (strategy)             |
| License **client** (`licensing/`)                  | `services/` — auth/payments backend (Stripe, D1)                   |
| `docs/`, `integrations/`, `tests/`, benchmarks     | `deploy/`, `release/` (publish machinery)                          |
| `frontend/`, `landing/`, `docs-site/`              | Stripe secrets (never committed)                                   |
