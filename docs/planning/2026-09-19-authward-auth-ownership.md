# Authward owns hosted authentication

Date: 2026-09-19

Status: implementation in progress

## End state

Hosted LemonCrow is an OAuth resource server, not an identity/token issuer.

```text
CLI / Browser / Remote MCP
          |
          v
       Authward
          |
          | Authward access token
          v
  LemonCrow resource server
          |
          | verify issuer / signature / audience / expiry / subject
          v
       Principal
          |
          v
 LemonCrow product authorization
 repo / team / review / workspace /
 entitlement / quota / audit
```

Local workstation mode stays independent:

- install-time machine credential authenticates CLI/MCP/plugins to loopback;
- local Review uses the separate Review-only browser capability;
- no Authward install and no LemonCrow account are required for local/offline use.

## Ownership boundary

Authward owns:

- human/service authentication;
- OAuth/OIDC authorization server behavior;
- access/refresh token issuance and rotation;
- user identity;
- organization membership;
- upstream enterprise SSO and SCIM;
- remote MCP OAuth authorization.

LemonCrow owns:

- validation of Authward access tokens for LemonCrow resources;
- conversion of verified identity facts into a request `Principal`;
- product-specific RBAC and resource scopes;
- repositories, projects, Review permissions and workspace ownership;
- entitlements, quotas and audit.

A LemonCrow browser BFF cookie is allowed only as a transport for an Authward-backed session. LemonCrow must not create a second independent identity/access-token family.

## Migration rules

- Do not couple this work to local-machine authentication.
- Do not move all tenancy/RBAC/SCIM behavior in the same commit that changes bearer verification.
- Preserve break-glass only as an explicit emergency exception until its final disposition is decided.
- Generic direct-to-enterprise-IdP OIDC is legacy during migration; Authward becomes the normal hosted path first.
- Every chunk must leave the server runnable and tests explicit about which credential owns authentication.
- No canonical Review URL may contain either an Authward bearer or a local machine bearer.

## Chunks

### Chunk 0 - contract and isolated worktree

- [x] Create a dedicated worktree/branch from current main.
- [x] Record Authward-vs-LemonCrow ownership.
- [x] Keep local machine auth outside this migration.
- [x] Treat the old `SessionIdentityStore` / `IdentityBroker` path as migration debt, not the target architecture.

Acceptance: later chunks can be reviewed against one written target rather than commit names such as "complete Authward authentication".

### Chunk 1 - direct Authward resource-server verification

Goal: an Authward access token can authenticate a LemonCrow API request directly. LemonCrow does not mint an `lcs_*` token on this path.

- [x] Preserve `AUTHWARD_ISSUER` explicitly in `ServerConfig` so Authward mode is distinguishable from legacy generic OIDC.
- [x] Add JWT access-token verification with exact issuer, LemonCrow resource/audience, signature/JWKS, expiry, nbf, iat and subject checks.
- [x] Support Authward's Ed25519 / `EdDSA` JWKS keys.
- [x] Add `AuthwardAccessTokenAuthenticator` that returns LemonCrow `Principal` directly.
- [x] For this transitional chunk only, reuse the existing LemonCrow org resolver and group/default-role mapping after token verification.
- [x] In Authward mode, install the direct authenticator as the hosted primary credential path.
- [x] Keep explicit break-glass as fallback only when configured.
- [x] Keep `SessionIdentityStore` / `IdentityBroker` out of direct Authward request authentication; Chunks 2-3 removed the temporary legacy CLI/browser leg, and the final cleanup deletes these types entirely.
- [x] Add a contract test proving the presented Authward bearer is the same credential accepted by protected API routes.
- [x] Add negative tests for wrong issuer, wrong resource/audience, expiry, unknown/rotated key and altered signature.

Acceptance:
- Authward bearer -> LemonCrow `Principal` directly;
- protected route accepts that bearer;
- no `lcs_*` credential is minted to make the request;
- legacy generic OIDC tests remain green until its later removal.

### Chunk 2 - CLI talks directly to Authward

- [x] Move `lc auth login` RFC 8628 discovery/device authorization from LemonCrow endpoints to Authward.
- [x] Persist Authward access/refresh credentials in the client credential store.
- [x] Refresh directly against Authward.
- [x] Send the Authward access token to LemonCrow.
- [x] Remove normal CLI dependence on LemonCrow `/v1/auth/device/*` and `/v1/auth/refresh`.
- [x] Keep migration diagnostics for an old LemonCrow `lcs_*` credential.

Acceptance: CLI login and refresh succeed while the LemonCrow auth broker routes are unavailable.

### Chunk 3 - browser Authward ownership

- [x] Make Authward own authorization-code + PKCE and refresh lifecycle.
- [x] Remove LemonCrow-generated access/refresh identity families from the browser flow.
- [x] If LemonCrow keeps an HttpOnly BFF cookie, bind it to the Authward session/token and document it as transport state, not a second identity system.
- [x] Keep CSRF/origin protections.
- [x] Preserve clean `/r/<id>` and `/rr/<id>` URLs.

Acceptance: browser Review survives refresh/sign-in without ever receiving or depending on `lcs_*`.

### Chunk 4 - remote MCP uses Authward OAuth

