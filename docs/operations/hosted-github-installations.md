# GitHub App installations per organization (hosted)

Hosted Review can serve several customer organizations from one deployment and one GitHub App. Each organization connects its own installation of the App, picks the repositories that sync, and sees each repository's sync state. The deployment-wide installation configured with `LEMONCROW_SERVER_GITHUB_REVIEW_INSTALLATION_ID` keeps serving the deployment organization (`LEMONCROW_SERVER_GITHUB_REVIEW_ORG_ID`) exactly as before.

## How an organization connects

An organization admin opens Settings > Code providers, enters the GitHub organization or account login, and chooses Connect GitHub. The server creates a single-use connect state (10 minutes, stored only as a SHA-256 hash, bound to the organization and to that admin) and sends the browser to GitHub:

- when the App is not yet installed on that account, to the App's install page (`https://github.com/apps/<slug>/installations/new?state=...`). Because the App requests user authorization during installation, GitHub then asks the installer to authorize the App and redirects to the App's Callback URL with `code`, `installation_id`, `setup_action` and `state`;
- when the App is already installed there (a reconnect, or an installation made before connecting), to GitHub's authorize page (`/login/oauth/authorize?client_id=...&state=...`), which redirects to the same Callback URL with `code` and `state` only.

The page posts those values to the server, which binds the installation only after four independent proofs:

1. **The connect state.** It is consumed, refusing a forged, expired, replayed, other-admin or other-organization state (`connect_state_invalid`).
2. **The GitHub user.** The `code` is exchanged once, with the App's client id and client secret, for a user access token. That token is used only for `GET /user`, `GET /user/installations` and (for an organization) `GET /user/memberships/orgs/{org}`, and is then dropped: it is never stored, logged, audited or returned. The installation must be one this user can access, on the account the admin named. A missing or malformed code is refused (400, `user_authorization_required`), as is a code GitHub does not accept (403, `user_authorization_refused`), an installation id the user cannot access (403, `installation_not_authorized_for_user`), and an installation on another account (403, `account_mismatch`). Without an `installation_id` (the authorize flow), the installation is the user's single installation on that account. A known or guessed installation id is therefore worthless without a GitHub user who can access it.
3. **The App API.** `GET /app/installations/{id}` (App JWT) must confirm that the installation exists, belongs to this App, and is on the named account. The account is matched by login and by its immutable id, which the user's own listing must report too, so a renamed or reused login cannot stand in for it (403, `account_mismatch`).
4. **Control of the account.** Access to an installation is not control of it: GitHub lists an installation in `GET /user/installations` for anyone who can read one of its repositories, an outside collaborator included. So:
   - **Organization account.** `GET /user/memberships/orgs/{org}`, with the user's own token, must return the user's membership in that organization (same organization id) with `state` `active` and `role` `admin` (an organization owner). A member, a billing manager or a pending invitation is refused (403, `github_org_admin_required`). If GitHub answers 403 or 404 (the user is not a member, the App lacks the organization **Members: read** permission, or the organization blocks the App), the connect is refused (403, `github_org_membership_unverifiable`); Settings > Code providers shows the message telling the admin that an organization owner must connect and that the App needs Members: read. Any other answer is an outage (503, retry later), never a pass.
   - **Personal account.** The installation's account id must be the authorizing GitHub user's own id (403, `installation_not_authorized_for_user` otherwise).

Every refusal is audited as `scm.installation.refuse` with its machine reason only; no token, code or GitHub response is recorded.

After those proofs the server also refuses an installation or GitHub account already bound to another organization, and never binds the deployment-wide installation to any organization but the deployment one. It then lists the repositories the installation reaches. None is enabled until the admin picks it, except when the deployment organization binds the deployment-wide installation itself, which keeps all of its repositories syncing. The verified GitHub user is recorded as the installer, so that user's `github_app_authorization` revocation stops the binding.

Reconnect is the same flow. It replaces the organization's previous installation, whose repositories stop syncing in the same transaction. Every connect, picker change, refresh and retry is audited (`scm.installation.*`, `scm.repositories.enablement`, `scm.repository.retry_sync`). Refusals are audited as `scm.installation.refuse`, with the reason above as the reason code. All of these routes require the organization admin role, and a read-only organization keeps its 403.

## Routing and precedence

A webhook's tenant is the installation record, never the payload's repository owner:

