# Domain Map

| Area | Source of truth | Notes |
| --- | --- | --- |
| Product positioning and roadmap | `docs/product/` | Strategic docs and execution priorities |
| Feature execution specs | `docs/specs/` | Task-ready briefs for implementation work |
| Agent operating rules | `docs/agent-os/` | Shared rules, workflow, validation, host overrides |
| Runtime architecture | `src/atelier/core/`, `src/atelier/infra/`, `src/atelier/gateway/` | Python runtime layers |
| Host install surfaces | `integrations/`, `scripts/install_*.sh`, `docs/hosts/` | Must stay aligned |
| Frontend behavior | `frontend/`, `frontend/src/api.ts` | Verify data shapes explicitly |
| Release and hardening | `docs/production-readiness.md`, `scripts/verify_*.sh` | Operational and deploy checks |
| Plans, decisions, debt | `docs/plans/`, `docs/decisions/`, `docs/quality/scorecard.md` | Durable execution memory |
