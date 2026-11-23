# Production and self-hosting notes

Normal local Workspaces plugin use does not require a LemonCrow application server. This page is for teams deliberately running Review/shared Workspaces APIs, workers, Postgres-backed deployments, hosted services, or Enterprise infrastructure.

## Before exposing a shared service

- run the repository verification gates for the exact build you plan to deploy;
- configure authentication before exposing the service beyond loopback/private infrastructure;
- use TLS at the network boundary;
- keep provider credentials and LemonCrow service credentials in the deployment's secret store;
- verify backup/restore before production rollout;
- document the rollback path for the release.

## Service commands

Shared HTTP services live under the advanced Workspaces namespace:

```bash
lc rt service --help
lc rt worker --help
```

They are separate from the normal local Workspaces plugin workflow.

## Storage

### SQLite

Use SQLite for local/single-node deployments where one machine owns the store.

Back up the LemonCrow state directory before schema-changing upgrades.

### PostgreSQL

Use Postgres for shared/multi-writer deployments. Before rollout:

- back up the database;
- run migrations in staging;
- verify the restore path;
- validate tenant/auth boundaries where applicable.

pgvector is optional unless the selected deployment explicitly enables a vector-backed capability.

#### Metadata schema 10 (production observation)

Metadata schema 10 lets the hosted job queue hold `production_observation`
jobs by widening the execution-class checks on `lc_hosted_jobs` and
`lc_hosted_job_dispatch_cursor`. The first replica that starts on this
release applies it:

1. Inside the migration transaction, it adds the widened checks `NOT VALID`.
   This is a catalog change, so the tables are not scanned while the DDL holds
   its exclusive lock.
2. After the transaction commits, it validates both checks. Validation scans
   the tables under a lock that lets reads and writes continue. Every existing
   row already passes, because the old checks were narrower. Later replicas
   find the checks validated and skip this step.

A schema-9 server still runs against a schema-10 database, but it cannot read a
`production_observation` job. Upgrade every API replica and the managed
review controller first. Only then set `"production_observation": true` for a
worker in the customer worker routes file.

Before you roll any replica back to an earlier release, remove that
designation and wait until no `production_observation` job is queued or
running.

#### Metadata schema 11 (job priority and retention)

Metadata schema 11 adds a `priority_rank` column to `lc_hosted_jobs` (default
`2`, automatic review), a `lc_hosted_job_retired_usage` table and a
`lc_hosted_job_dispatch_tier_cursor` table. The first replica that starts on
this release applies it:

1. Inside the migration transaction, it adds the column with a constant
   default and its range check `NOT VALID`. Both are catalog changes: the job
   table is not rewritten or scanned.
2. After the transaction commits, it validates the check.

The priority and retention indexes are not built at startup. The first
maintenance pass after the upgrade builds them with `CREATE INDEX
CONCURRENTLY`, so replicas start at once, and claims and enqueues continue
during the build. One replica builds at a time. If a build is interrupted it
leaves an invalid index; the next maintenance pass drops and rebuilds it.
The queue works correctly without these indexes, only more slowly, and a
failed build is logged without stopping the rest of maintenance.

A schema-10 server keeps running against a schema-11 database. Its new jobs
get the default priority, it never deletes jobs, and it keeps its own
round-robin cursor in `lc_hosted_job_dispatch_cursor`. Retention runs only on
schema-11 servers, so while a schema-10 replica is still serving, its
per-Review usage totals leave out jobs that retention already deleted. Totals
for 30-day and current-month windows are not affected, because retention
never deletes a job that young.

A schema-10 server refuses a quota policy file that has `dispatch.ceilings`.
Upgrade every replica before you add ceilings, and remove them before you roll
any replica back.

The quota authority moves to schema 2 (BI1, the shared background budget). The
upgrade only adds two tables, `lc_quota.background_budget_periods` and
`lc_quota.background_budget_reservations`, with the same forced row-level
security as every other quota table. A schema-1 server keeps serving against a
schema-2 database and ignores both the new tables and the policy file's
`background` section. The first server to start at schema 2 migrates the
database under the existing quota migration lock.

While old and new replicas run side by side, run organization purges
(`forget_org`) only on a schema-2 server. A schema-1 binary does not know the
background budget tables, so its purge leaves that organization's rows in
`lc_quota.background_budget_periods` and
`lc_quota.background_budget_reservations`. If a schema-1 replica did purge an
organization during the rollout, run the purge again from a schema-2 server.

The background envelope gives each priority a percentage share, and the share
is rounded down to whole model calls. A very small envelope can therefore leave
a lower priority with no calls at all. For example, a daily guard of 1 lets only
`review_auto` start, while from 2 calls up every background priority may start.
Set the monthly and daily ceilings with this in mind.

## Health and observability

For a shared service, verify the service's documented health/config endpoints and the current CLI status surfaces rather than using old Workspaces plugin port checks.

Useful commands include:

