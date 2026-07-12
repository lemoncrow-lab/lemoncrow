# Integrations — how this directory works and why

Design record for the agent instruction surface. Read this before "cleaning up"
anything that looks redundant or inconsistent here — most of it is deliberate,
and this file exists so those decisions survive across sessions and reviewers.

## Layout: sources vs generated

- **`agents/*.md`** — the ten mode docs (code, bare, auto, general, execute,
  solve, explore, plan, review, research). Source of truth, one per role.
- **`agents/shared/*.md`** — shared partials spliced into mode docs via
  `{{TOKENS}}` (`{{CORE_DISCIPLINE}}`, `{{CHANGE_DISCIPLINE}}`,
  `{{CODING_GUIDELINES}}`, `{{TOOL_DISCIPLINE}}`, `{{TOOL_DISCIPLINE_READ}}`).
- **Everything else** (`claude/plugin/agents/`, `codex/plugin/skills/`,
  `antigravity/`, `copilot/`, `cursor/`, `opencode/`, `.github/agents/`) —
  **generated** by `make sync-agent-context` (`scripts/sync_agent_context.py`).
  Never hand-edit generated files; edit the sources and re-run the sync, then
  `bash scripts/install_claude.sh` to deploy the Claude bundle.

Do not add stray `.md` files under `agents/`: `load_mode_docs()` parses every
top-level `*.md` there and requires `mode:` frontmatter (a README would crash
it), and `tests/gateway/test_telegraphic_budget.py` counts every `.md` under
`agents/` (including `shared/`) against a hard persona token ceiling. That is
why this README lives one level up.

## The telegraphic register is stated twice — on purpose

Every editing persona carries the register in two places:

1. **Top** — the `Output proportional — always default to the telegraphic
   register` bullet in `shared/core-discipline.md`: the full definition (what
   to drop, when to expand, what may never be cut).
2. **Bottom** — a one-line `Reply register: telegraphic — …` echo as the last
   line of the mode doc, with a short example reply.

This is not accidental duplication. Style rules are the weakest instruction
class: they compete with the model's trained prose default on every token and
decay as context grows, and the *end* of the prompt dominates the register of
the next reply. Primacy defines, recency reminds. Removing either half
measurably weakens adherence in long sessions — **do not deduplicate**.

Related choices in the same area:

- The wording is *"always default to"*, not *"always reply"* — the same bullet
  keeps three escape hatches (expand on user request or material complexity;
  full explicit prose for safety-critical content; a mode's declared output
  contract wins). A literal "always" would contradict them.
- The example reply (`Example: "Fixed race in engine.py:412 …"`) appears
  **only in editing modes** (code, bare, auto, general, execute, solve).
  Few-shot examples pull behavior harder than prose; a code-change exemplar in
  a read-only persona would model exactly the reply those modes must never
  produce. Explore gets a findings-and-citations echo instead.
- **Contract-bearing modes (plan, review, research) have no echo at all** —
  they end on their own output contract (plan format, fenced JSON verdict,
  memo format). The recency slot belongs to the contract, and review's "the
  JSON verdict is the final element" would conflict with anything after it.
- Canonical shared-block order in mode docs: `CORE_DISCIPLINE` →
  `CHANGE_DISCIPLINE` → mode-specific extras → `CODING_GUIDELINES` →
  `TOOL_DISCIPLINE`. Keep new modes on that order so recency weighting stays
  comparable across roles.
- All of this text ships on every request, so it is written telegraphically
  and budget-gated by `tests/gateway/test_telegraphic_budget.py`. Compress
  before raising a ceiling. Human-facing docs (like this one) stay prose.

## Claude host decisions

- **Tool naming.** The canonical install is the user-scope MCP server
  registered by `scripts/install_claude.sh` (`claude mcp add --scope user
  lemoncrow …`), which yields `mcp__lc__*` tool names — so generated agent
  bodies use that prefix. A marketplace plugin install would namespace them as
  `mcp__plugin_lemoncrow_lc__*`. Runtime consumers (hooks, session parsers)
  accept both shapes, and the `disallowedTools` deny-list blocks **both** edit
  spellings so read-only roles stay read-only under either install.
- **Claude personas ship a thin tool-discipline block.** Claude Code folds the
  MCP server's `instructions` (the full generic tool discipline) into every
  context, so the persona only adds what that string cannot carry: delegation
  targets and the host tool-name mapping. Other hosts get the full shared
  partial.
- **Spawn-aware delegation.** Roles whose policy denies the `Agent` tool
  (execute, solve) do not get the "delegate read-only work to
  lc:explore/lc:plan" line — telling an agent to use a tool it
  cannot call wastes a turn.
- **auto has an unattended override.** `auto` denies questions and plan gates
  (CI/headless), but the shared change discipline demands explicit
  confirmation before destructive actions. The override resolves the
  contradiction: don't ask, don't proceed — finish the safe remainder and
  report the destructive step as blocked in the summary.
- **Read-only roles keep `bash`.** Review needs to run tests; explore/plan
  need git inspection. The `edit` tool is policy-denied (both name shapes),
  and shell mutation is forbidden at the prompt level ("read-only role —
  `bash` never mutates" in `shared/tool-discipline-read.md`). Known residual:
  this half is prompt-enforced, not policy-enforced — accepted trade-off.

## Where to change what

| Change | Edit |
| --- | --- |
| Behavior shared by every role | `agents/shared/*.md` |
| One role's behavior or output contract | `agents/<role>.md` |
| Claude-specific overrides, tool prefix, frontmatter | `scripts/sync_agent_context.py` |
| Tool deny policy per role | `_claude_disallowed_tools` in `src/lemoncrow/core/capabilities/default_definitions.py` |

After any of the above: `make sync-agent-context`, run
`tests/gateway/test_telegraphic_budget.py` and
`tests/gateway/test_generated_agent_contexts.py`, then
`bash scripts/install_claude.sh`.
