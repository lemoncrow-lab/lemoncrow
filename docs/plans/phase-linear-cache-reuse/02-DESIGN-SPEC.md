# Design Spec — Phase-Linear Cache-Reuse Run Mode

Concrete design for the run mode. Written as Atelier's own architecture; all
mechanisms are described in first-principles terms.

## 1. A declarative phase state machine

A run is a small ordered state machine. Each step is one of: an **agent turn**, a
**human gate**, or a **side-effect** (e.g. persist the plan). A minimal schema:

```jsonc
{
  "name": "plan_run",
  "entry": "survey",
  "steps": {
    "survey":    { "kind": "agent", "profile": "reader",
                   "objective": "prompts/survey.md", "next": "plan" },
    "plan":      { "kind": "agent", "profile": "reader",
                   "continue_from": "survey",          // reuse the survey conversation
                   "objective": "prompts/plan.md",   "next": "review" },
    "review":    { "kind": "gate", "prompt": "Accept this plan?",
                   "accept": ["implement", "persist_plan"], "reject": ["stop"] },
    "implement": { "kind": "agent", "profile": "writer",
                   "objective": "prompts/implement.md", "input": "{{plan}}" },
    "persist_plan": { "kind": "side_effect", "action": "save_plan" }
  }
}
```

## 2. `continue_from` is the cache mechanism

The load-bearing field is **`continue_from`**. When the Plan step declares
`continue_from: survey`, the runner does **not** open a new conversation — it
takes the Survey step's full message list (system prompt + every file read + tool
result) and appends the Plan objective. The prefix is identical, so the provider
prompt cache is a hit for the entire Survey history. A cache breakpoint is set at
the tail of each phase via the existing `prefix_cache/planner.py`.

Deliberate scope: cache continuation covers the **read-heavy Survey→Plan**
boundary, where nearly all the ingested-token cost lives. The **Implement** step
is a *separate* writer agent that receives only the approved plan text — it does
not continue the conversation, so it starts lean with write tools.

## 3. One fixed system prompt + phase objectives as user messages

All agent profiles share the **same** system-prompt body. It ends with a short
statement that the run proceeds through ordered phases and that each phase begins
with a user message defining the current objective. Behavior therefore lives in
the per-phase **user** message, not in a per-phase system prompt — which is what
keeps the cached system prefix constant.

Each phase objective (a small markdown file) opens with a one-line header telling
the model to set aside the prior phase and focus on the new objective:

- **survey.md** — read-only investigation; goal is to build grounding, end with a
  brief "survey complete" signal, no file changes.
- **plan.md** — read-only; produce a step-by-step implementation plan and end with
  a short "critical files" list. Do not re-read what is already in history.
- **implement.md** — apply the plan precisely, file by file; reuse file contents
  already in history before re-reading; make no unrequested changes.

## 4. Two profiles, differing only by tool grants

| Profile | Tools | Used by |
|---|---|---|
| `reader` | read, search, glob, language-server query, web (NO write/edit/delete) | survey, plan |
| `writer` | reader tools **plus** write, edit, delete | implement |

The read-only grant during survey/plan structurally enforces "plan before you
touch anything."

## 5. Minified reads

A `minify_source(text, lang)` helper collapses non-semantic whitespace on the
read path that feeds files into context. It must preserve meaning for
whitespace-significant languages (Python, YAML) by limiting itself to safe
line-trimming there; for others it can collapse whitespace runs more
aggressively. The runner records original vs. minified token counts so the
savings are measurable. Minification applies to the `reader` profile only; the
`writer` profile reads exact bytes.

## 6. Cache-warmth guard & telemetry

- Track wall-clock since the last provider call; if a phase hand-off would land
  outside the cache TTL window, either bridge with a tiny keep-alive call or log
  the cold read.
- Record per turn: fresh-input / cache-write / cache-read / output token counts,
  plus stripped-whitespace counts, into the run ledger. The cache-read share is
  the proof the mechanism is working — do not assume it.

## 7. Mode selection

A `mode` switch chooses `linear` (this design), `per_agent` (current flow), or
`auto`. The `auto` heuristic picks `linear` when phases share context and the
projected prefix is under threshold, and falls back to `per_agent` for tasks
with divergent sub-contexts or very large fan-out.

## 8. Mapping to Atelier modules

| Design element | Atelier home |
|---|---|
| Phase state machine + runner | new `PhaseRunner` in `core/capabilities/context_reuse/`, driven by `core/runtime/engine.py` |
| `continue_from` (warm prefix) | one shared messages list; breakpoints via `prefix_cache/planner.py` |
| Shared system prompt + phase headers | `prompts/` templates under the new capability |
| `reader` vs `writer` tool grants | per-phase tool allowlist on the runner |
| `minify_source()` | `core/capabilities/context_compression/` |
| Human review gate | `core/capabilities/proof_gate` / AskUserQuestion |
| Cost/cache telemetry | `pricing.py` + `infra/runtime/` ledger |
| Persist accepted plan | run-dir artifact write |

**Minimal viable version = three things:** (1) the Plan phase continues the
Survey conversation (warm cache), (2) phase behavior carried in injected user
messages over a fixed system prompt, (3) minified reads during survey/plan.
The review gate, prefix compaction, and `auto` mode are follow-on polish.
