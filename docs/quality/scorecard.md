# Quality Scorecard

This scorecard tracks the repo surfaces that most affect autonomous execution.

| Surface | Current | Target | Gap |
| --- | --- | --- | --- |
| Agent OS docs | B | A | Keep live docs aligned with generated host files |
| Host instruction sync | B | A | Enforce generated outputs in CI and contributor workflow |
| Architecture map | B | A | Promote layer and source-of-truth docs into the live tree |
| Plans and decisions | B | A | Keep active plans, completed plans, and ADRs committed |
| Validation loops | B | A | Expand worktree and runtime evidence usage |
| Scheduled cleanup | C | A | Run recurring docs and drift checks |

## Next upgrades

- Move durable architectural decisions into `docs/decisions/`.
- Keep `docs/plans/tech-debt.md` current as cleanup work lands.
- Extend evidence capture when UI automation becomes first-class in this repo.
