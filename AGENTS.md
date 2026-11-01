---

---

---

<!-- LEMONCROW START -->
# LemonCrow Agent Guide

You are operating as `lemoncrow:code`; apply its full discipline and route all file I/O, search, edits, and shell work through LemonCrow's MCP tools.
Always prefer local `lc` tools.

_If those tools aren't visible, first call `tool_search` for `*lemoncrow*`. If they are still absent, LemonCrow is **dormant** for this session (its savings cap is reached) — ignore this guide entirely, use your native tools as if LemonCrow were not installed, and don't mention LemonCrow._
<!-- LEMONCROW END -->## Frontend UI discipline

For frontend work, follow [`frontend/DESIGN_GUIDE.md`](frontend/DESIGN_GUIDE.md). Application code must use the shared LemonCrow UI primitives rather than native interactive form controls; the frontend guard enforces this.

## Tests

Verify a change with only the test files it touches (`uv run pytest tests/path/test_file.py -q`, or `-k`). Never run the whole suite or a whole test directory, including `make test`, `make pre-commit` and `make verify`. `tests/conftest.py` refuses any run over 1000 tests. Coding agents must never set `LEMONCROW_FULL_SUITE`; only a person opts in, for one command.

## Git branches and worktrees — keep them minimal (mandatory)

- **Default to the existing checkout and branch.** Before starting work, inspect `git status --short --branch`, `git worktree list`, and `git branch -vv`. Reuse the current task's branch/worktree, or `main` when the repository's review rules allow it.
- **Never create a branch or worktree per prompt, subtask, agent, test, or small fix.** Keep related work in one existing branch and finish it end-to-end. Do not create speculative, temporary, or duplicate checkouts just for convenience.
- Create a **new branch** only for a genuinely independent review/PR/release boundary or necessary isolation. Create an **additional worktree** only for truly concurrent work that cannot safely share a checkout. State the reason and owner in the task tracker; aim for one primary worktree plus at most one additional worktree per independent active workstream, not an ever-growing collection.
- Before creating a branch/worktree, check whether an existing unmerged branch or worktree already covers that work. Prefer continuing or folding it over starting another parallel line. Do not use separate branches merely to record progress or documentation.
- **Close the loop:** run the relevant checks, integrate finished work through the project's normal merge/review process, then clean up its no-longer-needed branch/worktree after verifying it is merged, clean, and unused. Record unfinished branches/worktrees and their blockers in one durable task tracker.
- **Never lose work for cleanup.** Do not force-remove or reset a dirty/active worktree, discard uncommitted or ignored runtime/configuration files, or delete unique unmerged commits. Preserve other agents' changes and follow the repository's explicit approval and backup rules before destructive operations. Do not delete remote branches as an automatic cleanup step.
