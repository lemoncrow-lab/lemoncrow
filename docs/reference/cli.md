# LemonCrow CLI reference

`lc` is the command-line entry point for LemonCrow Runtime and Review.

There are two `lc` command trees:

- **The installed client.** `curl -fsSL https://lemoncrow.com/install.sh | bash` installs it. It carries Review capture (`lc review`, `lcr`), Runtime Lite (`lc with`, `lc rt ...`), and product management (`lc status`, `lc update`, `lc uninstall`, `lc auth`).
- **The full CLI.** A LemonCrow source checkout runs it as `uv run lc ...` (see [Installation](../setup/installation.md#source-checkout--contributor-setup)). It adds the commands marked *full CLI* below. The installed client recognizes those commands and says where they live instead of running them.

For the exact flags supported by your installed version, use built-in help:

```bash
lc --help
lc runtime --help
lc code --help
lc review --help
```

## Main workflow

The root command groups the product around the jobs developers actually do:

| Command | Purpose |
| --- | --- |
| `lc code` | Use LemonCrow's Hosted owned coding agent |
| `lc review` | Open or update a durable Review for the current change |
| `lc with <host>` | Try Runtime Lite with Claude Code, Codex, OpenCode, or Pi without changing saved host configuration |
| `lc rt host ...` | Manage public Pi host binaries; LemonCode host lifecycle needs the full CLI |
| `lc runtime index` | Build or refresh the local Runtime Lite index |
| `lc rt map` | Full CLI: browse this repository's code in the terminal: folders, symbols, imports, source |
| `lc rt dashboard` | Full CLI: spend and savings over the last 7 days, and the latest runs |
| `lc runtime` / `lc rt` | Manage Runtime integrations and advanced Runtime commands |
| `lc context` | Full CLI: inspect repository/agent context |
| `lc session` | Full CLI: pick a recent coding-agent session and read its cost and savings report |
| `lc usage` | Full CLI: summarize coding-agent usage across supported hosts |
| `lc run` | Full CLI: explain recorded work or drive an owned run |
| `lc status` | Show installed products, deployment, sign-in, and Runtime integrations |
| `lc auth` | Sign in/out of a configured hosted LemonCrow deployment |
| `lc audit` | Print what this client does and does not do (endpoint, credentials source, no telemetry) |
| `lc settings` | Full CLI: pick settings interactively, or show, get, set, and reset them |
| `lc doctor` | Full CLI: run installation and integration diagnostics. The installed client uses `lc status` and `lc rt status` |
| `lc update` | Re-run the installer for each installed product; host registrations are untouched |
| `lc uninstall review\|runtime\|all [--dry-run]` | Remove a product (required argument, no prompt) |

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

## Hosted coding agent: `lc code`

`lc code` has one product meaning: LemonCrow's Hosted owned coding agent. It is not the local Runtime Lite launcher and it has no `--pro` compatibility mode.

```bash
lc code
lc code -p "fix the failing parser test"
lc code --resume <session-id>
```

The open-source CLI may carry the auditable client surface, but the owned execution loop, provider/model routing, spend controls, cache orchestration, and advanced private intelligence belong to Hosted/Enterprise Runtime. Current builds fail closed if the remote owned-loop transport is not available.

For local Runtime Lite, use `lc with claude|codex|opencode|pi`, persistent `lc rt enable <host>` integration, or Cursor's integration from a source checkout. Build the local index explicitly with `lc runtime index`.

Pi and LemonCode are public coding-agent hosts, managed separately:

```bash
lc rt host install --engine pi
lc rt host install --engine lemoncode   # full CLI; the installed client says so
```

A Pro plan grants Hosted Runtime access. It is not an engine flag. The old `lc code --pro` mode is removed.

## Reviewing agent-written changes

The command is `lc review`.

The common case is:

```bash
lc review --open
```

With no explicit range, Review uses the current working-tree change; on a clean tree it falls back to the latest commit and says so. `--open` is the default; `--no-open` captures and prints the Review URL without opening a browser.

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
lc review watch                  # keep the Review current as source settles
```

Marking, commenting, changes since your review, and feedback for the author happen in the Review Reader (**Mark reviewed**, **Add comment**, **Changed since your review**, **Copy feedback**, **Send to agent**).

The full CLI (`uv run lc review` in a source checkout) adds scriptable forms of that loop and more. The installed client rejects these with a pointer to the Reader:

```bash
lc review --mark src/auth.py
lc review --comment "ttl is hardcoded" --on src/auth.py:L81
lc review --since-my-review
lc review --feedback
lc review --finish
lc review --current
lc review --resume-author
lc review --pr 123
lc review --pr 123 --ai --depth auto   # AI review in the terminal or CI; --publish-bot posts it as a bot
lc review --ai                        # AI review of the working tree (or --staged, REV, --base/--head); posts nothing
lc review hook install                # a git hook that runs it before a push (--pre-commit: before a commit)
lc review data-flow
lc review config --effective            # the merged review rules (review.yaml, import.rules_files, AGENTS.md) and their sources
lc review config --dry-run --pr 123   # which rules would apply to that pull request's files
lc review diagnostics
lc review observation-worker check --server URL --pins pins.json
```

### Local AI review and git hooks

`lc review --ai` reviews a change that has no pull request, on your own model key, and posts nothing anywhere. It runs the engine path the [GitHub Action](../setup/github-action.md) runs (capture, the AI stage, the same severity rule), so a finding has the same severity here as on a pull request.

```bash
lc review --ai                         # the working tree against HEAD (the last commit when the tree is clean)
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
| `0` | Clean: no open finding at or above `--fail-on`. An incomplete review is also `0` unless `--strict`: a missing key, a provider error or a spent budget never blocks a push by itself. |
| `1` | A finding at or above `--fail-on` is open. `--fail-on p0` (the default) stops on a P0, `p1` on a P0 or P1, `none` never. A found P0 outranks an incomplete review. |
| `2` | The review is incomplete and `--strict` is set: no model route, the intelligence kill switch is off, the model-call or dollar budget ran out, a model call failed, or risk signals were left unexamined (Auto depth examines two; `--depth thorough` covers more). |
| `3` | Usage error: a flag that cannot be combined with `--ai`, a value that is not one of the choices, a revision that does not resolve, or no Git repository. |

Nothing to review (no changed files) is `0` and needs no model. An error inside the review that is not a finding is reported as an incomplete review, never as exit `1`.

**What is shown.** The verdict line, then each open P0 and P1 with its place, why, the consequence and a checked replacement when there is one, the P2 findings as one line each, what was reviewed in this run and how much was reused, the model, depth and usage. Colour is off with `--no-color`, with `NO_COLOR`, and when output is not a terminal; text written by the model or a file name is stripped of control characters before it is printed.

**`--json`.** One document on standard output (progress goes to standard error), `schema: 1`:

| Field | Meaning |
| --- | --- |
| `kind` | `local_review` |
| `scope` | `{mode, label, base, head}`: `working_tree`, `staged` or `commit_range`, the commits or tree the review covered |
| `empty`, `cached` | nothing to review; no model call was needed because every changed unit was reviewed, unchanged, last time |
| `verdict` | `{state, headline, p0, p1, p2}`; `state` is `clean`, `must_fix`, `partial`, `incomplete` or `budget` |
| `complete`, `incomplete_reason` | whether the review finished and examined every risk signal, and why not |
| `exit` | `{code, reason, fail_on, strict}`: the exit status and why |
| `findings` | every tracked finding as the pull request bot's hidden state records it (`id`, `severity`, `category`, `state`, `path`, `line`, `end_line`, `title`, `explanation`, `consequence`, `suggestion`, ...); `state` is `new`, `carried`, `fixed`, `stale`, `unreported` or `suppressed` |
| `coverage` | `{files, targets, rereviewed, unchanged, candidates, investigated}` |
| `depth`, `model`, `usage`, `usage_total` | the run's depth, the model that served it, and its usage (`calls`, `input_tokens`, `output_tokens`, `cost_usd`) and the total for this change |
| `notes` | things worth knowing, such as the clean-tree fallback |

**Model route.** Your own: `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`, or `LEMONCROW_LLM_BACKEND` for LiteLLM, Ollama or a local agent CLI (`claude`, `codex`, `opencode`) with its variables, as `--pr N --ai` uses. The intelligence policy and its kill switch apply: with AI turned off, nothing is sent and the review is incomplete (exit `0`, or `2` under `--strict`).

**Carried results.** Like the bot, the review keeps its result between runs, in a small file under the LemonCrow root (`review-local-ai/`), one per change: the working tree and the staged changes of a branch, or a range's base and the branch it ends on. A re-run reviews only what changed since: on an unchanged tree no model call is made and the open findings still decide the exit code; after an edit only the changed files go to the model; a fixed finding is closed. A changed review policy or depth reviews everything once; `--fresh` does so on request. `--max-usd-per-pr` and `--max-runs-per-pr` limit what one change may cost over its runs, read from the same carried usage; when they are reached nothing new is reviewed and the findings stand.

**`lc review hook install`** writes a hook that runs this before a push, or before a commit with `--pre-commit`:

```bash
lc review hook install                        # .git/hooks/pre-push (or wherever core.hooksPath points)
lc review hook install --print                # show the script, write nothing
lc review hook install --fail-on p1 --strict  # the options the hook runs with
lc review hook install --pre-commit           # review what is staged before each commit
lc review hook install --force                # replace a hook that already exists
```

The script is a few lines of shell around `lc review hook run`. For a push it reviews what the push would publish: the commits of each pushed branch that the remote does not have (from where a new branch leaves the default branch). Branch deletions and tags are skipped. A review that cannot be done lets the push through and says why, unless the hook was installed with `--strict`. Skip it once with `LEMONCROW_REVIEW_SKIP=1 git push` (`git push --no-verify` skips every hook). The installer never overwrites a hook that exists, whether another tool's or an earlier one of its own with other options, without `--force`; installing the same hook twice writes nothing the second time. A model call happens on every push with new code, so keep the depth at `auto` for hooks.

Review keeps durable human judgment across Snapshots where the underlying reviewed work is unchanged; changed or new work returns to the review queue.

See [`lc review`](review.md) for the product model and [Review walkthrough](review-walkthrough.md) for a hands-on flow.

## Runtime namespace

`lc runtime` (short alias `lc rt`) contains Runtime setup plus advanced machinery.

### Setup

```bash
lc rt status
lc rt enable claude
lc rt disable claude
```

Hosted Runtime is a separate choice:

```bash
lc rt enable claude --hosted
```

Signing in does not silently turn an existing local integration into hosted execution.

### Local Savings (Verified Local Work)

Local Savings lets LemonCrow repair small mechanical failures with a local model **only when your existing checker can prove the candidate works**. It never switches the host agent's model and never rewrites a host-created subagent's model, so the expensive agent keeps its prefix cache.

The normal setup in the full Runtime CLI is one command:

```bash
lc rt savings enable
```

Serverless **Runtime Lite** stays dependency-free and does not embed the local-model repair engine. Its thin client recognizes `runtime savings`, `runtime model`, and `runtime work` and delegates them to the optional full Runtime CLI when installed; otherwise it fails explicitly instead of pretending the command exists in Lite.

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

### Advanced Runtime commands

The Runtime namespace also contains lower-level commands for benchmarking, telemetry, service operation, workers, model/agent/skill management, traces, dashboards, and other developer/enterprise workflows.

These surfaces are intentionally not the first-run path. Use:

```bash
lc rt --help
```

for the installed command tree.

Evaluation, profiling, and default bootstrap are checkout-only engineering
commands: `lc dev eval`, `lc dev perf`, and `lc dev defaults`. They require a
private LemonCrow checkout.

## Settings: `lc settings` (full CLI)

In a terminal, `lc settings` opens an inline picker. The essentials come first (reply style, commit attribution, status line, updates, offline mode, Review fix agent, approval gates, verified work). Every other setting is under **Browse all settings** or a `/` search away. Switches flip on Enter, choices open a list, and numbers or text are typed and validated. Each change is saved immediately. Off a terminal, or with `--no-input`, it prints the same overview instead.

```bash
lc settings show                 # essentials plus anything you changed
lc settings show --all           # every setting, grouped by area (--category NAME for one)
lc settings get cli.telegraphic  # one setting: value, default, choices, env var, description
lc settings set cli.offline on
lc settings reset cli.offline    # back to the default
```

An exported `LEMONCROW_*` environment variable wins over a saved value. `show`, `get`, and the picker mark such settings with `⚠`. API keys and tokens are never displayed.

## Code map: `lc rt map` (full CLI)

Runtime Lite has no web dashboard, so the full CLI's code map is in the terminal. `lc rt map` brings the local index up to date, then opens a picker: folders with their file and symbol counts, the most imported files, each file's symbols nested by extent with what it imports and what imports it, and a symbol's source. `/` searches symbols by name and `o` opens the selected file or symbol in `$VISUAL` / `$EDITOR` at its line. Off a terminal it prints the overview. With LemonCrow Review's local server running, `lc rt map --open` shows the same repository in the web dashboard.

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

Normal local Runtime plugin use does not require a LemonCrow application server or a listening Runtime daemon.

Review, hosted Runtime, Enterprise, API integrations, and self-hosted deployments can use shared HTTP services. Their service commands live under the advanced Runtime/operations surfaces rather than the basic local plugin workflow.

See [Installation](../setup/installation.md), [Privacy & network](../setup/privacy.md), and [Production readiness](../operations/production-readiness.md).

## Low-level protocol commands

LemonCrow still exposes protocol-level commands for integration authors and compatibility work. Most users should not need them.

See [Protocol / MCP reference](../sdk/mcp.md) when you are building or debugging a host integration rather than simply using LemonCrow.
