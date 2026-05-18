# Reliability

Reliable agent execution in Atelier depends on reusable evidence loops.

## Required habits

- Prefer commands and scripts that another agent can rerun without extra context.
- Capture service health and runtime summaries with repository scripts.
- When booting the stack from a worktree, use the worktree-aware environment helper.

## Key entrypoints

- `scripts/worktree_env.py`
- `scripts/runtime_evidence.py`
- `docs/production-readiness.md`
- `scripts/verify_atelier_service.sh`
- `scripts/verify_atelier_postgres.sh`
