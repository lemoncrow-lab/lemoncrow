# Legacy author-agent WIP retirement: Oct 31, 2026

**Disposition:** preserve the old `wip/review-author-agent` branch as Git ancestry for source navigation; **do not restore its API or feature changes into production**. This is archival integration, not a statement that the obsolete code passed release qualification.

The old WIP expects automatic `capability_filter` assignment and a hosted route/device selection scheme that no longer matches current exact-job permissions. It has 16+ overlapping conflicts in the HTTP server, worker and Review reader. Restoring it directly could circumvent or confuse the newer mandatory source-disclosure approval.

| Legacy responsibility | Supported implementation on `main` | Acceptance evidence |
|---|---|---|
| Manual hosted task initiation | `enterprise/server/src/lemoncrow_server/review_agent_tasks.py` and `app.py` with `external_source_approved: true` | 23 focused hosted-agent tests passed Oct 31 |
| Explicit browser approval | `enterprise/frontend/src/review/HostedAgentRequestSheet.tsx` (PR #68) | 254 enterprise-frontend tests, TypeScript and production build passed Oct 31 |
| Operator-approved local execution | `client/src/lemoncrow_client/review_agent_task_cli.py`; `lcr task run JOB_ID --allow-external-source`, exact job ID | Existing typed per-job route, usage and budget controls; no worker daemon |
| One-model-call brokerage | `review_model_broker.py`, `review_model_attempts.py`, `review_model_submission.py` | Local provider key prompt; once-only attempt ledger; no automatic retry after unknown billing outcome |
| Observation publication | `enterprise/server/src/lemoncrow_server/review_author_agent_result.py` | Unverified observations, never synthetic PASS/fix acceptance |

**Not implied:** live provider qualification, customer-machine connectivity, model accuracy, current release readiness, or a production rollout. Those retain their own separate gates. Do not delete the historical remote WIP ref until archival ancestry is on remote main and references are verified.
