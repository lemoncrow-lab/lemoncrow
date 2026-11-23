# Workspaces Lite quickstart

From install to an agent's change landed on `main`, on your own machine, in about ten minutes. Workspaces Lite is free and open source (Apache-2.0). It needs no LemonCrow account, and it uploads nothing to LemonCrow.

You need:

- macOS or Linux, with Git.
- A Git repository whose remote `origin` has a `main` branch you can push to. Each task starts from `origin/main` and lands there.
- A coding agent: Claude Code, Codex or OpenCode.

## 1. Install

```bash
curl -fsSL https://lemoncrow.com/install.sh \
  | bash -s -- --product workspaces --agent claude
```

This installs Workspaces Lite, with `lc verify` and `lc task`, and gives Claude Code its tools. Use `--agent codex` or `--agent opencode` for those agents. Open a new terminal after the install, or run the `source` line the installer prints.

Already have LemonCrow Review installed? Run `lc rt enable claude` instead. It fetches Workspaces once.

## 2. Say how a change is checked

In your repository:

```bash
lc verify init
```

It finds the checks the repository already runs: Makefile targets, `package.json` scripts, GitHub Actions run steps, and pytest, ruff or mypy in `pyproject.toml`. It writes them to `.lemoncrow/environment.toml`:

```text
lc verify: wrote .lemoncrow/environment.toml with 1 check(s):
  test  make test
lc verify: no [toolchain]; a Workspaces server needs one: lc verify init --toolchain IMAGE
lc verify: review the checks, commit the file, then run `lc verify hook install`
```

Open the file and keep the checks you want. If it finds none, it writes nothing and says so; then write the file by hand, as [Environment definition](../reference/environment.md) shows. Run every check once:

```bash
lc verify --all
```

Then commit the file, push it and install the pre-push hook:

```bash
git add .lemoncrow/environment.toml
git commit -m "Add .lemoncrow/environment.toml"
git push origin main
lc verify hook install
```

From now on, a push runs the checks its commits touch, and stops when one fails.

## 3. Start a task

```bash
lc task start fix-123
```

```text
lc task: work in /home/you/your-repo/.worktrees/fix-123
lc task: commit, then run 'lc task land' inside the worktree (it runs lc verify)
```

The task is a new worktree on the branch `agent/fix-123`, from `origin/main`. Your own checkout is left alone. Start your agent in the worktree, ask it for the change, and have it commit:

```bash
cd .worktrees/fix-123
claude
```

## 4. Land it

Inside the worktree, once the work is committed:

```bash
lc task land
```

It rebases on `origin/main`, runs the checks the change touches, and pushes to `main`:

```text
lc task: the pre-push hook runs the checks (lc verify --pre-push)
lc verify: [test] $ make test
lc verify: 1 check(s) passed
lc task: landed 0cde2c6 on main. Next, from outside this worktree:
  lc task finish fix-123
```

When a check fails, nothing is pushed. Fix the work, commit, and run `lc task land` again.

## 5. Finish

```bash
cd ../..
lc task finish fix-123
```

This removes the task's worktree and branch. It removes nothing while the worktree holds uncommitted or untracked files, such as a `__pycache__/` folder your checks left: commit them, or add them to `.gitignore`. `lc task list` shows every task branch.

Every task follows the same steps: `lc task start`, work and commit, `lc task land`, `lc task finish`.

## Next: tasks on a Workspaces server

A Workspaces server runs each task in a sandbox of its own, can run the agent for you, and lands work only after LemonCrow Review passes it. It is offered within Enterprise, on your own infrastructure. The file and the verbs stay the same. A server needs one more thing in the file: the image your checks run in.

```bash
lc verify init --toolchain python:3.12-slim
git commit -am "Pin the toolchain for a Workspaces server"
git push origin main
lc task start fix-124 --server https://acme.workspaces.lemoncrow.com
```

`--toolchain` asks the image's registry for its digest and pins it; it reads public images only. Name the image your checks need. For Enterprise, email [contact@lemoncrow.com](mailto:contact@lemoncrow.com?subject=Enterprise) with the subject `Enterprise`.
