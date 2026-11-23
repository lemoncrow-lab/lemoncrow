# LemonCrow CLI reference

`lc` is the command-line entry point for LemonCrow Workspaces and Review.

There are two `lc` command trees:

- **The installed client.** `curl -fsSL https://lemoncrow.com/install.sh | bash` installs it. It carries Review capture (`lc review`, `lcr`), the AI review in the terminal (`lc review --ai`, `lc review hook`, `lc review init-action`, `lc review config`), Workspaces Lite (`lc with`, `lc rt ...`), and product management (`lc status`, `lc update`, `lc uninstall`, `lc auth`).
- **The full CLI.** A LemonCrow source checkout runs it as `uv run lc ...` (see [Installation](../setup/installation.md#source-checkout--contributor-setup)). It adds the commands marked *full CLI* below. The installed client recognizes those commands and says where they live instead of running them.

For the exact flags supported by your installed version, use built-in help:

```bash
lc --help
lc runtime --help
lc review --help
```

## Main workflow

The root command groups the product around the jobs developers actually do:

| Command | Purpose |
| --- | --- |
| `lc code` | Reserved for LemonCrow's hosted coding agent, which is not yet available. The installed client says so and exits |
| `lc review` | Open or update a durable Review for the current change |
| `lc with <host>` | Try Workspaces Lite with Claude Code, Codex, OpenCode, or Pi without changing saved host configuration |
| `lc rt host ...` | Manage public Pi host binaries |
| `lc runtime index` | Build or refresh the local Workspaces Lite index |
| `lc runtime` / `lc rt` | Manage Workspaces integrations and advanced Workspaces commands |
| `lc verify` | Run the checks in `.lemoncrow/environment.toml` that your change touches; `lc verify init` writes the file from the checks the repository already runs, and `lc verify hook install` makes it the pre-push hook. See [Environment definition](environment.md) |
| `lc task start\|land\|finish\|list` | One worktree and `agent/ID` branch per agent task, in one repository or several; `land` rebases, runs `lc verify` and pushes to `main`. With `--server URL`, the same verbs on a Workspaces server. See [Agent tasks](#agent-tasks-lc-task) |
| `lc status` | Show installed products, deployment, sign-in, and Workspaces integrations |
| `lc auth` | Sign in/out of a configured hosted LemonCrow deployment; `lc auth login --server URL` also signs in to a Workspaces server, with a token for that server only |
| `lc audit` | Print what this client does and does not do (endpoint, credentials source, and whether anonymous feature counts are on) |
| `lc settings` | `show`, `get`, `set` and `reset` a setting, also from the installed client (save an API key with `lc settings set llm.anthropic_api_key`); the full CLI also opens an interactive picker |
| `lc update` | Re-run the installer for each installed product; host registrations are untouched. With `cosign` installed, the installer runs only once its signature verifies; without it, one line on stderr says the signature was not checked and how to get it checked ([install cosign](https://docs.sigstore.dev/cosign/system_config/installation/)), and the update continues |
| `lc uninstall review\|workspaces\|all [--dry-run] [--yes] [--purge]` (`runtime` is the old name of `workspaces`) | List exactly what the installer added, then remove it; asks before anything of yours (settings, saved reviews, a git hook it wrote, a repository's `.lemoncrow` folder). See [Removal](#removal-lc-uninstall) |

These run only from a source checkout (`uv run lc ...`, the full CLI). The installed client names them and says where they live:

| Command | Purpose |
| --- | --- |
| `lc rt map` | Browse this repository's code in the terminal: folders, symbols, imports, source |
| `lc rt dashboard` | Spend and savings over the last 7 days, and the latest runs |
| `lc context` | Inspect repository/agent context |
| `lc session` | Pick a recent coding-agent session and read its cost and savings report |
| `lc usage` | Summarize coding-agent usage across supported hosts |
| `lc run` | Explain recorded work or drive an owned run |
| `lc doctor` | Run installation and integration diagnostics. The installed client uses `lc status` and `lc rt status` |

An unknown command prints one line naming it and, where one exists, the command to use instead (for example `lc host` points to `lc rt host`).

## Use LemonCrow with an existing agent

Claude Code, Codex, and OpenCode support a temporary plugin launch:

```bash
lc with claude
lc with codex
lc with opencode
```

This keeps the host's own model, provider account, credentials, and saved settings.

Persist the local integration with:

```bash
lc rt enable claude
lc rt enable codex
lc rt enable opencode
```

Inspect integrations:

```bash
lc rt status
lc status
```

Cursor is also supported, but neither the installer nor `lc rt enable` configures it: its IDE/CLI integration is installed from a source checkout. See [Cursor integration](../hosts/cursor-install.md).

## Hosted coding agent: `lc code` (not yet available)

`lc code` is reserved for LemonCrow's hosted coding agent, which is **not yet available**. It is not the local Workspaces Lite launcher and it has no `--pro` compatibility mode. Today the installed client says so and exits.

The open-source CLI may carry the auditable client surface, but the hosted execution loop, provider/model routing, spend controls, cache orchestration, and advanced private intelligence belong to Hosted/Enterprise Workspaces. Current builds fail closed because the remote transport is not available.

For local Workspaces Lite, use `lc with claude|codex|opencode|pi`, persistent `lc rt enable <host>` integration, or Cursor's integration from a source checkout. Build the local index explicitly with `lc runtime index`.

Pi is the public managed coding-agent host:

```bash
lc rt host install --engine pi
```

Workspaces Pro grants hosted retrieval. It is not an engine flag. The old `lc code --pro` mode is removed.

## Reviewing agent-written changes

The command is `lc review`.

The common case is:

```bash
lc review --open
```

With no explicit range, Review uses the current working-tree change; on a clean tree it falls back to the latest commit and says so. `--open` is the default; `--no-open` captures and prints the Review URL without opening a browser. A plain `lc review` never calls a model: it captures the change and opens the Reader. Add `--ai` to get an AI review in the terminal (below).

The installed client accepts these options (`lc review --help` lists them):

```bash
lc review --staged               # exactly what is staged
lc review --working-tree         # current working-tree changes
lc review main...HEAD            # the branch change from its merge base with main
lc review --base REV --head REV  # an explicit range
lc review --title "Auth TTL"     # set the Review title
lc review --no-impact            # skip impact analysis
lc review --reopen-review        # restore an archived matching Review
lc review --json                 # machine-readable output
lc review --ai                   # AI review printed in the terminal (see below)
lc review hook install           # git hook: AI review before each push
lc review init-action            # write the GitHub Action workflow
lc review config --effective     # the review settings and rules in force, each with the file it came from
lc review config import FOLDER   # bring another review tool's config over: map its settings, write .lemoncrow/review.yaml, print what was carried and dropped
```

Opening the Reader from the printed URL of `--no-open` shows "This browser isn't connected to your local review" until the terminal has connected that browser; run `lc review --open` (or `lc review` again) and retry.

Marking, commenting, changes since your review, and feedback for the author happen in the Review Reader (**Mark reviewed**, **Add comment**, **Changed since your review**, **Copy feedback**, **Send to agent**), or from the terminal with the installed client and the full CLI alike. Each of these captures the current change first, then acts on the same Review through the Reader API:

```bash
lc review --mark src/auth.py                 # a label, a part key (see --units) or a path; --mark-state STATE
lc review --comment "ttl is hardcoded" --on src/auth.py:L81   # --comment-kind KIND, --reply-to ID
lc review --since-my-review                  # what changed since your last review
lc review --feedback                         # open comments as Markdown; sends nothing
lc review --marks                            # also --units, --comments
lc review --finish                           # or --reopen-review
lc review status                             # the current Review, without capturing; --json
```

`--mark`, `--comment`, `--finish` and `--reopen-review` record human judgment and are refused inside a coding-agent session (the client checks the agent's environment, and the server checks the calling process). Capture, `status`, `--feedback`, `--since-my-review` and the listings stay available to agents.

**`lc review status`.** It reads the Review the last capture in this checkout updated (matched by the server's recorded worktree root, open Reviews and this branch first; `--review r/ID` names another; `--repo-root DIR` another checkout). The text says `Revision N` counted from 1, plus one `Risk` line and one `Understanding` line in the Brief's words. `--json` prints one document (`schema: 1`):

| Field | Meaning |
| --- | --- |
| `state`, `exit_code` | `ready` (0), `not_ready` (1), `no_review` (2) or `error` (3), with `message` for the last two |
| `review` | `{id, ref, title, status, range_mode, branch}` |
| `revision` | `{id, ref, number, latest_number, label}`: `number` is the server's zero-based number, `label` the text `Revision N` |
| `files`, `targets` | changed files; `{total, reviewed, needs_changes, changed_since_review, unreviewed, unknown, later}` parts; `later` counts parts set aside with `--mark-state later`, which are not judged |
| `comments` | `{open, orphaned, resolved, obsolete, unresolved}`; `unresolved` is open plus orphaned |
| `feedback` | `{unsent, in_flight, sent, addressed}` |
| `work_remains` | any target unreviewed, needing changes or changed since review, any unresolved comment or unsent feedback |
| `risk` | `{level, label, reason, text, facts, not_measured}`: what this change could break, from measured facts only. `level` is `high`, `medium`, `low` or `not_measured`; `text` is the Brief's line: the level and the top fact, then `· and N more` for the other facts (`High · can_view_order is access-control code · and 3 more`); each fact has `{text, kind, level, target_ids}`; `not_measured` names each input that could not be read. Absent when the server could not build the Brief |
| `understanding` | `{reviewed, total, risky, text}`: parts judged on this revision (Reviewed or Needs changes) among the risky ones, the parts the queue puts in Review first (a mark does not take a part out of this set; every part when there are none), e.g. `0 of 2 risky parts reviewed` |
| `readiness` | `{available, ready, blocker_count, actions, blocking_reasons}`: the server's readiness verdict and what blocks it (`actions` such as `Finish review`). When the server cannot evaluate readiness, `available` is false and `work_remains` decides |
| `reader_url` | the Review Reader URL |

| Exit code | Meaning |
| --- | --- |
| `0` | Ready: the server's readiness verdict says so |
| `1` | Not ready yet: work remains (`readiness.actions` says what) |
| `2` | No Review found for this repository (run `lc review`), or `--review` names none |
| `3` | Error: the server could not be reached or refused, or the command line is wrong |

In the full CLI, `--no-open` alone is a terminal-only look that records nothing: add `--track` (or any action above) to update the Review without a browser. The installed client always captures and accepts `--track`, so `lc review --no-open --track` captures in both.

The full CLI (`uv run lc review` in a source checkout) adds more; the installed client says it does not carry these:

```bash
lc review --current
lc review --resume-author
lc review --pr 123
lc review data-flow
lc review diagnostics
lc review observation-worker check --server URL --pins pins.json
```

`lc review --help` is one screen; `lc review --help-all` lists every option and subcommand of the full CLI.

### Where the AI review runs

Everything in this section runs from the installed `lc` and from a source checkout alike. The installed client starts the Review engine's interpreter on it (the installer puts the engine at `~/.lemoncrow/review/engine`), so there is one `lc` and one help. A client with no local engine (a Hosted install) says how to get one. These four commands are the only ones that can call a model, and only `--ai` makes `lc review` do so: a key in your environment, or `--agent`, `--pr` and `--fail-on` on their own, never start a review that costs money.

### Local AI review and git hooks

`lc review --ai` reviews a change that has no pull request, on your own model key or a signed-in agent CLI, and posts nothing anywhere. It runs the engine path the [GitHub Action](../setup/github-action.md) runs (capture, the AI stage, the same severity rule), so a finding has the same severity here as on a pull request.

```bash
export ANTHROPIC_API_KEY=sk-ant-...   # or OPENAI_API_KEY; or skip the key and use --agent below
lc review --ai                         # the working tree against HEAD (the last commit when the tree is clean)
lc review --ai --agent claude          # review with your signed-in Claude Code instead (also codex, opencode)
lc review --pr 123 --ai                # a GitHub pull request (needs the gh CLI); prints what the bot would post, posts nothing
lc review --ai --staged                # exactly what is staged
lc review --ai main...HEAD             # a branch against its merge base with main (or REV, or --base/--head)
lc review --ai --fail-on p1 --strict   # stricter exit codes, for a hook or a script
lc review --ai --json                  # the result as JSON
lc review --ai --depth thorough        # go deeper (auto, thorough, deep); the hard model-call cap applies at every depth
lc review --ai --fresh                 # ignore the result carried from the last review of this change
```

**Exit codes.** A hook reads these; they are the same for `lc review --ai` and for the hooks below.

| Code | Meaning |
| --- | --- |
| `0` | Clean: no open finding at or above `--fail-on`. An incomplete review (a provider error, a spent budget) is also `0` unless `--strict`, so it never blocks a push by itself. |
| `1` | A finding at or above `--fail-on` is open. `--fail-on p0` (the default) stops on a P0, `p1` on a P0 or P1, `none` never. A found P0 outranks an incomplete review. |
| `2` | The review is incomplete and `--strict` is set: no AI model set up, the monthly AI budget is `0` or used up, the model-call or dollar budget ran out, a model call failed, or part of the change was left unexamined (the hard cap of 20 model calls was too small for it: the headline reads `Partially reviewed: N of M changed areas examined`; the next run takes what is left). |
| `3` | The review could not be set up, so it did not run: a flag that cannot be combined with `--ai`, a value that is not one of the choices, a revision that does not resolve, no Git repository, **no AI model set up**, or a provider that rejected the key, has no credit or is rate limiting. A git hook never exits `3` for the last three: it lets the push through and says why. |

Nothing to review (no changed files) is `0` and needs no model. An error inside the review that is not a finding is reported as an incomplete review, never as exit `1`.

**What is shown.** The verdict line, then each open P0 and P1 with its place, why, the consequence and a checked replacement when there is one, the P2 findings as one line each, the findings your repository's threshold holds back in a section of their own (below), what was reviewed in this run and how much was reused, the model, depth and usage. Colour is off with `--no-color`, with `NO_COLOR`, and when output is not a terminal; text written by the model or a file name is stripped of control characters before it is printed.

**`--json`.** One document on standard output (progress goes to standard error), `schema: 1`:

| Field | Meaning |
| --- | --- |
| `kind` | `local_review` |
| `scope` | `{mode, label, base, head}`: `working_tree`, `staged` or `commit_range`, the commits or tree the review covered |
| `empty`, `cached` | nothing to review; no model call was needed because every changed part was reviewed, unchanged, last time |
| `verdict` | `{state, headline, p0, p1, p2}`, plus `held_back` (a count) when the repository's threshold holds findings back; `state` is `clean`, `must_fix`, `partial`, `incomplete` or `budget` |
| `complete`, `incomplete_reason` | whether the review finished and examined every changed area it set out to look at, and why not |
| `exit` | `{code, reason, fail_on, strict}`: the exit status and why |
| `findings` | every tracked finding as the pull request bot's hidden state records it (`id`, `severity`, `category`, `state`, `path`, `line`, `end_line`, `title`, `explanation`, `consequence`, `suggestion`, ...); `state` is `new`, `carried`, `fixed`, `stale`, `unreported` or `suppressed`; the held-back ones are among them |
| `held_back` | only when the threshold holds findings back: those findings, in the same shape. They are in no `verdict` count and never decide the exit code |
| `coverage` | `{files, targets, rereviewed, unchanged, candidates, investigated}` |
| `depth`, `model`, `usage`, `usage_total` | the run's depth, the model that served it, and its usage (`calls`, `input_tokens`, `output_tokens`, `cost_usd`) and the total for this change |
| `notes` | things worth knowing, such as the clean-tree fallback |

**Noise controls.** A review of your own change reads `.lemoncrow/review.yaml` (and the rules files it imports, `AGENTS.md` and `CLAUDE.md`) from your **working tree**, a file you have written and not committed yet included, and says so in one line under the title (`Config: .lemoncrow/review.yaml from your working tree (not committed yet; ...)`, or `no .lemoncrow/review.yaml in this working tree; built-in defaults.`). A pull request review, `lc review --pr N --ai` and the Action, still reads the committed copy at the merge base, so a pull request cannot loosen its own review. The `review:` block works the same either way: `strictness` / `finding_threshold` hold back the open findings below the threshold, `rules.<key>.severity` sets the severity of the findings that cite a rule, and `ignore` keeps files out of the review. A held-back finding is shown once, in a `held back below strictness 3` section, counted in the headline (`1 held back`) and listed under `held_back` in `--json`; it is in no verdict count and never makes `--fail-on` exit `1`. A P0 is never held back. Lowering the threshold shows the finding again on the next run with no new model call. See [Reducing noise](../setup/github-action.md#reducing-noise).

**Model route.** Your own, in this order: `--agent claude|codex|opencode` (a local agent CLI you are signed in to; it uses that tool's own plan and is only ever used when named), an explicit `LEMONCROW_LLM_BACKEND` (LiteLLM, OpenAI, Ollama or an agent CLI) with its variables, `ANTHROPIC_API_KEY`, then `OPENAI_API_KEY`. With only a key, the installed engine calls the provider over HTTPS itself and needs no SDK; set `ANTHROPIC_BASE_URL` or `LEMONCROW_OPENAI_BASE_URL` to use a gateway or a local server. A key saved with `lc settings set llm.anthropic_api_key` (or `llm.openai_api_key`) counts as the exported one; an exported key wins. With none of them the review does not run: it prints three lines (the `export` line, the `--agent` alternative, one docs link) on standard error, exits `3` (`2` under `--strict`, `0` in a hook), and nothing is sent anywhere. With no key, an agent you saved with `lc review --ai --agent NAME --remember` (the setting `review.agent`) is the route; one that is merely on your `PATH` is only suggested by name, never run. A key the provider rejects, an account with no credit and a rate limit each stop the review with one line saying which (exit `3`). The intelligence policy and the monthly AI budget (`intelligence.monthly_budget_usd`, no limit unless you set one) apply: with the budget at `0` or used up, nothing is sent and the review is incomplete.

**Carried results.** Like the bot, the review keeps its result between runs, in a small file under the LemonCrow root (`review-local-ai/`), one per change: the working tree and the staged changes of a branch, or a range's base and the branch it ends on. A re-run reviews only what changed since: on an unchanged tree no model call is made and the open findings still decide the exit code; after an edit only the changed files go to the model; a fixed finding is closed. A changed review policy or depth reviews everything once; `--fresh` does so on request. `--max-usd-per-pr` and `--max-runs-per-pr` limit what one change may cost over its runs, read from the same carried usage; when they are reached nothing new is reviewed and the findings stand.

**`lc review hook install`** writes a hook that runs this before a push, or before a commit with `--pre-commit`:

```bash
lc review hook install                        # .git/hooks/pre-push (or wherever core.hooksPath points)
lc review hook install --print                # show the script, write nothing
lc review hook install --fail-on p1 --strict  # the options the hook runs with
lc review hook install --pre-commit           # review what is staged before each commit
lc review hook install --force                # replace a hook that already exists
```

The script is a few lines of shell around `lc review hook run`. It calls the absolute path of the `lc` that wrote it (the first `lc` on your PATH only if that file is gone), so a different `lc` earlier on the PATH is never the hook's. For a push it reviews what the push would publish: the commits of each pushed branch that the remote does not have (from where a new branch leaves the default branch). Branch deletions and tags are skipped.

**A hook never stops a push because LemonCrow broke.** It stops it for two reasons only: an open finding at or above `--fail-on`, and, if it was installed with `--strict`, a review that could not finish. No key, a provider error, a spent budget, a crashing or missing `lc`, a broken installation: the push goes through, with one line saying why. (The script asks `hook run` to answer "stop" with the exit status 87, which a crash cannot produce, and treats every other status as "let it through".) Skip it once with `LEMONCROW_REVIEW_SKIP=1 git push` (`git push --no-verify` skips every hook). The installer never overwrites a hook that exists, whether another tool's or one of its own with other options, without `--force`; an older LemonCrow hook (v1, which let a crashing `lc` stop the push; v2, which said `lc was not found` on every push once `lc` was gone) is replaced, and installing the same hook twice writes nothing the second time. If `lc` is gone and the hook is still there (a repository `lc uninstall` never saw), the hook says so once for that repository, names the file to delete, and from then on stays silent and lets every push through. `lc uninstall` removes the hooks it wrote (see below). A model call happens on every push with new code, so keep the depth at `auto` for hooks.

**`lc review init-action`** writes `.github/workflows/lemoncrow-review.yml` from the setup guide's minimal workflow (`--provider anthropic|openai` picks the key input it passes, `--fail-on none|p0|p1`, `--chat` to add the comment triggers, `--force` to replace an existing file) and prints the steps left to you: adding the secret and pinning the Action to the release commit the release notes print. See [GitHub Action](../setup/github-action.md#install-the-github-action).

Review keeps durable human judgment across revisions where the underlying reviewed work is unchanged; changed or new work returns to the review queue.

See [`lc review`](review.md) for the product model and [Review walkthrough](review-walkthrough.md) for a hands-on flow.

## Agent tasks: `lc task`

`lc task` keeps the same verbs on this machine and on a Workspaces server. A verb goes to a server with `--server URL`; with `LEMONCROW_WORKSPACES_SERVER` set instead, it goes there only for a task this machine started there. The server is offered within Enterprise. Walkthrough: [Workspaces Lite quickstart](../setup/workspaces-lite-quickstart.md).

| Verb | On this machine | With `--server URL` |
| --- | --- | --- |
| `start ID` | A worktree `.worktrees/ID` on branch `agent/ID`, from `origin/main` | The task on the server, in its `[toolchain]` image; the task's token kept in an owner-only file |
| `land` | `land [ID]`: rebase on `origin/main`, run `lc verify`, push to `main` | `land ID -m MESSAGE`: commit, check, Review, push to `main` |
| `finish ID` | Remove a landed task's worktrees and branches | Save unlanded work, remove the rest, forget the token |
| `list` | Task branches | Tasks on the server |
| `verify`, `claude`, `codex`, `run`, `watch`, `tell`, `stop`, `attach`, `mcp`, `discard`, `preview` | Not run: `lc task VERB is for tasks on a Workspaces server: add --server URL or set LEMONCROW_WORKSPACES_SERVER` (exit 2) | Server only |

A server runs each task in the `[toolchain]` image of `.lemoncrow/environment.toml`, pinned by digest. `lc verify init` writes the file without one, so moving a repository from this machine to a server is one command and a commit. Until then, `lc task start ID --server URL` is refused (exit 1), nothing is created on the server, and the refusal names the fix:

```text
lc task: Workspaces server: a server task needs a [toolchain] image in .lemoncrow/environment.toml (HTTP 422)
lc task: fix it in the repository with: lc verify init --toolchain IMAGE  (IMAGE: the image the checks run in, such as python:3.12-slim)
lc task: then commit .lemoncrow/environment.toml, push it, and start the task again
```

A toolchain not pinned by digest gets the same, with the command naming that image (`--toolchain` pins it). A toolchain the server cannot pull, such as a private image, gets the server's reason and `lc verify init --toolchain IMAGE` for an image it can pull. The server reads the file at the commit the task starts from, so the fix counts once it is pushed; `lc task` does not check the file on this machine first.

## Workspaces namespace

`lc runtime` (short alias `lc rt`) contains Workspaces setup plus advanced machinery.

### Setup

```bash
lc rt status
lc rt enable claude
lc rt disable claude
```

Hosted retrieval (Pro) is a separate choice:

```bash
lc rt enable claude --hosted
```

Signing in does not silently turn an existing local integration into hosted execution.

### Local Savings (Verified Local Work)

Local Savings lets LemonCrow repair small mechanical failures with a local model **only when your existing checker can prove the candidate works**. It never switches the host agent's model and never rewrites a host-created subagent's model, so the expensive agent keeps its prefix cache.

The normal setup in the full LemonCrow CLI is one command:

```bash
lc rt savings enable
```

Serverless **Workspaces Lite** stays dependency-free and does not embed the local-model repair engine. Its thin client recognizes `runtime savings`, `runtime model`, and `runtime work` and delegates them to the optional full LemonCrow CLI when installed; otherwise it fails explicitly instead of pretending the command exists in Lite.

LemonCrow discovers standard loopback OpenAI-compatible runtimes (Ollama, LM Studio, llama.cpp, vLLM), probes a small bounded shortlist, selects a local worker, requires execution isolation for automatic promotion, and starts in **Learning** mode. Learning mode never changes the live checkout: it repairs/verifies candidates in isolation while the frontier agent continues normally.

Check progress with:

```bash
lc rt savings status
lc rt savings doctor
```

The state machine is:

```text
OFF -> LEARNING -> READY -> AUTO
                    |        |
                    +--------+-- degrades safely when recent quality drops
```

A repair class becomes Auto-qualified independently. For example, lint can be ready while typecheck keeps learning. By default Auto qualification requires all of the following for that model + repair class:

- a qualified local endpoint;
- qualified network isolation and resource limits;
- at least 20 recent verifier outcomes;
- at least 90% recent verifier success;
- acceptable average latency;
- at least 20 clean paired Learning baselines;
- positive calibrated net savings.

Learning provides the paired baseline naturally: the local worker attempts the repair in isolation, but Claude still sees the original failure and fixes it normally. LemonCrow measures the host work between that failure and the same verifier later passing, then pairs it with the local attempt. Economics qualification uses a bounded recent paired window and signed net savings (`measured host cost - actual local cost`), so newer negative economics can demote an old profitable class. Calibrated savings are reported separately from LemonCrow's older modelled savings and are never folded into `Total saved` as if they were the same evidence class.

Ready is not consent. Auto is opted in **per git repository**, and only after that repository's own recent verifier window qualifies. When a repair class is Ready here:

```bash
lc rt savings auto            # opt this repository in (records who and when)
lc rt savings auto --revoke   # withdraw this repository's opt-in
```

The opt-in covers only the (model, repair class) pairs Ready at that moment; a class or model that graduates later is never added until you rerun `lc rt savings auto`. Setting `verified_work.mode` to `enforce` by hand opts no repository in: without an opt-in, enforce behaves like Learning, and the first eligible failure in that repository asks once. Unqualified classes continue to the frontier agent unchanged. If recent model quality regresses, that class stops being selected even if its lifetime record was strong.

Every automatic repair prints the files it changed and its undo command:

```bash
lc rt savings undo <id>   # or: lc rt savings undo --last
```

Undo restores the exact pre-repair bytes, including uncommitted and untracked changes, only while the files still match the repair. If any file changed afterwards, it refuses and names the files. LemonCrow keeps the newest 50 repair records for 30 days.

Auto applies in Claude Code. Codex and OpenCode stay Learning-only; `lc rt savings status` says so.

Return to non-mutating learning or disable immediately (both revoke every repository opt-in):

```bash
lc rt savings learn
lc rt savings disable
```

A specific local worker can be selected without bypassing qualification:

```bash
lc rt savings enable --model custom/ollama/YOUR_MODEL
```

Advanced users can inspect/tune the underlying controls with:

```bash
lc settings show --category verified_work
lc rt work report
```

The low-level repair primitive remains available for qualification/debugging:

```bash
lc rt work repair --verify "npm run typecheck" --mode shadow
lc rt work repair --verify "uv run pytest -q tests/example.py::test_case" --mode enforce
```

`lc runtime savings` is the aggregate-savings and statusline surface. Cost-history detail and reset are available only under `lc usage optimize`.

### Advanced Workspaces commands

The Workspaces namespace also contains lower-level commands for benchmarking, telemetry, service operation, workers, model/agent/skill management, traces, dashboards, and other developer/enterprise workflows.

These surfaces are intentionally not the first-run path. Use:

```bash
lc rt --help
```

for the installed command tree.

Evaluation, profiling, and default bootstrap are checkout-only engineering
commands: `lc dev eval`, `lc dev perf`, and `lc dev defaults`. They require a
private LemonCrow checkout.

## Settings: `lc settings`

The installed client runs the plain commands in its Review engine; a source checkout (the full CLI) also opens an inline picker in a terminal. The essentials come first (reply style, commit attribution, status line, updates, offline mode, Review fix agent, approval gates, verified work). Every other setting is under **Browse all settings** or a `/` search away. Switches flip on Enter, choices open a list, and numbers or text are typed and validated. Each change is saved immediately. Off a terminal, or with `--no-input`, it prints the same overview instead.

```bash
lc settings show                 # essentials plus anything you changed (in the installed client: the settings lc review --ai uses)
lc settings show --all           # every setting, grouped by area (--category NAME for one)
lc settings get cli.telegraphic  # one setting: value, default, choices, env var, description
lc settings set cli.offline on
lc settings set llm.anthropic_api_key   # a key or token: leave the value out to type it without it landing in your shell history
lc settings reset cli.offline    # back to the default
```

An exported `LEMONCROW_*` environment variable wins over a saved value (an exported `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` wins over a saved key). `show`, `get`, and the picker mark such settings with `⚠`. API keys and tokens are never displayed. They are kept as plain text in `<root>/plugin_settings.json` (`~/.lemoncrow/plugin_settings.json`), a file only your user can read (mode 600): saving one says so, and the first save after an upgrade fixes an older, world-readable copy. There is no system keychain.

## Removal: `lc uninstall`

```bash
lc uninstall all --dry-run     # list what would be removed; changes nothing
lc uninstall all               # list it, ask once, remove what the installer added; then ask about your data
lc uninstall review            # Review only; Workspaces keeps working (or: workspaces, old name runtime)
```

It prints the plan before it does anything, and the dry run prints the same plan, so what it lists is what it removes. **Installed by LemonCrow** (removed after one question, or with `--yes`): the `lc` and `lcr` commands, the Review client and engine, the installer's scripts and the Reader, carried review results (a cache), the background server (stopped) and its process files, the lines the installer added to `~/.bashrc`, `~/.profile`, `~/.zshrc` and the other startup files you have (only that block, nothing else in them), and the git hooks `lc review hook install` wrote (with `all`, also the one `lc verify hook install` wrote, marked `# lemoncrow-verify-hook`): it keeps a list in `~/.lemoncrow/installed-hooks.txt`, and a hook is recognised by its `# lemoncrow-review-hook` marker line, in every repository on that list and in the one you run it in. A hook calls `lc`, so it goes with `lc`; a file at a listed path that has no marker (a hook you have since replaced with your own) is left alone. **Yours** (asked about, one question per group; `--yes` keeps them, `--purge` removes them): `~/.lemoncrow/plugin_settings.json` (your settings and any saved key), `~/.lemoncrow/server/data` (the reviews you opened in the Reader), a hosted sign-in, and the full CLI's telemetry files. **In your repository** (asked about; `--yes` keeps them): for the repository you run it in, the `.lemoncrow` folder and the marker file a review leaves; `--purge` never removes a repository's `.lemoncrow` folder. It does not run `npm`, does not touch coding-agent settings (except `lc rt disable`, when Workspaces is removed too) and leaves any file it does not recognise, listing it. Without a terminal it removes nothing until you pass `--yes`.

## Code map: `lc rt map` (full CLI)

Workspaces Lite has no web dashboard, so the full CLI's code map is in the terminal. `lc rt map` brings the local index up to date, then opens a picker: folders with their file and symbol counts, the most imported files, each file's symbols nested by extent with what it imports and what imports it, and a symbol's source. `/` searches symbols by name and `o` opens the selected file or symbol in `$VISUAL` / `$EDITOR` at its line. Off a terminal it prints the overview. With LemonCrow Review's local server running, `lc rt map --open` shows the same repository in the web dashboard.

## Context, sessions, usage, and recorded evidence (full CLI)

LemonCrow exposes recorded execution evidence rather than hidden model reasoning.

In a terminal, `lc session` lists recent sessions by day; Enter opens one's report, the same one `lc session report <id>` prints. `lc rt dashboard` adds up the last 7 days. Runs with no recorded usage are counted, not listed. `lc rt dashboard open` opens the web dashboard, which comes with LemonCrow Review.

Useful commands:

```bash
lc context --help
lc session --help
lc usage --help
lc run --help
```

Example `lc usage` output uses LemonCrow's own accounting basis rather than claiming a vendor invoice:

```text
NAME     SESSIONS  ROWS  TOKENS  COST   BASIS
project  2         14    182000  $3.42  estimated
```

The exact capabilities differ by host because Claude Code, Codex, OpenCode, Cursor, Pi, and other integrations expose different lifecycle and usage data. See the [host capability matrix](../hosts/host-capability-matrix.md).

## Hosted and shared services

Normal local Workspaces plugin use does not require a LemonCrow application server or a listening daemon.

Review, Workspaces Pro, Enterprise, API integrations, and self-hosted deployments can use shared HTTP services. Their service commands live under the advanced Workspaces/operations surfaces rather than the basic local plugin workflow.

See [Installation](../setup/installation.md), [Privacy & network](../setup/privacy.md), and [Production readiness](../operations/production-readiness.md).

## Low-level protocol commands

LemonCrow still exposes protocol-level commands for integration authors and compatibility work. Most users should not need them.

See [Protocol / MCP reference](../sdk/mcp.md) when you are building or debugging a host integration rather than simply using LemonCrow.