1. An organization with its own live installation record is served by that installation only.
2. A delivery for an installation that has a record goes to that record's organization, and only for repositories the admin enabled. Other repositories are acknowledged and ignored (`repository_not_enabled`).
3. A delivery for the deployment-wide installation with no record goes to the deployment organization with all of its repositories (the pilot's behaviour). Once that organization connects another installation, deliveries for the old one are ignored (`installation_replaced`).
4. A delivery for an installation nobody connected is acknowledged and ignored (`installation`), so organization B's installation can never write into organization A.

Lifecycle events stop and restart sync:

| Event | Effect |
| --- | --- |
| `installation` `deleted` | Record becomes `deleted`; its repositories lose availability and enablement; the cached installation token is dropped. |
| `installation` `suspend` / `unsuspend` | Sync stops or resumes; Settings shows Reconnect while suspended. |
| `installation_repositories` `removed` / `added` | Removed repositories stop syncing at once; added ones become available but are not enabled until picked. |
| `github_app_authorization` `revoked` | The installations that user connected become `revoked` until reconnected. |

Each repository row in Settings > Repositories shows its last sync, last attempt, last error (credential or delivery) and credential state, recorded from webhook delivery, replay and Refresh from GitHub, with Retry sync (requeues that provider's dead-lettered deliveries for the replay loop) or Reconnect where they apply.

## Operator steps

Before deploying this release:

1. Note the App's slug (the last path segment of `https://github.com/apps/<slug>`).
2. Plan the PostgreSQL Review schema migration to v39. It is expand-only (three new tables, no change to existing rows), so a v38 replica keeps serving during the rollout. A v38 replica still ignores installations other than the deployment-wide one, and an organization it deletes leaves its v39 rows behind.

After deploying, in the GitHub App's settings (a person does this on github.com; this repository never changes it):

1. Under **Identifying and authorizing users**, set the first **Callback URL** to the hosted web app's `https://<your hosted app host>/settings/code-providers`, and turn on **Request user authorization (OAuth) during installation**. GitHub then sends installers to the Callback URL instead of the Setup URL, so the Setup URL and **Redirect on update** are unused; a reconnect goes through Connect GitHub, which uses the authorize page.
2. Generate a **client secret** and note the App's **Client ID** (both on the App's General page).
3. Under Permissions & events > **Organization permissions**, set **Members** to **Read-only**. The App's user access token needs it for `GET /user/memberships/orgs/{org}`, the proof that the connecting user is an organization owner; without it every organization connect is refused as `github_org_membership_unverifiable`. Organizations that already installed the App must approve the added permission (GitHub asks an organization owner on the installation's settings page) before their next connect. Personal-account installations do not need it.
4. Under Permissions & events, subscribe **Installation** and **Installation repositories** in addition to the events you already deliver. GitHub sends `github_app_authorization` to every App without a subscription.
5. Set **Where can this GitHub App be installed?** to **Any account** only when you intend to onboard other GitHub organizations.
6. On the server, set `LEMONCROW_SERVER_GITHUB_REVIEW_APP_SLUG=<slug>`, `LEMONCROW_SERVER_GITHUB_REVIEW_APP_CLIENT_ID=<client id>` and `LEMONCROW_SERVER_GITHUB_REVIEW_APP_CLIENT_SECRET_FILE=<path>`, then restart. The secret file is a private mount checked like the App's private key (`chmod 600`). The client id and secret file are set together and require the slug, and the slug requires App authentication (`LEMONCROW_SERVER_GITHUB_REVIEW_APP_ID`). With the slug or the client secret missing, connect stays hidden and refused (HTTP 501), and the deployment-wide installation behaves exactly as before.

## Retention

- Installation records are kept after `deleted`, `revoked` or `replaced` as connection history (the last ten are shown). They hold no credential; the installer is kept as a GitHub user id only.
- Repository rows keep their last sync and last error after a repository is removed, with availability and enablement cleared.
- Connect states are deleted when used and pruned when expired.
- The GitHub user access token from the callback is never persisted.
- Deleting an organization (`delete_org` on PostgreSQL; the organization's directory, including `scm_installations.db`, on SQLite) removes all three.

## GitHub users and LemonCrow principals

The deployment's principal map (`LEMONCROW_SERVER_GITHUB_REVIEW_PRINCIPAL_MAP_FILE`) maps GitHub logins to principals of the deployment organization only. An organization's own installation maps no GitHub user. For that organization:

- **What works.** A pull request on an enabled repository still becomes a Review in that organization. Every member whose organization role can read Reviews sees it in the Reviews list and can open it, annotate it, and act on it, and an approval or request for changes is published back to GitHub. GitHub checks, publishing and refetches use that installation's App token, so they are unaffected.
- **What is weaker.** The pull request author does not become the Review's author participant, so the Review is not in the author's own changes and author-directed attention does not reach them. GitHub requested reviewers and CODEOWNERS do not become LemonCrow review requests, so nobody gets a "requested of you" inbox entry or a review-request notification. Reviews submitted on GitHub are recorded without a LemonCrow principal, and source-trust evidence names the GitHub login instead of a principal.

## Not supported yet

- Per-organization GitHub login-to-principal mapping (see above).
- Proof of GitHub account administration: the connecting user must be able to access the installation (it is listed by `GET /user/installations`), which GitHub grants to the account's owners and admins and to members with access to a repository through it. A member of a GitHub organization who is also an admin of a LemonCrow organization could therefore connect that installation first. The installation and account uniqueness rules then keep it out of every other organization until it is released, and the connect is audited with the GitHub user as installer.
- App authentication still requires the deployment-wide installation id.
- Self-service organization creation.
