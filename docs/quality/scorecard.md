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
| Rubric coverage | B | A | Packaged rubrics seeded on init; `rubric_code_review` and `rubric_verification_ladder` applied at review time |
| Learnings capture | C | A | Use `trace(learnings=[...])` to persist decisions and lessons across sessions; promote durable ones to `docs/decisions/` ADRs |

## Next upgrades

- Move durable architectural decisions into `docs/decisions/`.
- Keep `docs/plans/tech-debt.md` current as cleanup work lands.
- Extend evidence capture when UI automation becomes first-class in this repo.
- Wire `rubric_verification_ladder` into the post-implementation verify step for all coding work.
- Run `make check-agent-context` in CI to catch generator drift on host instruction files.
