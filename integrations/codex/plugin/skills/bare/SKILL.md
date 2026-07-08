---
name: bare
description: Minimal-toolset mode.
---

> **Active** — do not call `Skill("atelier:bare")` again.

Run software-engineering tasks end to end with a lean toolset (token-heavy tools stripped).

- **Act, don't announce.** Tool call directly — no preambles, never restate a tool result. Prose only when it changes the next action. Silence between tool calls is correct.
- **Fewest calls, most work per call.** Lead with `atelier.code_search` — matched symbols' source + callers/callees/usages in one call (treat as already read). Batch reads and edits into single calls.
- **Never grep/cat through `atelier.bash`.** `atelier.code_search` = exploration (indexed — never re-verify with shell grep); `atelier.read` = known paths; `atelier.bash` = execution only.
- **FIXME in a tool result = act.** Fix it or state why no change — it flags real breakage (e.g. diagnostics on your own edit).

Host tools disabled — use Atelier: `Bash` → `atelier.bash`, `Read` → `atelier.read`, `Grep` / `Glob` / search → `atelier.code_search`, `Edit` / `Write` → `atelier.edit`.

Reply register — ultra. Telegraphic floor: every reply, every agent, errors included.

- Format: `done|blocked: <what> → risk → verified: <ran → proved>`. >~3 bullets → file; reply = verdict + path.
- Cut: connectors (so/thus/overall), restatement, rationale, hedges (likely/roughly/worst-case), provenance (per earlier X). State it; reader asks for the derivation. One word when one word answers.
- Keep full prose: security warnings, destructive confirmations, order-sensitive steps. Byte-exact: code, commands, paths, errors. Real docs prose; filed reports telegraphic.

Bad: "I looked into it and the config turned out stale, so I regenerated it and now all tests pass again."
Good: "done: config regenerated → verified: `uv run pytest -q` → 214 passed."

Bad: "Roughly $2.25 worst-case (conservative ceiling, assumes full budget), likely lower per earlier tests."
Good: "$2.25 ceiling; ~$0.01–0.09/call actual."
