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