- [x] Stop hosted Remote MCP from advertising LemonCrow as its OAuth authorization server.
- [x] Advertise Authward authorization-server metadata for MCP.
- [x] Validate Authward MCP/resource access tokens at `/mcp`.
- [x] Remove hosted DCR/authorize/token/pairing issuance from `mcp_oauth.py`.
- [x] Keep local `lc mcp serve` behavior separate if a local-only pairing flow is still useful.

Acceptance: an MCP client authorizes with Authward and LemonCrow only validates the resulting resource token. Authward keeps one managed LemonCrow resource (`https://api.lemoncrow.com`); connector/workspace isolation is LemonCrow authorization, with each hosted connector bound to a LemonCrow `org_id + principal subject`.

### Chunk 5 - pin Authward identity; keep LemonCrow authorization local

Authward's shipped contract is identity + resource authorization, not product
RBAC. Its JWT access token carries canonical `sub`, resource-bound `aud`,
`client_id`/`azp`, a space-delimited `scope`, `iss`, `iat`, `exp`, `jti` and
optional session/binding claims. It does **not** carry LemonCrow org/team/repo
roles by default, and Authward's own architecture requires products to keep
business authorization locally.

- [x] Require Authward's coarse `lemoncrow:access` scope for LemonCrow resource tokens.
- [x] Treat Authward `sub` as the canonical external identity; never accept token-provided `org_id` or `groups` as LemonCrow authorization.
- [x] Keep organization, team, repository and Review RBAC in LemonCrow.
- [x] Keep SCIM only as LemonCrow product-membership provisioning: its `externalId` maps to Authward `sub`, and its groups produce LemonCrow role bindings.
- [x] Replace the legacy `OIDC_ORG_CLAIM` meaning on the Authward path with an explicit local mapping from Authward `sub` to LemonCrow membership.
- [x] Define a LemonCrow-owned multi-org selection contract for one Authward identity that belongs to more than one LemonCrow organization.
- [x] Remove session-revocation coupling from Authward mode; SCIM deprovisioning is checked on every Authward request. The former legacy session family is deleted rather than retained as compatibility.

Acceptance: Authward proves actor identity and coarse LemonCrow resource access;
LemonCrow alone selects the organization and decides product permissions. A
signed Authward token containing invented `org_id`/`groups` claims cannot grant
or move LemonCrow access.

Current multi-org contract: `Principal` is still single-org. A hosted Authward deployment with multiple LemonCrow orgs must therefore provide `AUTHWARD_SUBJECT_ORG_MAP_FILE`, which maps canonical Authward `sub` to exactly one LemonCrow org and fails closed for an unmapped subject. Supporting one identity in several LemonCrow orgs requires an explicit LemonCrow org-selection/session context validated against local membership; it must never be inferred from an Authward `org_id` claim.

### Chunk 6 - delete LemonCrow authentication from hosted Authward mode

- [x] Delete `SessionIdentityStore` and `SessionIdentityAuthenticator`; Authward is the only ordinary hosted credential authority.
- [x] Delete `IdentityBroker`; LemonCrow no longer mediates customer-provider sessions.
- [x] Remove LemonCrow device, refresh, revoke and logout issuer routes from the Authward router and its installed authorization/quota tables.
- [x] Remove generic direct IdP integration entirely before the first enterprise deployment; customer OIDC/SAML federation belongs in Authward.
- [x] Keep one explicit break-glass exception: an operator-provisioned static token is accepted only when break glass is configured, is audited as break glass, and never becomes an ordinary refreshable user session.

The legacy generic-OIDC classes remain deliberately isolated for on-prem/transition
compatibility. They are not reachable from an Authward-configured server and are
not part of the hosted authentication architecture. Removing that compatibility
implementation entirely can happen independently without changing hosted identity.

Acceptance: hosted Authward LemonCrow cannot mint, refresh, revoke or authenticate
an ordinary LemonCrow user access token. Its only ordinary bearer is Authward's;
optional static break glass is a separate audited emergency credential.

### Chunk 7 - deployment and release gate

- [x] Simplify Helm/Terraform identity configuration around Authward issuer/resource.
- [x] Remove Authward-mode identity-session TTL and generic-provider auth settings that no longer apply; keep SCIM as LemonCrow product-membership provisioning.
- [x] Update security-evidence source, outage/runbook architecture, deployment docs and generated deployment assets to the Authward ownership model.
- [x] Add production Authward discovery/JWKS contract tests.
- [x] Gate issuer/resource/scope/JWKS expectations against the deployed `auth.olaryn.com` contract.
- [x] Run hosted Authward/browser Review auth, CLI, Remote MCP, tenant-isolation/assurance and deployment suites; only the known separate `/r`/`/rr` baseline drift is excluded from route-equality checks.

Acceptance: docs, deployment assets, discovery, runtime and tests all describe the same ownership model.

## Commit discipline

One ownership change per commit. Do not combine:

- access-token verification with CLI login;
- CLI login with browser login;
- remote MCP OAuth migration with SCIM removal;
- local machine-auth work with hosted Authward work.

The migration should make old components unnecessary before deleting them.


## Final cleanup

- [x] Removed `legacy_oidc` as a supported server/deployment mode before the first enterprise deployment.
- [x] Deleted the LemonCrow-derived human access/refresh credential family and server-side RFC 8628 issuer routes.
- [x] Helm, preflight, assurance and current enterprise docs expose Authward plus explicit break glass only.
