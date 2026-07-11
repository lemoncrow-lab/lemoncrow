# Licensing & Pro features

LemonCrow is source-available and runs locally. Existing paid Pro control surfaces
are gated behind the signed-in account's plan. Free remains genuinely useful;
Pro beta covers the existing gated capabilities.

> Customer-facing plans and prices: [Plans & Pricing](./pricing.md). This page
> documents the technical entitlement design.

- **Client (Apache-2.0):** `src/lemoncrow/core/capabilities/licensing/` — holds
  the OAuth session and answers "is this feature unlocked?".
- **Auth server (proprietary):** the landing site's `/api/auth/*` functions
  plus the Stripe webhook. Google OAuth signs the user in, Stripe payments set
  the account's plan, and `/api/auth/me` reports `{email, plan, device_id}` to
  the CLI.

## How entitlement works

`lemon login` runs a browser OAuth flow against the auth server and stores a
session token at `~/.lemoncrow/auth_token` (mode `0600`; override with the
`LEMONCROW_AUTH_TOKEN` env var — handy for CI). Entitlement checks read the plan
from `/api/auth/me`, cached on disk for 6 hours (`~/.lemoncrow/auth_user.json`),
so normal operation makes a handful of auth calls a day. If the server is
unreachable and no fresh cache exists, gated surfaces stay locked and the check
retries hourly; Free surfaces are never affected.

`lemon logout` deletes the session and reverts to Free. There are no offline
license keys, no device-bound leases, no local crypto, and no dev backdoor —
the account's plan is the single source of truth, and every check fails closed
to Free.

## Free vs Pro beta

| Capability | Free | Pro beta |
| --- | :---: | :---: |
| Code-nav MCP tools, host packaging, agents, skills, and benchmarks | ✅ | ✅ |
| Normal-size repo map and context engine | ✅ | ✅ |
| Local savings estimate and session replay | ✅ | ✅ |
| Large-repo search and indexing | — | ✅ |
| Session recall and cross-vendor memory | — | ✅ |
| Reasoning library | — | ✅ |
| Savings engine, compression, and budget optimization | — | ✅ |
| Model routing | — | ✅ |
| Multi-worktree swarm | — | ✅ |

Feature keys remain in src/lemoncrow/core/capabilities/licensing/features.py.
Customer-facing plans and prices: [Plans & Pricing](./pricing.md).

A free account is required to activate the official install:

```bash
lemon login          # browser OAuth; stores the session token
lemon init           # activates the official install
lemon login --status # show account and plan
lemon logout         # removes the local session
```
A Pro beta account supports up to **three active CLI devices**; the auth server
tracks the slots. LEMONCROW_PRO_URL overrides the buy link shown in upsells.
## The entitlement contract

Every gate calls one tiny API:

```python
from lemoncrow.core.capabilities import licensing

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

Current gates (reference): `lemon optimize apply`, `lemon savings --deep`,
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
