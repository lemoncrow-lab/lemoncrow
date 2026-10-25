# Hosted Review: administrator guide

Hosted Review is a team workspace for your pull requests: a shared inbox where your team reads, comments on and
judges Reviews, with roles, audit history and background job status. It is invite-only and set up with you by
LemonCrow. There is no self-serve sign-up or checkout. To start, email
[contact@lemoncrow.com](mailto:contact@lemoncrow.com?subject=Hosted%20Review) with the subject `Hosted Review`. To look
around first, open the read-only demo:
[app.lemoncrow.com/demo](https://app.lemoncrow.com/demo?return_to=/reviews).

This guide takes the administrator of a new organization from the first sign-in to the first shared Review, and says
plainly what each step does today, what LemonCrow does for you and what you do yourself. The hosted app is at
[app.lemoncrow.com](https://app.lemoncrow.com/), for invited organizations. The API and the `lc` command line use
`https://api.lemoncrow.com`.

## Where Hosted Review fits

LemonCrow review is three steps. You can stop at any of them.

| Step | What it is | Status today |
| --- | --- | --- |
| 1. Local | `lc review` on your machine, with your own model key if you want AI. No account. | Available today. |
| 2. Team pull requests | The [GitHub Action](./github-action.md) reviews each pull request in your CI, on your own key, and posts the result on the pull request. | Early access. Not installable by a customer yet. |
| 3. Team workspace | **Hosted Review**: a shared inbox, roles and audit for your team's reviews of the same pull requests. | Invite-only pilot. Human review. |

AI review runs where your key is: on your machine (`lc review --ai`) or in your CI (the Action). Hosted Review is where
your team reviews together. It does not run AI review yet, and the app says so (once) instead of showing a button
that cannot work: **AI review on hosted isn't enabled for this workspace yet.** Hosted has no AI review of its own, so the Action and a hosted workspace never post competing reviews; the Action's
comments show up in the hosted Review as bot feedback, separate from human comments.

## What works, and what does not yet

Works today:

- Pull requests on your connected repositories become Reviews, and each new push becomes a new Snapshot of the Review.
- A shared inbox, with roles (Admin, Developer, Reviewer), groups, per-repository grants, suspension and removal.
- Comments, judgments and review outcomes recorded in LemonCrow, and an explicit step to publish them to GitHub
  ([section 7](#7-publish-to-github-a-separate-step)).
- Audit history, background job status, invitations and notifications.
- Sharing a Review from a checkout with `lc review` ([section 11](#11-the-command-line)).
- Your repository's `.lemoncrow/review.yaml`, read from the pull request's base branch: the same file and parser as
  local review and the Action.

Not yet: see [What is not available yet](#what-is-not-available-yet). The short list: AI review on hosted, a model key
per organization, importing local or Action history, editing Review policy in the app, linking GitHub reviewers to
members by yourself, and billing.

## Who does what

| Step | LemonCrow | You |
| --- | --- | --- |
| Create your organization, make you its first Admin, email the invitation | Yes | Reply, and sign in once |
| Connect your GitHub organization | Does it for you, unless self-service connect is on for your deployment | Where self-service is on: an owner of the GitHub organization connects it |
| Choose the repositories that sync | Does it for you, unless you use self-service connect | Where self-service is on: tick them in Settings |
| Add people and set roles | Adds members if your identity provider does not provision them (SCIM) | Invite sign-ins, set roles and groups |
| Link GitHub reviewers to members (for "To review") | Yes, it is an operator setting | Tell your contact who is who |
| GitHub App permissions | Asks for and maintains them | An owner of your GitHub organization accepts each change |
| Publish a review to GitHub | Needs the App's `Pull requests: Read and write` | Presses the button, per review |
| Merge readiness status on pull requests | Off until a repository or the operator turns it on | Opt in per repository ([section 8](#8-the-merge-readiness-status-opt-in)) |

## What LemonCrow sets up for you

LemonCrow:

- creates your organization;
- makes the administrator you named a member with the Admin role. This needs that person's LemonCrow sign-in to exist,
  so your contact may first ask them to sign in once at [app.lemoncrow.com](https://app.lemoncrow.com/) (they will see
  **Invite required**, which is expected);
- emails that administrator an invitation.

Unless your organization's Settings say otherwise (see [Connect GitHub](#2-connect-github)), LemonCrow also connects your
GitHub organization and turns on the repositories you asked for. Your contact at LemonCrow tells you which of these
they did.

## 1. Sign in for the first time

Open the invitation link and sign in with the identity it was sent to. If you signed in before your organization was
ready, you saw **Invite required** ("This LemonCrow resource is currently invite-only"). That is expected, and the
invitation clears it: open its link, or enter its code under **Have an invitation code?** and choose **Accept
invitation**.

If you are signed in but see "This account does not have access to this organization", the identity you used is not a
member yet. Sign in again with the invited identity, or ask your LemonCrow contact to add it.

After sign-in, **Home** shows where your workspace stands and the one next step (connect GitHub, choose repositories,
or open the Reviews that exist). **Settings > Setup** lists the steps that are still open and says when LemonCrow does a
step for you rather than you. A member who is not an admin is told to ask the admin instead of being shown a button.

## 2. Connect GitHub

Open **Settings > Code providers**.

- **If you see Connect GitHub**, self-service connection is on for your deployment. Enter your GitHub organization or
  account login and choose **Connect GitHub**. GitHub asks you to install the LemonCrow GitHub App (or, if it is
  already installed, to authorize it), then returns you to LemonCrow. LemonCrow checks the installation with GitHub
  before binding it, and binds one GitHub account to one LemonCrow organization only.
- **If the page says self-service installation is not available**, LemonCrow connects GitHub for you. Ask your contact
  to connect it, or to change which repositories sync.

**Only an owner of a GitHub organization can connect it.** GitHub lists an installation for anyone who can read one of
its repositories, an outside collaborator included, so LemonCrow asks GitHub to confirm that you own the organization
before it binds anything. That check needs the App to hold the organization permission **Members: Read-only**. If
GitHub will not confirm it, the page tells you in plain words:

> GitHub did not confirm that you own the GitHub organization acme, and LemonCrow only connects an organization for its
> owners. The usual cause is that the LemonCrow GitHub App is missing the organization permission "Members: read".
> LemonCrow adds it to the App; an owner of the organization then accepts the update (GitHub > organization Settings >
> Installed GitHub Apps > LemonCrow). Until then, ask LemonCrow to connect GitHub for you (contact@lemoncrow.com). If
> you are not a member of the organization, ask one of its owners to connect it.

That check is not skipped or postponed, and there is no "connect now, verify later": without it anyone who can read a
single repository could attach someone else's installation, and that organization's pull requests, to their own
workspace. The way around it is the one above: LemonCrow connects GitHub for you.

To reconnect (for example after the installation was suspended or removed on GitHub), use the same page.

## 3. Choose repositories

Pick repositories in **Settings > Code providers**, under **Repositories to sync**: tick the ones you want reviewed and
choose **Save repositories**. A newly connected installation turns on no repository by itself, and a repository you add
to the App's installation on GitHub later also stays off until you pick it. If your organization does not use
self-service connection, LemonCrow chooses the repositories for you; ask your contact to add or remove one.

**Settings > Repositories** then shows each repository's readiness. Each row shows its sync state, credential state, last
sync and last error, with **Retry sync** or **Reconnect** where they apply. Repositories are shown as `owner/name`. A
repository GitHub syncs reads **Can review now** as soon as it is picked: its pull requests come from GitHub, so it needs
no code index, and **Source / index** reads "Not needed" for it. It reads **Not ready** only when it is disabled, when
GitHub access was lost (suspended, removed or revoked: use **Reconnect**) or when its last sync failed (**Retry sync**).
"Not loaded" is the state of a repository that a developer's `lc` has not opened a source view of; it only matters for
repositories LemonCrow sets up for you that way, and then you contact LemonCrow.

## 4. Invite people, and make them members

Two separate things give someone access:

1. **A LemonCrow sign-in.** On the **Account** page, an administrator can create an invitation, optionally emailed to the
   person. An invitation only lets one person create a LemonCrow sign-in with their own identity. It does not add them
   to your organization and gives them no access to your repositories or Reviews. The panel shows each invitation's email
   state, and **Resend** issues a fresh link (at most once every ten minutes per recipient).
2. **Membership in your organization.** Either your identity provider provisions people over SCIM, or you ask LemonCrow
   to add them. Until then they see the "does not have access" message above.

Once someone is a member, set their role on the **Organization** page.

**Linking GitHub reviewers to members.** A pull request's author and the reviewers GitHub requests are GitHub accounts.
They become people in the inbox ("assigned to you", the **To review** view, a notification) only when a LemonCrow
operator has linked each GitHub account to a member. That link is an operator setting today; you cannot make it
yourself, and an organization with its own GitHub installation has none yet. Until it exists:

- every member who can read Reviews still sees every Review, and can open, comment on and judge it;
- the inbox opens on **All** instead of an empty **To review**, with one line saying why;
- nobody gets a "requested of you" entry or notification.

Tell your contact which GitHub account is which member and they set it up.

## 5. Roles

| Role | What it allows |
| --- | --- |
| Reviewer | Read Reviews, comment, and record review outcomes (approve or request changes) |
| Developer | Everything a Reviewer can, plus managing Reviews and their work |
| Admin | Everything, including the Organization page, Settings for the organization, invitations, audit and jobs |

On **Organization > People** an administrator can:

- set a member's organization role (Admin, Developer or Reviewer);
- grant a role on one repository only (Developer or Reviewer), which adds to what the member's groups give them;
- add a member to a group or remove them from one;
- suspend, reactivate or remove a member.

LemonCrow refuses a change that would leave the organization without an active administrator: "this change would leave
the organization without an active administrator; make another member an administrator first". Make someone else an
Admin first.

If your identity provider provisions the organization over SCIM, its next update for a person or group wins over a
suspension, removal or group change made on this page. Roles and repository grants set here are LemonCrow's own, and SCIM
never overwrites them.

## 6. Your first Review

Open or update a pull request in a connected repository. Its Review appears in the **Reviews** inbox for your team.
Open it to read the change, the checks LemonCrow could gather, and the comments; record comments and a verdict as a
reviewer.

Recording a comment or a verdict in LemonCrow does not post anything to GitHub. Publishing is a separate, explicit step
([section 7](#7-publish-to-github-a-separate-step)).

The Reader shows only what works on your deployment. AI review controls (**Run deeper review**, the AI panel, **Ask**)
appear only where the server reports that AI is available; where it is not, one line says so. Code, Activity and Usage
tabs are not offered while there is no coding-agent telemetry or indexed project to show.

## 7. Publish to GitHub, a separate step

To put your comments or verdict on the pull request, open the Review and choose the GitHub pull request button in the
header. Pick **Comment**, **Approve** or **Request changes**, tick the human comments to include, and publish. Only human
comments you select are published; AI findings are never posted from hosted. Each publish is keyed, so a retry never
posts twice.

Publishing needs the GitHub App's repository permission **Pull requests: Read and write**. Without it GitHub refuses, and
the sheet shows:

> GitHub refused: the LemonCrow GitHub App does not have the "Pull requests: Read and write" permission on acme/shop, so
> it cannot post reviews on pull requests. An owner of the GitHub organization can grant it under Settings > GitHub Apps >
> LemonCrow > Permissions (and accept the updated permissions), then retry. Nothing was posted.

LemonCrow changes the App's permissions; an owner of your GitHub organization accepts each change on GitHub. Then press
retry.

## 8. The merge readiness status, opt in

LemonCrow can publish a commit status named `LemonCrow / Merge readiness` on a pull request's head commit: `pending` while
review conditions are open, `success` when LemonCrow's own conditions are met, `failure` when one fails. It is **off by
default**. A pending status nothing resolves would only block a merge, so hosted posts none until a repository or your
operator turns it on, per repository:

```yaml
# .lemoncrow/review.yaml on the default branch (or the operator's repository Review policy)
landing:
  merge_status:
    enabled: true
```

Any layer that says `enabled: false` keeps it off. When it is on, the App also needs **Commit statuses: Read and write**,
and you should keep the Action's own `LemonCrow Review` status in mind: they are different statuses with different jobs.
Turning the setting off stops new statuses; a status already posted on an older commit stays as it was.

## 9. Audit history and background jobs

The **Organization** page has two more tabs for administrators:

- **Audit history** lists your organization's audit records, newest first: who did what, the decision (allow, refuse or
  error) and a reason code, with no source or comment content. You can filter by actor, event, reason, decision and time.
  The tab shows how long records are kept.
- **Background jobs** shows AI review, validation and indexing work that is queued, running or failed, and code-host
  webhook deliveries that failed or are waiting to be retried, with the repository's name. An administrator can replay a
  dead-lettered or delayed delivery from here.

GitHub Check updates are not shown on this tab. An administrator can replay a failed Check update through the API only:
`POST /v1/admin/repos/{repo_id}/github-checks/{entry_id}/replay`.

## 10. Notifications

Each person chooses their own notifications in **Settings > Notifications**: which kinds of attention notify them (a review
requested of you, work or feedback returned to you, a mention or reply, a failed required check, landing attention, a
production outcome), and whether to get them one at a time or as a digest. Pull request comments and checks on GitHub
stay the primary channel.

Email is off for everyone by default. Turning it on stores the verified email address of your current sign-in for these
notices; turning it off clears it. If turning it on is refused with "your sign-in did not grant access to your email
address; sign out and sign in again, then retry", do exactly that. Each message ends with a note that email can be turned
off on the same page.

## 11. The command line

Install the Review client pointed at the hosted API, then sign in with your invited account:

```bash
curl -fsSL https://lemoncrow.com/install.sh | bash -s -- --hosted
export PATH="$HOME/.lemoncrow/bin:$PATH"
lc auth login
```

`--hosted` installs the Review client only; it never changes your coding agents. A bare `install.sh` installs Local Review
instead, which needs no account and shares nothing. `lemoncrow.com/hosted.sh` is the same Review client installer the
`--hosted` mode runs, served from the latest release; you do not need to call it yourself.

Run `lc review` in a checkout signed in to your workspace to share a Review. What carries over from local review and the
Action, and what does not:

- **Carries over:** `.lemoncrow/review.yaml`, the same file and parser, read from the pull request's base branch (its
  `effort` is honoured).
- **Does not carry over:** review history. There is no importer for local reviews, and hosted does not read the Action's
  carried-forward findings or suppressions (kept in the pull request's summary comment). A Review shared from a checkout
  and the GitHub-connected pull request of the same repository are two separate repositories in the workspace.

## What is not available yet

- **AI review on hosted.** No hosted executor and model route are enabled, so the Reader offers no AI control; AI runs
  locally or in your CI. A **model key per organization** is not a setting either: any model route is one LemonCrow sets
  for the deployment, never yours.
- **Unattended agent execution.** A connected machine does not run agents on its own for a Review; executor-backed checks
  need separately qualified workers.
- **External sandbox providers** as a supported place to run Review work.
- **Coding-agent dashboards.** Home, Activity and Usage show no agent telemetry for a hosted organization (there is no
  tenant ingest yet), so they are not offered; Code appears once an indexed project exists. Hosted Home shows the next step
  instead.
- **Editing Review policy in the app.** Settings show the policy read-only; a repository's `.lemoncrow/review.yaml` on its base
  branch applies, and an operator sets organization policy.
- **Self-service organization creation**, linking GitHub reviewers to members yourself (an operator links them; an
  organization with its own GitHub installation has no link yet, so a pull request's author and GitHub's requested
  reviewers do not become participants and get no "requested of you" entry), importing local or Action history, and
  billing or checkout.
- **GitHub Checks** (a LemonCrow check run on the pull request) are off until separately qualified.

## For the LemonCrow operator

What the customer cannot do for itself, in the order it blocks a first user (the cutover and sign-in realm steps are in the
production rollout plans):

1. Run the production cutover for the Review webhook and subscribe the App's pull request events.
2. Activate the sign-in realm and map the first administrator to it.
3. The GitHub App's permissions, set once on github.com (an owner of each customer organization accepts changes):
   - repository **Contents: Read**, **Pull requests: Read and write** (read alone cannot publish), **Metadata: Read**;
   - **Checks: Write** only if GitHub Checks are enabled; **Commit statuses: Read and write** only for repositories that
     opt in to the merge readiness status;
   - organization **Members: Read-only**, the owner proof that self-service connect needs;
   - events: pull requests and their reviews and comments, **Installation** and **Installation repositories**;
   - a callback URL at `/settings/code-providers`, **Request user authorization during installation**, and **Any account**
     only when onboarding other GitHub organizations.
   The steps and the server settings are in [GitHub App installations per
   organization](../operations/hosted-github-installations.md).
4. Link GitHub accounts to members (the principal map) and give each new organization its members.
5. For AI on hosted, a durable queue, a managed worker and a model route. Until they exist the app hides AI, by design.

## Behaviour notes for API and integration owners

- **Review outcomes need the revision you reviewed.** `POST /v1/repos/{repo_id}/reviews/{review_id}/outcomes` requires
  `expected_revision_id`: without it the answer is 400; if it names any Snapshot other than the Review's latest, 409 with
  `details.revision_id` naming the latest.
- **Webhook header names are matched case-insensitively**, as HTTP defines, so a proxy that lowercases them does not break
  deliveries.
- **The demo is read-only.** A demo session is never an administrator.
- **Hosted session views hide raw file and artifact links**, which are shown only where the files are on your own machine.

## Security

Report a security issue to [security@lemoncrow.com](mailto:security@lemoncrow.com). If that address does not work, use
[contact@lemoncrow.com](mailto:contact@lemoncrow.com).

See also [LemonCrow Pro: Hosted Runtime](./pro-hosted-runtime.md) for hosted search in your coding agents, the
[GitHub Action](./github-action.md) for AI review in your CI, and [Privacy & network](./privacy.md).
