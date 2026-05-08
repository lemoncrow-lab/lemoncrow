# Atelier — Positioning & Adoption Plan

**Status:** Draft v1 · 2026-05-05
**Audience:** Coding agent implementing the additions below. Read top-to-bottom before
writing any code.
**Companion to:** [IMPLEMENTATION_PLAN_V3.md](IMPLEMENTATION_PLAN_V3.md),
[V3 INDEX](work-packets-v3/INDEX.md).

---

## 0. What this document is and is not

**Is:** a positioning statement, three concrete adoption-tooling additions, hard scope
boundaries, and an implementation order. Mid-level — concrete enough to implement, not so
formal as to need full packet decomposition.

**Is not:** a V4 plan, a marketing doc, an architectural rewrite, or a license to broaden
Atelier into a different product. Every change here is additive and stays within the V3
boundaries.

If, while implementing, the work seems to require changing Atelier's architecture (running
an agent loop, calling LLMs on the user's hot path, owning the host's tool-dispatch loop,
adding a UI layer that's not a doc), **stop and re-read § 4 (Hard rules).**

---

## 1. The positioning

> **Atelier is governance for AI-assisted coding.**
>
> Companies have style guides, ADRs, lint rules, and CI gates. None of them apply to an AI
> agent's output. Atelier is the layer that makes the company's existing rules actually
> _retrieved_ by the agent on every task, _checked_ before code is written, _verified_ on
> outcome, and _audited_ per task — with evidence.

### What this is _not_ a claim of

- Atelier is not a linter — those exist and Atelier doesn't replace them.
- Atelier is not a wiki replacement — Confluence/Notion exist and Atelier doesn't replace
  them.
- Atelier is not a CI gate — branch protection and GitHub Actions exist and Atelier doesn't
  replace them.
- Atelier is not a code-review tool — humans still review.

### What it _is_ a claim of

Atelier provides the layer that all of those tools fail at when an AI agent is in the loop:

| Existing tool      | Why it fails for AI-driven coding          | Atelier's role                              |
| ------------------ | ------------------------------------------ | ------------------------------------------- |
| Style guide / wiki | Agent doesn't read them                    | ReasonBlocks: agent retrieves on every task |
| Lint / format      | Catches syntax, not procedure              | Rubric gates catch procedural decisions     |
| CI rules           | Post-hoc, agent already committed          | `lint` validates before code is written     |
| Code review        | Doesn't scale to high-volume agent commits | Per-task trace + rubric verdict per change  |
| ADRs               | Static, not retrieved at decision time     | ReasonBlocks are retrieved at decision time |

### One-line pitch

> _"You already have the rules. Atelier makes the AI agent on your team actually follow
> them, on every task, with evidence."_

Use this language in README, docs, replies to inbound interest. Do not invent a fancier
version.

---

## 2. Why we need adoption tooling

V3 shipped the technical surfaces that deliver on the positioning above. **The adoption
gap is operational, not technical.** Three frictions block real-world rollout:

1. **Empty-block-store problem.** A new user runs `atelier init` and gets an empty
   `./.knowledge/blocks/` directory. They have to author institutional knowledge from a blank
   page. Most quit before writing the third block.
2. **Existing-docs gap.** Companies have `STYLE.md`, `CONTRIBUTING.md`, exported Confluence
   pages full of procedural rules. None of it is in a form Atelier can use. Re-authoring it
   manually is a multi-day task nobody volunteers for.
3. **No leader-facing evidence.** Engineering managers care about adherence trends, not raw
   traces. There is currently no way to answer "what % of agent-driven changes passed
   rubric gates last week?" without ad-hoc SQL.

The three additions in § 5 each remove one friction. They are scoped, small, and
boundary-safe.

---

## 3. What's already shipped (don't re-build)

Per the V3 audit on 2026-05-05, all 8 V3 packets and 4 V3.1 packets are implemented in
code. Specifically, these surfaces are live:

- ReasonBlock store with versioned, retrievable blocks (`./.knowledge/blocks/*.md`).
- MCP tools: `reasoning`, `lint`,
  `verify`, `rescue`, `trace`,
  `memory_*` family, `lesson_inbox` / `lesson_decide`, `consolidation_inbox` /
  `consolidation_decide`, `search`, `edit`,
  `atelier sql inspect`, `compact`, `atelier_repo_map`.
- CLI: `atelier letta &#123;up,down,logs,status,reset&#125;`, `atelier reembed`,
  `atelier consolidate`.
- Local Ollama integration via `src/atelier/infra/internal_llm/ollama_client.py`
  (boundary: internal/background only, never user-hot-path).
- Letta self-hosted Docker setup (`deploy/letta/docker-compose.yml`).
- Honest measured savings benchmark (currently 13.27 % per `BenchmarkRun`).
- Boundary CI gate (`tests/infra/test_no_external_llm_clients.py`).

