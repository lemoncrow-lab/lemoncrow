# Hosted Review: administrator guide

This guide takes the administrator of a new Hosted Review organization from the
first sign-in to the first shared Review. It covers what LemonCrow sets up for
you, connecting GitHub, choosing repositories, people and roles, audit, jobs and
notifications.

Hosted Review is sold by the LemonCrow team and set up by invitation. There is no
self-serve sign-up or checkout. To start, email
[contact@lemoncrow.com](mailto:contact@lemoncrow.com?subject=Hosted%20Review)
with the subject `Hosted Review`. To look around first, open the read-only demo:
[app.lemoncrow.com/demo](https://app.lemoncrow.com/demo?return_to=/reviews).

The hosted app is at [app.lemoncrow.com](https://app.lemoncrow.com/), for invited
organizations. The API and the `lc` command line use `https://api.lemoncrow.com`.

## What LemonCrow sets up for you

LemonCrow:

- creates your organization;
- makes the administrator you named a member with the Admin role. This needs
  that person's LemonCrow sign-in to exist, so your contact may first ask them
  to sign in once at [app.lemoncrow.com](https://app.lemoncrow.com/) (they will
  see **Invite required**, which is expected);
- emails that administrator an invitation.

Unless your organization's Settings say otherwise (see
[Connect GitHub](#2-connect-github)), LemonCrow also connects your GitHub
organization and turns on the repositories you asked for. Your contact at
LemonCrow tells you which of these they did.

## 1. Sign in for the first time

Open the invitation link and sign in with the identity it was sent to. If you
signed in before your organization was ready, you saw **Invite required** ("This
LemonCrow resource is currently invite-only"). That is expected, and the
invitation clears it: open its link, or enter its code under **Have an invitation
code?** and choose **Accept invitation**.

If you are signed in but see "This account does not have access to this
organization", the identity you used is not a member yet. Sign in again with the
invited identity, or ask your LemonCrow contact to add it.

After sign-in, open **Settings > Setup**. It lists the steps that are still open
for your organization, and says when LemonCrow does a step for you rather than
you.

## 2. Connect GitHub

Open **Settings > Code providers**.

- **If you see Connect GitHub**, self-service connection is on for your
  organization. Enter your GitHub organization or account login and choose
  **Connect GitHub**. GitHub asks you to install the LemonCrow GitHub App (or, if
  it is already installed, to authorize it), then returns you to LemonCrow. You
  must be able to access that installation on GitHub, which GitHub allows for the
  account's owners and admins. LemonCrow checks the installation with GitHub
  before binding it, and binds one GitHub account to one LemonCrow organization
  only.
- **If the page says self-service installation is not available**, LemonCrow
  connects GitHub for you. Ask your LemonCrow contact to connect it, or to change
  which repositories sync.

To reconnect (for example after the installation was suspended or removed on
GitHub), use the same page.

## 3. Choose repositories

Pick repositories in **Settings > Code providers**, under **Repositories to
sync**: tick the ones you want reviewed and choose **Save repositories**. A newly
connected installation turns on no repository by itself, and a repository you
add to the App's installation on GitHub later also stays off until you pick it.
If your organization does not use self-service connection, LemonCrow chooses the
repositories for you; ask your contact to add or remove one.

**Settings > Repositories** then shows each repository's readiness. Each row
shows its sync state, credential state, last sync and last error, with **Retry
sync** or **Reconnect** where they apply. If a repository's source stays not
loaded, contact LemonCrow.

## 4. Invite people, and make them members

Two separate things give someone access:

1. **A LemonCrow sign-in.** On the **Account** page, an administrator can create
   an invitation, optionally emailed to the person. An invitation only lets one
   person create a LemonCrow sign-in with their own identity. It does not add them
   to your organization and gives them no access to your repositories or Reviews.
   The panel shows each invitation's email state, and **Resend** issues a fresh
   link (at most once every ten minutes per recipient).
2. **Membership in your organization.** Either your identity provider provisions
   people over SCIM, or you ask LemonCrow to add them. Until then they see the
   "does not have access" message above.

Once someone is a member, set their role on the **Organization** page.

## 5. Roles

| Role | What it allows |
| --- | --- |
| Reviewer | Read Reviews, comment, and record review outcomes (approve or request changes) |
| Developer | Everything a Reviewer can, plus managing Reviews and their work |
| Admin | Everything, including the Organization page, Settings for the organization, invitations, audit and jobs |

On **Organization > People** an administrator can:

- set a member's organization role (Admin, Developer or Reviewer);
- grant a role on one repository only (Developer or Reviewer), which adds to
  what the member's groups give them;
- add a member to a group or remove them from one;
- suspend, reactivate or remove a member.

LemonCrow refuses a change that would leave the organization without an active
administrator: "this change would leave the organization without an active
administrator; make another member an administrator first". Make someone else an
Admin first.

If your identity provider provisions the organization over SCIM, its next update
for a person or group wins over a suspension, removal or group change made on
this page. Roles and repository grants set here are LemonCrow's own, and SCIM
never overwrites them.

## 6. Audit history and background jobs

The **Organization** page has two more tabs for administrators:

- **Audit history** lists your organization's audit records, newest first:
  who did what, the decision (allow, refuse or error) and a reason code, with no
  source or comment content. You can filter by actor, event, reason, decision and
  time. The tab shows how long records are kept.
- **Background jobs** shows AI review, validation and indexing work that is
  queued, running or failed, and code-host webhook deliveries that failed or are
  waiting to be retried. An administrator can replay a dead-lettered or delayed
  delivery from here.

GitHub Check updates are not shown on this tab. An administrator can replay a
failed Check update through the API only:
`POST /v1/admin/repos/{repo_id}/github-checks/{entry_id}/replay`.

## 7. Notifications

Each person chooses their own notifications in **Settings > Notifications**:
which kinds of attention notify them (a review requested of you, work or
feedback returned to you, a mention or reply, a failed required check, landing
attention, a production outcome), and whether to get them one at a time or as a
digest. Pull request comments and checks on GitHub stay the primary channel.

Email is off for everyone by default. Turning it on stores the verified email
address of your current sign-in for these notices; turning it off clears it. If
turning it on is refused with "your sign-in did not grant access to your email
address; sign out and sign in again, then retry", do exactly that. Each message ends with a note that email can be turned off on the same
page.

## 8. Your first Review

Open or update a pull request in a connected repository. Its Review appears in
the **Reviews** inbox for your team, and outcomes recorded in LemonCrow are
published back to GitHub.

You can also share a Review from a checkout. In **Settings > Local / CLI**, the
page shows the exact commands; in short:

```bash
curl -fsSL https://lemoncrow.com/hosted.sh | bash -s -- --url https://api.lemoncrow.com
export PATH="$HOME/.lemoncrow/bin:$PATH"
lc auth login
```

Then run `lc review` in a checkout signed in to your workspace. A bare
`install.sh` (without `--hosted`) installs Local Review instead, which needs no
account and shares nothing.

## What is not available yet

- **Unattended agent execution.** A connected machine does not run agents on its
  own for a Review; executor-backed checks need separately qualified workers.
- **External sandbox providers** as a supported place to run Review work.
- **Model tasks billed by LemonCrow.** Any AI work runs on a model provider you
  configure, with your own key.
- Self-service organization creation, and per-organization mapping of GitHub
  users to LemonCrow members. With your own GitHub App installation, a pull
  request's author and GitHub's requested reviewers do not yet become LemonCrow
  participants, so they get no "requested of you" inbox entry or notification.

## Behaviour notes for API and integration owners

- **Review outcomes need the revision you reviewed.**
  `POST /v1/repos/{repo_id}/reviews/{review_id}/outcomes` requires
  `expected_revision_id`: without it the answer is 400; if it names any Snapshot
  other than the Review's latest, 409 with `details.revision_id` naming the
  latest.
- **The demo is read-only.** A demo session is never an administrator.
- **Hosted session views hide raw file and artifact links**, which are shown
  only where the files are on your own machine.

## Security

Report a security issue to [security@lemoncrow.com](mailto:security@lemoncrow.com).
If that address does not work, use [contact@lemoncrow.com](mailto:contact@lemoncrow.com).

See also [LemonCrow Pro: Hosted Runtime](./pro-hosted-runtime.md) for hosted
search in your coding agents, and [Privacy & network](./privacy.md).
