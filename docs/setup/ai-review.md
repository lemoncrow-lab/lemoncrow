# Your first AI review

An AI review of the change you have not committed yet, printed in your terminal. It posts nothing anywhere.

## Three steps

```bash
curl -fsSL https://lemoncrow.com/install.sh | bash         # 1. install
export ANTHROPIC_API_KEY=sk-ant-...                         # 2. a key (or OPENAI_API_KEY), or use an agent instead (below)
cd your-project && lc review --ai                           # 3. the review
```

Open a new terminal after the install, or run the `source` line the installer prints. A plain `lc review` (no `--ai`) never calls a model, even when a key is exported: a key you set for other tools is not permission to spend it.

## No key? Use an agent you are signed in to

If you already use Claude Code, Codex or OpenCode, let it do the review on its own plan. You name it, so nothing is ever spent through it by surprise:

```bash
lc review --ai --agent claude        # or: codex, opencode
lc review --ai --agent claude --remember   # once; after that `lc review --ai` uses it whenever no key is set
```

With no key set, `lc review --ai` tells you which of the three it found on your machine and shows the command, but never starts one on its own. An exported `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` wins over a remembered agent. To stop using it: `lc settings reset review.agent`.

## What it says when nothing is set up

```text
No AI model is set up. Set a key:  export ANTHROPIC_API_KEY=sk-ant-...   (or OPENAI_API_KEY=sk-...)
Or use an agent you are signed in to:  lc review --ai --agent claude   (also codex, opencode)
Details: https://docs.lemoncrow.com/setup/ai-review
```

and exits with status 3 (a script or CI step sees that the review did not run). A git hook never blocks on this: see [exit codes](../reference/cli.md#local-ai-review-and-git-hooks).

If you reviewed this change before with a key, you see those findings first, under the line `Not re-checked: these findings are from your last review of this change.`, then the same three lines; the exit code stays the findings' (1 for an open P0), because nothing could re-check them. The local Reader says it the same way: `lc review` (no `--ai`) opens it, and its AI review line reads `No AI model is set up.` with the same key and agent commands and this page's link. AI review itself runs from your terminal with `lc review --ai`.

## When it asks the model nothing

The model is not asked about every change. It is asked about a *risk signal* the review finds without a model (code whose path or name says security, an API, concurrency, storage, caching or performance, or a check that failed) and about each *rule* you wrote in `.lemoncrow/review.yaml`. A change that raises neither is not sent to the model, and the review says so under the `Model:` line:

```text
No must-fix findings
Reviewed 1 target in this run · 1 file in the range
Model: none · depth auto · no model calls
No model was asked about this change: no risk signal matched it and no review rule covers it. To have the model check it, add a rule under rules: in .lemoncrow/review.yaml.
```

`No must-fix findings` there means no finding was raised, not that a model read the change. A rule such as "Changed code must not introduce a logic error" (scope it with `paths:`) puts every change it covers in front of the model. A rule is a critical rule: a violation it finds is a P0.

## Keep your key instead of exporting it

```bash
lc settings set llm.anthropic_api_key     # asks for the key, so it stays out of your shell history
lc settings set llm.openai_api_key
```

The key is stored as plain text in `~/.lemoncrow/plugin_settings.json`, a file only your user can read (mode 600; an older, world-readable copy is fixed the next time anything is saved). There is no system keychain. A key you export in your shell always wins over a saved one. `lc settings` shows what is set (never the key itself); `lc settings reset llm.anthropic_api_key` removes it.

## When the provider says no

A key the provider rejects, an account with no credit and a rate limit each stop the review with one line saying which, and exit 3:

| You see | Do this |
| --- | --- |
| `Anthropic rejected the API key (HTTP 401): check ANTHROPIC_API_KEY.` | Check the key you exported (or saved with `lc settings`), or set it again. |
| `Anthropic says your account has no credit left (HTTP 400): add credit at console.anthropic.com/settings/billing.` | Add credit at the page it names. |
| `Anthropic rate limit reached (HTTP 429): wait a minute and run it again.` | Wait, then run it again; an unchanged tree makes no new model call. |

(OpenAI is the same, with `OPENAI_API_KEY` and its own billing page.)

## Which settings does it use?

The review rules and noise settings in `.lemoncrow/review.yaml` of **your working tree**, including a file you have written and not committed yet. The output says so in one line, right under the title:

```text
Config: .lemoncrow/review.yaml from your working tree (not committed yet: pull request reviews read the committed copy).
```

A pull request review, run by the GitHub Action, still reads the committed base branch, so a pull request cannot loosen its own review. To see every setting and rule in force, each with the file it came from:

```bash
lc review config --effective           # your working tree
lc review config --effective --base main   # what a pull request into main would use
```

## Next

```bash
lc review hook install      # an AI review before each push; it stops only on a real finding, never because LemonCrow failed
lc review init-action       # the GitHub Action workflow for pull requests
lc uninstall all --dry-run  # what removing LemonCrow would delete, without deleting anything
```

The installed `lc` sends no telemetry. The full engine from a source checkout does, and prints a one-line notice on its first run: [Privacy & network behavior](./privacy.md).