**Do not re-implement any of these.** The three additions in § 5 build on top.

---

## 4. Hard rules (do not violate; rejection on PR review otherwise)

These are inherited from V3 and apply to every change in this doc.

1. **Atelier never drives the user-facing agent loop.** The host CLI (Claude Code, Codex,
   opencode, Gemini, custom) owns the conversational loop, the tool dispatch, and the
   subagent spawning. Atelier provides MCP tools the host calls.
2. **Atelier never calls a remote/billed LLM.** No `anthropic`, `openai` (except for
   `text-embedding-3-small` inside `infra/embeddings/`), `litellm`, `cohere`, `mistralai`,
   `google.generativeai`. The boundary CI gate enforces this.
3. **Internal Ollama only.** Local Ollama via `infra/internal_llm/ollama_client.py` is
   allowed for background processing (lesson reflection, summarization, consolidation,
   memory arbitration, the import-style-guide CLI in § 5.2). It is never on the user's
   hot path.
4. **No new agent framework dependencies.** No LangGraph, no Deep Agents, no DSPy. Atelier
   is a tool/data provider, not an agent runtime.
5. **No marketing language without measurement.** Any percentage in README, docs, or
   commit messages must link to a `BenchmarkRun` row or be qualified as "design target"
   with a footnote.
6. **Human review gates remain.** Any change that auto-mutates ReasonBlocks, lessons, or
   memory blocks without human approval is out of scope.

---

## 5. Three additions

Each subsection is a self-contained mini-spec. The implementation order in § 6 reflects
leverage-per-effort.

### 5.1 — Engineering-leader weekly report (`atelier report`)

**Why.** Engineering managers want adherence evidence, not raw traces. Today there is no
single command that answers "is the team using Atelier and is it working?"

**What to build.**

- **CLI:** `atelier report --since 7d [--format markdown|json] [--output PATH]`
  - Default: prints Markdown to stdout suitable for a Slack post or weekly email.
  - `--format json`: stable, machine-parseable output for downstream automation.
  - `--output PATH`: write to file instead of stdout.
- **Capability:** `src/atelier/core/capabilities/reporting/weekly_report.py` —
  `generate_report(since: timedelta, ...) -> Report` that reads the trace store + memory
  store and computes:
  - **Rubric pass rate** overall and per `domain` (e.g., `beseam.shopify.publish`,
    `beseam.pdp.schema`).
  - **Top 5 ReasonBlocks** retrieved (by `reasoning` count).
  - **Top 5 rubric failures** with affected file paths and a one-line failure summary
    extracted from the trace's `output_summary`.
  - **New lesson candidates** pending review (count + top 3 by cluster size).
  - **Drift signals**: rubric pass rate vs. prior period; rescue-attempt rate change;
    average context budget usage trend.
- **Pydantic model:** `Report` carries the structured fields above plus `period_start`,
  `period_end`, `git_sha` of the repo at report time.
- **Markdown rendering:** `render_markdown(report) -> str`. Stable, scannable format.
  Headings, small tables, no emoji. Suitable for paste into Slack.
- **MCP tool:** `atelier report(since_iso, format)` — host can call programmatically.

**Acceptance.**

- `atelier report --since 7d` runs in < 3 seconds on a repo with thousands of recorded
  traces.
- Markdown output is < 80 lines for a typical week and renders cleanly in Slack.
- JSON output passes a JSON schema validation test.
- Drift signals are computed by comparing the report period to the immediately prior
  period of the same length.
- Tests: `tests/core/test_weekly_report.py` (unit-level, with fixture trace store) and
  `tests/gateway/test_report_cli.py` (CLI smoke).

**Boundary.** Read-only over the trace + memory stores. No LLM call. Pure deterministic
aggregation.

**Effort.** 2-3 dev-days.

---

### 5.2 — Style-guide importer (`atelier import-style-guide`)

**Why.** The single biggest adoption blocker. Companies have `STYLE.md`,
`CONTRIBUTING.md`, exported Confluence pages, ADRs in folders. None of them are in a form
Atelier uses. Manually re-authoring as ReasonBlocks is nobody's priority.

**What to build.**

- **CLI:** `atelier import-style-guide PATH [PATH ...] [--domain DOMAIN] [--dry-run]
[--limit N]`
  - Accepts one or more Markdown files or directories (recurses into directories).
  - Uses local Ollama (already in scope per V3 WP-36) to extract procedural rules from the
    text and draft `LessonCandidate` rows.
  - Each candidate is surfaced via the existing `lesson_inbox` workflow for human review;
    nothing is auto-promoted to a ReasonBlock.
  - `--dry-run` prints proposed candidates without writing.
  - `--limit N` caps candidates produced (avoids generating 200 candidates from a giant
    `STYLE.md`).
