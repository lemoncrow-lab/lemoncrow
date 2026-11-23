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

Verify a change with only the test files it touches (`uv run pytest tests/path/test_file.py -q`, or `-k`). Never run the whole suite or a whole test directory, including `make test`, `make pre-commit`, `make verify-all` and `make verify-full`. `tests/conftest.py` refuses any run over 1000 tests. Coding agents must never set `LEMONCROW_FULL_SUITE`; only a person opts in, for one command.

## Agent workflow (mandatory)

- **One task, one worktree.** Start each task with `lc task start <task-id>`. It creates `.worktrees/<task-id>` on branch `agent/<task-id>` from `origin/main`. Work only there: never edit the main checkout, and never share a worktree with another agent. `.githooks/agent-task ARGS` runs the same `lc task ARGS` with this checkout's client; use it when the installed `lc` has no `task` command.
- **Gate, then land.** Commit, run `make verify`, then `lc task land` inside the worktree. `make verify` runs the checks in `.lemoncrow/environment.toml` that your change touches (`lc verify`). `land` rebases onto `origin/main` and pushes to `main`; `git push` runs the same gate on exactly what lands. If a check fails, fix it and land again.
- **Never bypass the gate** with `--no-verify`; only a person may. Report a failure you cannot fix.
- **Clean up.** After landing, run `lc task finish <task-id>` from outside the worktree. It removes the worktree and branch only when every commit is on `origin/main` and no file is modified or untracked; ignored files (dependencies, build output) go with it. `lc task list` shows tasks not yet landed.
- **Never lose work.** Do not force-remove a worktree, discard uncommitted or untracked files, delete unlanded commits, or delete remote branches. Leave other agents' worktrees alone. If you stop before landing, report the worktree and branch that hold the work.
- The desktop runner re-checks `main` after each push. `make verify` also runs `git config core.hooksPath .githooks`, which turns the gate on for the clone.
- `make verify` runs only the test files you changed. Never run `make verify-all` or `make verify-full`: both run the whole suite.

## Testing Policy

- Write only high-value tests that catch meaningful bugs, regressions, or security issues.
- Prefer real behavior and integration tests over mocked or implementation-detail tests.
- Avoid trivial, redundant, or coverage-driven tests.
- Run focused tests during development; preserve existing mandatory CI and release checks.