```bash
lc status
lc rt service --help
lc rt worker list
lc runs --help
lc usage --help
```

Workspaces plugin health is checked with `lc rt status`, not by assuming a local Workspaces server exists.

## Logging and evidence

Production logging should:

- carry request/run/trace identifiers where available;
- redact credentials, provider tokens, source text, and other sensitive values;
- preserve enough operational context to diagnose failures without storing hidden model reasoning;
- separate product telemetry from customer/provider traffic.

## Security

- enforce authentication for non-loopback/shared HTTP deployments;
- validate provider and workspace boundaries;
- keep local-only trust assumptions local;
- do not expose a loopback Review/service port as a public remote service;
- verify secret-redaction and shell/tool supervision tests for the release;
- keep Enterprise identity/RBAC/policy controls in the Enterprise plane rather than recreating them in local Workspaces.

## Workers and queued jobs

Workers are explicit service infrastructure:

```bash
lc rt worker list
lc rt worker run-once
```

Run a persistent worker only when the deployment actually uses queued jobs.

### Hosted dispatch: shares, ceilings and priority

On the hosted managed pool, the quota policy file's `dispatch` section sets
each organization's and repository's fair share of running AI review work
(`defaults`, plus per-organization `overrides`). A share is guaranteed while
other organizations have work waiting. When no work within its share is
waiting anywhere, an idle slot goes to work beyond its share. Running work is
never preempted. To put a hard limit on that, add `ceilings` (with its own
`defaults` and `overrides`); work never runs past a ceiling, even when slots
are idle:

```json
"dispatch": {
  "defaults": {"model_work": {"org": 4, "repo": 2}},
  "ceilings": {"overrides": {"org_trial": {"model_work": {"org": 1}}}}
}
```

No ceilings are configured by default, so an organization can use idle capacity
up to the size of the pool. Every replica must run this release before you add
`ceilings`: an earlier release refuses a policy file that has them.

Jobs fall into two tiers, and all foreground work is dispatched before any
background work:

- Foreground: required reviews (pre-merge checks, manual requests and fresh
  re-runs), then automatic reviews.
- Background: `knowledge_refresh`, then `discussion_summary`, then
  `learning_suggestion`.

Within a tier, organizations take turns. The order above applies only within
one organization's turn, so one organization's required reviews never hold
back another organization's automatic reviews. Background work never takes the
last free slot, and it does not age: while foreground work keeps the pool busy,
background work waits.

One dispatch examines at most 64 organizations per tier for each of its two
passes (shares, then borrowing), which is at most 256 organizations. If more
than 64 organizations in a row are all at their share, the dispatch lends no
slot and the next one continues the rotation.

Each organization sees its own shares, ceilings, running work and queue wait
under **Organization usage and limits**. A queued AI review shows one `Queued`
chip that says why it is waiting. `python -m lemoncrow_server.ops queue-delay`
gives operators the same view across all organizations.

### Hosted job retention

Maintenance deletes finished hosted jobs, including their stored results, once
they are older than `LEMONCROW_SERVER_HOSTED_JOB_RETENTION_DAYS` (default 90).
The server refuses to start with a value under 31: the 30-day usage window plus
one day.

Retention never deletes:

- work that is queued, running or cancel-requested;
- work that still holds a lease;
- a review, runtime-validation or production-observation success whose
  publication is still pending (index and maintenance jobs publish nothing,
  so they are deleted once old enough);
- a job whose deadline is inside the horizon. This keeps an open production
  observation window from being offered again.

In the same statement that deletes a job, its compute time, model calls and
provider cost are folded into `lc_hosted_job_retired_usage`, so usage totals
never drop. Folded usage is kept for 400 days.

After a job is deleted, replaying its idempotency key queues and runs a new
job. For an AI pre-merge check, its retry count also starts again. Retention
only deletes jobs that ended at least 31 days ago, so only a replay that late is
affected.

If retention fails, maintenance logs the error, skips that step and carries on
with the rest of the pass.

### Audit history retention

The same leased pass deletes organization audit-history rows older than
`LEMONCROW_SERVER_AUDIT_RETENTION_DAYS` (default 365, at least 1), at most
50,000 per pass, and logs how many it removed. A failure is logged and the
rest of the pass runs. `ops purge --org` deletes an organization's history.
Nothing else deletes a row, and no request route updates or deletes one.
The audit log sinks are not touched; rotate them in your log pipeline.

## Fresh-store validation

For changes that affect Workspaces storage/index initialization, use the current Workspaces init command in an isolated store/worktree:

```bash
lc rt init
```

Then run the targeted benchmarks/evals relevant to the changed capability. Do not treat a fresh init as proof for unrelated Workspaces/Review behavior.

## Enterprise deployments

Enterprise can run in customer-controlled or dedicated infrastructure and adds identity, policy, managed workspaces, audit, usage attribution, and broader agent-workload controls.

Production deployment details depend on the chosen Enterprise topology; the public local Workspaces architecture is not a substitute for those controls.