- **Capability:** `src/atelier/core/capabilities/style_import/importer.py`:
  - `import_files(paths, domain) -> list[LessonCandidate]`
  - Pipeline:
    1. Collect input files (recurse, filter to `.md`, skip blacklisted paths like
       `node_modules/`, `_book/`, `_site/`).
    2. Chunk each file at H2/H3 boundaries (or every ~800 tokens if no headings).
    3. For each chunk, call `internal_llm.ollama_client.chat` with a fixed prompt that
       extracts: (a) is this procedural? (b) if yes, draft a one-paragraph block body in
       the project's existing ReasonBlock style.
    4. Skip chunks the LLM marks non-procedural.
    5. Embed each draft (already deterministic via `Embedder`) and check against existing
       ReasonBlocks — flag near-duplicates so reviewer can merge instead of accept.
    6. Write surviving drafts as `LessonCandidate` rows with
       `source = "style-guide-import"` and `evidence = &#123;file_path, chunk_range&#125;`.
- **Prompt template:** `src/atelier/core/capabilities/style_import/prompts.py` — single
  fixed prompt, version-stamped. Do not let the prompt drift between releases.
- **Tests:**
  - `tests/core/test_style_import_chunking.py` — chunker handles headings, code fences,
    and very long sections.
  - `tests/core/test_style_import_pipeline.py` — with mocked Ollama, fixture
    `CONTRIBUTING.md` produces N expected candidates.
  - `tests/core/test_style_import_dedup.py` — near-duplicate against existing block is
    flagged, not silently dropped.
  - `tests/gateway/test_import_style_guide_cli.py` — CLI smoke with `--dry-run`.

**Acceptance.**

- A user can run `atelier import-style-guide CONTRIBUTING.md` on a real project's
  contributing guide and have ≥ 5 reasonable lesson candidates appear in the inbox within
  60 seconds (with Ollama running locally).
- No candidate is auto-promoted; existing `lesson_decide` MCP tool / CLI is the gate.
- `--dry-run` produces a stable, deterministic-given-fixed-Ollama-temperature output.

**Boundary.** Internal Ollama only. Output goes through the V2/V3 lesson-review human gate.
No autonomous mutation of the ReasonBlock store.

**Effort.** 2-3 dev-days.

---

### 5.3 — Starter ReasonBlock packs (`atelier init --stack`)

**Why.** New users staring at an empty `./.knowledge/blocks/` directory don't know what a
ReasonBlock looks like for their stack. Templates remove the blank-page problem.

**What to build.**

- **Templates directory:** `templates/reasonblocks/<stack>/` — one folder per supported
  stack. Each contains 8-15 `.md` ReasonBlocks following the existing schema. Initial
  stacks (ship one to start, add more as feedback arrives):
  - `python-fastapi/` — Pydantic models, dependency injection, Alembic migrations,
    pytest layout, secret handling, structured logging, Ruff/Black config, request
    tracing.
  - `nextjs-typescript/` — server vs. client components, environment handling, API
    routes, Prisma migrations, error boundaries, accessibility checks.
  - `python-django/`, `rails/`, `go-stdlib/`, … — defer until requested.
- **CLI flag:** extend the existing `atelier init` command with `--stack STACK` (and
  `--list-stacks`). On `init --stack python-fastapi`, copy the matching templates to the
  user's `./.knowledge/blocks/` directory, prefixed with `template_` so the user can rename or
  delete without confusion.
- **Manifest:** each stack folder contains a `manifest.toml` with `name`, `description`,
  `version`, `blocks: list[&#123;file, title, summary&#125;]`. `--list-stacks` reads these
  manifests and prints a table.
- **Tests:**
  - `tests/gateway/test_init_with_stack.py` — `atelier init --stack python-fastapi`
    copies the expected files to the right place.
  - `tests/infra/test_template_blocks_valid.py` — every template in `templates/` parses as
    a valid ReasonBlock per the existing Pydantic model.

**Acceptance.**

- `atelier init --stack python-fastapi` produces a populated `./.knowledge/blocks/` in under a
  second.
- Every shipped template parses cleanly under V3's ReasonBlock validation.
- Templates are commented to make their adaptation obvious (TODO markers where users will
  want to customize for their company).

**Boundary.** Static templates. No LLM, no network call, no agent loop. Templates live in
the repo and version with releases.

**Effort.** 2-3 dev-days for the first stack (mostly content authoring, not code). Code
itself is < 200 LOC.

---

## 6. Implementation order (default)

```
Step 1 — atelier report         (5.1)   ~3 days   no new deps
Step 2 — atelier import-style-guide  (5.2)   ~3 days   uses existing internal_llm
Step 3 — starter ReasonBlock packs   (5.3)   ~3 days   first stack only; ongoing curation
```

Reasons for this order:

1. **Step 1 first** because it's deterministic, reuses existing data, ships a leader-
   facing artifact immediately, and validates the trace/memory stores have the right
   shape for reporting.
2. **Step 2 second** because it's the highest-leverage adoption tool — converts existing
   docs into agent-readable blocks. Depends on Step 1 only conceptually (tests showing
   imported candidates flow into the inbox).
3. **Step 3 last** because the _code_ is small but the _content_ (well-curated templates
   per stack) takes ongoing curation. Ship one stack to validate the mechanism; add stacks
   in response to demand, not on speculation.

Steps can also run in parallel if multiple agents are available — they touch different
modules. The only ordering constraint is that all three depend on V3 + V3.1 being shipped,
which they are.

---

## 7. What NOT to build (scope traps)

These ideas may surface during implementation. **Reject them.** Each one is a different
product Atelier deliberately doesn't build.

- ❌ **GitHub PR comment bot.** "Atelier auto-comments on PRs that fail rubrics" — that's
  GitHub Actions territory. The host CLI / a GitHub Action / `gh` CLI can call Atelier's
  rubric gate; Atelier does not need its own GitHub integration.
- ❌ **Slack bot.** Direct Slack-API integration. The `atelier report` Markdown output
  pastes cleanly into Slack via the user's existing tooling; we don't build a bot.
- ❌ **Web UI for ReasonBlock authoring.** Blocks are Markdown files in git. Authoring
  happens in the user's IDE. We do not build a web editor.
- ❌ **Real-time enforcement on every keystroke.** Atelier is per-task, not per-keystroke.
  Continuous-review tools like Cursor's tab-completion are a different product.
- ❌ **Auto-apply lesson candidates.** Even high-confidence Ollama-extracted candidates
  go through human review. The gate is non-negotiable.
- ❌ **Cross-team ReasonBlock marketplace.** "Buy/sell ReasonBlocks". Out of scope; users
  share via git like any other code.
- ❌ **Multi-tenant SaaS.** Atelier is local + self-hosted. The Letta integration is
  self-hosted via Docker. Adding a multi-tenant cloud is a different product.

If you are uncertain whether a request fits Atelier or is one of these traps, the rule is:
_if it requires Atelier to either (a) call a remote LLM on the user's hot path, (b) own
the user's agent loop, or (c) replace an existing developer-tool category (lint, CI, IDE,
wiki, GitHub features), it is out of scope._

---

## 8. Reply template for inbound interest

When someone reaches out (like Andrino's message), do not pitch. Reply with curiosity and
one open question. Template:

> Hi `<name>`, thanks for the kind words — that genuinely made my day.
>
> I'm based in `<location>`. Atelier is a side project that grew out of frustration with
> AI coding agents drifting from team conventions, so your story about `<their specific
pain point>` resonated. Would love to hear more about what practices you've been trying
> to get adopted at your company — I'm curious whether the shape of Atelier (procedural
> rules an AI agent reads + verifies on every task) maps to your problem or is solving
> something different.

What this template does, deliberately:

1. Acknowledges them without flattery.
2. Restates Atelier's positioning in one specific sentence (governance for AI-assisted
   coding, in plainer language).
3. Asks **one** open-ended question.
4. Does not pitch a product, a meeting, or a feature.

If their reply confirms the fit, the conversation continues. If their reply reveals
they're solving a different problem (a wiki, a linter, an IDE plugin), thank them for the
clarification — Atelier isn't the right fit and pretending otherwise wastes both
people's time.

---

## 9. Acceptance checklist for this whole document

After all three additions are implemented, the following should be true:

- [ ] A new user can run `atelier init --stack python-fastapi` and have a starter
      ReasonBlock library in their repo within seconds.
- [ ] A user with an existing `CONTRIBUTING.md` can run
      `atelier import-style-guide CONTRIBUTING.md` and have ≥ 5 review-ready lesson
      candidates in the inbox within a minute (with Ollama running).
- [ ] An engineering leader can run `atelier report --since 7d` and get a Slack-pasteable
      Markdown summary of adherence trends in under 3 seconds.
- [ ] None of the three additions introduces a new runtime dependency outside the existing
      `[smart]` extra.
- [ ] The boundary CI gate (`test_no_external_llm_clients.py`) still passes.
- [ ] No marketing percentage in any doc lacks a measurement footnote.
- [ ] Every PR for these changes records a `Trace` with the matching capability area.
- [ ] The "What NOT to build" list in § 7 is referenced in a CONTRIBUTING.md note for
      future contributors so the same scope creep doesn't keep getting proposed.

When that checklist is green, this document is `done`. It does not need to be promoted
into a V3.2 plan or formal packets unless the implementation reveals interlocking decisions
that warrant it; the additions are independent enough to ship without that overhead.
