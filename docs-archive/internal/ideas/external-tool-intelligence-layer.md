# Atelier External Tool Intelligence Layer — Future Design

**Audience:** future maintainer (likely future me) picking this up after Atelier has real adoption.
**Status:** future vision. **NOT** scheduled. **NOT** approved for build.
**Trigger condition:** ≥ ~100 developers actively using Atelier. Until then, this is wasted surface area.
**Author context:** captured 2026-05-07. Reviewed against current Atelier identity in `README.md` and existing capability set under `src/atelier/core/capabilities/`.

---

## 0. Why this document exists, and why it is on hold

The original prompt was to build an "External Tool Intelligence Layer" — discovery + scoring + sandboxed execution of MCP servers and Claude Skills, with curated catalogs and a security proxy. The intent is sound: as the MCP / Skills ecosystem grows, an Atelier user will eventually ask *"which of these tools are safe and useful, and how do I wire them in?"* Atelier is well-positioned to answer that, because it already gates plans, audits tool calls, and verifies outputs.

It is **not** the right thing to build right now, for three reasons:

1. **No user demand signal.** Without ~100 devs we have no evidence that "external tool curation" is a top-five problem for them. Building a 9-module subsystem against a hypothesis is how runtimes accumulate dead weight.
2. **Maintenance trap risk.** Source adapters for MCPMarket / Glama / mcp.so / Awesome MCP Servers will silently rot. Each one is a perpetual liability against an unstable contract.
3. **Identity drift risk.** Parts of the original plan (Phase 4 — MCP proxy / supervisor that runs external servers) contradict Atelier's stated position: *"Not an agent framework — Atelier does not execute tools, manage model calls, or own the agent loop."* That line is load-bearing for how Atelier explains itself. We should not blur it without an explicit decision.

The doc below preserves the full vision so a future maintainer doesn't have to re-derive it. It also bakes in the design constraints that the original prompt under-specified, so the next attempt starts from a sharper position.

---

## 1. Goal

Make Atelier the place where an engineer goes when they want to:

1. **Find** a trustworthy external MCP server or Claude/Agent Skill for a need ("I want to ground generation against current docs", "I want UI validation in CI").
2. **Vet** it against a published, deterministic policy (security, maintenance, host compatibility).
3. **Wire it in** to one of their agent hosts (Claude Code, Codex, opencode, Copilot, Gemini CLI) with the right permission shape.
4. **Audit** what it did, the same way they audit Atelier-supervised tool calls today.

What we are **not** building:

- A marketplace UI.
- A marketplace clone.
- An auto-installer.
- A runtime that executes third-party MCP servers in-process.

---

## 2. Where this fits in Atelier's identity

Atelier today sits between agent hosts and their environments. It supervises, gates, verifies, and remembers — but it does not *run* the agent's tools. Adding external-tool intelligence must respect that contract.

The cleanest version of this feature is:

> Atelier is a **policy and audit plane** for external tools. It catalogs them, scores them, emits host-side configuration, and ingests audit data from host traces. It does not stand up its own MCP proxy.

This framing is the most important design constraint in this document. Every other choice flows from it.

---

## 3. Design constraints (lessons from the v0 review)

These are non-negotiable for any future attempt.

### 3.1 Sidecar identity preserved
Atelier never runs an external MCP server in-process. Atelier emits configuration that the host runs, and ingests traces back. If we ever want to run external servers ourselves, that is a separate product decision and a separate doc.

### 3.2 Curated YAML is the source of truth
The catalog ships hand-curated YAML in-repo. Live source adapters (MCPMarket, Glama, mcp.so) are **optional, manual, offline-capable** importers — never required, never auto-refreshed in a cron, never on the test critical path. They produce diff-able PRs into the curated YAML, reviewed by a human.

### 3.3 Categorical recommendations, not float scores
The output of the recommender is one of `{recommended, candidate, blocked}` plus a structured reason list. No `atelier_fit_score: 0.83`. This matches the existing rubric / lint pattern (`ok / warn / blocked`) and avoids inventing a number that nothing else in Atelier consumes.

### 3.4 Skills do not auto-promote into ReasonBlocks
ReasonBlocks encode domain judgment ("don't parse handle from URL"). Skills are reusable capability bundles. They are different shapes. Imported skills land in a quarantined namespace and surface as candidates in the existing `lesson inbox` flow. They never silently become ReasonBlocks.

### 3.5 Capability-first, not catalog-first
The win is making existing Atelier capabilities better. The catalog is plumbing. Pick a capability + tool pair, prove the adapter contract, then backfill catalog and scoring around real demand. Do not build a catalog and hope integrations follow.

### 3.6 Coexist with `integrations/skills/`
That directory already ships Atelier-as-a-skill into Claude. The new work lives alongside it, not in a parallel `integrations/external_tools/`. Reconcile the directory layout before writing code.

### 3.7 Honest scope budget
The full vision below is realistically 3–5 weeks of careful work, including `mypy --strict` work. Not a single sprint. If a future planner sizes this as one task, that is the first sign the plan has lost contact with reality.

---

## 4. The full vision (10 phases)

The phases below are the original ambition, adjusted for the constraints in §3.

### Phase 1 — Tool Catalog Model

Add a typed model and on-disk layout for the curated catalog.

```
src/atelier/external_tools/
  __init__.py
  models.py               # ExternalTool, ExternalSkill, Permission, Capability enums
  catalog.py              # load/validate curated YAML, in-memory index
  sources.py              # source adapters (manual import only, see Phase 2)
  scorer.py               # categorical recommender
  security.py             # policy types, permission classification
  recommendations.py      # `recommend` query layer
  adapters.py             # ExternalCapabilityAdapter contract (Phase 6)
  audit.py                # audit-log ingest from host traces (Phase 4-bis)

catalog/external_tools/
  curated.yaml            # the source of truth
  blocked.yaml            # explicit denylist, with reasons
  sources.yaml            # registered upstream sources, with snapshot dates
  mappings.yaml           # capability → tool-id mapping for Phase 6
```

`ExternalTool` fields (preserved from original spec, with notes):

- `id` (slug, stable, never reused)
- `name`
- `source` (one of `curated | mcpmarket | glama | mcp_so | awesome_mcp | anthropic_skills`)
- `homepage_url`, `repository_url`, `marketplace_url`
- `category`
- `description`
- `license`
- `install_method`
- `transport`: `stdio | http | sse | streamable_http | cli | skill`
- `host_support`: dict of `claude | codex | opencode | copilot | gemini → bool`
- `capabilities`: subset of `{docs, browser, repo, code_search, sql, testing, reasoning, memory, design, observability}`
- `permissions`: subset of `{read_files, write_files, shell, network, browser, database, secrets}`
- `security_risk`: `low | medium | high | blocked`
- `maintenance_signal`: structured (last_release, open_issue_age, archived_flag) — **not** a single score
- `popularity_signal`: structured (stars, weekly downloads, source-specific) — **not** a single score
- `recommendation`: `recommended | candidate | blocked` (computed by scorer)
- `recommendation_reasons`: list of structured reason codes
- `notes`
- `status`: `approved | candidate | blocked | deprecated` (manual lifecycle)

Acceptance:
- Curated YAML loads offline.
- Malformed entries are rejected with a clear error.
- Schema migrations are versioned.

### Phase 2 — Discovery sources (manual, offline-capable)

Source adapters exist to **populate** curated YAML, not to power recommendations at runtime. Each adapter:

- Runs on demand via `atelier external source refresh --source <name>`.
- Outputs a diff against `curated.yaml` for human review.
- Stores raw snapshots under `catalog/external_tools/snapshots/<source>/<date>/` so we can replay or audit without re-fetching.
- Has a clear "last successful refresh" timestamp surfaced in `atelier external source list`.

Sources to support, in priority order:

1. Anthropic Skills repo (most stable, most aligned).
2. Local curated YAML (always works, no network).
3. Awesome MCP Servers (markdown parsing, low effort).
4. mcp.so, Glama, MCPMarket (best-effort, may break, that's fine).

Default tests do **not** hit the network. Each adapter ships with a fixture-based test using a captured snapshot.

CLI:

```
atelier external source list
atelier external source refresh --source <name>     # human-in-the-loop, produces a diff
atelier external tool list
atelier external tool show <id>
atelier external tool search <query>
```

Acceptance:
- `atelier external tool list` works with zero network access.
- A broken upstream source does not break the CLI.
- Snapshots are diff-able and committable.

### Phase 3 — Categorical recommender

The recommender is a deterministic function from `(query, host, mode)` to a sorted list of `(tool, recommendation, reasons)`.

It does **not** return a float fit score. It returns one of:

- `recommended` — passes all hard filters, matches capability, host-compatible, low/medium risk, maintained.
- `candidate` — matches capability but at least one signal is yellow (medium risk, stale maintenance, partial host support).
- `blocked` — fails any hard filter, or appears in `blocked.yaml`.

Hard filters (any failure → `blocked`):
- License is incompatible.
- Permissions include `shell` or `secrets` and the tool is not on the explicit shell/secrets allowlist.
- Provenance is unknown (no repository, no maintainer, no signed release).
- Tool executes remote code as a normal mode of operation.

Soft signals (any one yellow → `candidate`):
- No release in N months.
- Open issues skewed toward unanswered.
- Single-maintainer with no fallback.
- Host compatibility partial.

CLI:

```
atelier external recommend --capability docs
atelier external recommend --capability browser
atelier external recommend --host claude
atelier external recommend --mode safe
atelier external recommend --include-blocked        # opt-in, shows reasons
```

Acceptance:
- Recommendations are deterministic across runs given the same catalog.
- Blocked tools never appear unless `--include-blocked`.
- Every result lists its reason codes.

### Phase 4 — Policy emission and audit (replaces "MCP proxy")

This is the phase that diverged most from the original prompt. Restating the constraint: **Atelier does not run external MCP servers.** Instead, it does two things.

**4a. Policy emission.** Atelier produces host-specific configuration snippets the user can drop into their agent host. For example:
- A `claude_desktop_config.json` fragment for Claude Code.
- An `opencode.jsonc` snippet for opencode.
- A Codex MCP entry.

Each emitted snippet carries the security policy as comments / metadata: timeout, env allowlist, cwd restriction, network policy, read-only mode flag. The host still runs the server. Atelier tells the user *how* to run it safely and refuses to emit a snippet for a `blocked` tool.

**4b. Audit ingest.** When the host emits traces back into Atelier (via the existing trace path), tool calls hitting external servers are tagged and surfaced in a new `atelier external audit` view. This reuses the existing trace store. No new daemon, no new proxy.

Modes:
- `catalog-only` — the tool exists in the catalog but no config is emitted.
- `dry-run` — Atelier prints the snippet but does not write it anywhere.
- `emit` — Atelier writes the snippet to the host config path (with backup).
- `blocked` — terminal; no emission possible.

CLI:

```
atelier external install <tool-id> --host claude --dry-run
atelier external install <tool-id> --host claude --mode emit
atelier external uninstall <tool-id> --host claude
atelier external audit                                # tail of external tool calls
atelier external audit --tool <id>
```

Acceptance:
- A `blocked` tool cannot be emitted.
- Emitting always writes a backup of the host config first.
- Audit view reflects observed tool calls from existing trace ingest.
- No new long-running process is introduced.

**If the future maintainer concludes Atelier should genuinely supervise external MCP servers** (not just emit policy), that is a separate, larger product decision and needs its own design doc — including how it reconciles with the "not an agent framework" line in the README.

### Phase 5 — Curated tool set

Initial seed of `curated.yaml`. Aim for ~10 entries at first ship, not 50. Each entry is hand-vetted by reading the source repo, not by trusting the marketplace listing.

Mapping by capability:

1. **Documentation grounding** — a docs lookup tool. Used by `reasoning_reuse`, `semantic_file_memory`, SDK correctness checks.
2. **Browser / UI validation** — a Playwright-style MCP. Used by `rubric_verification` for frontend rubrics, dashboard validation.
3. **Repo / code context** — repo browsing tools. Used by `semantic_file_memory`, OSS comparison.
4. **Structured reasoning helper** — sequential-thinking style tools. Used by `reasoning_reuse` and `failure_analysis`.
5. **Test / code-quality helper** — local lint/test MCPs. Used by `tool_supervision`, `rescue`, CI diagnostics.

Candidate-only tier (in catalog, not recommended by default):
- Database / schema introspection.
- Observability / log query.
- Design / Figma readers.
- GitHub PR review tools.

Blocked tier (with explicit reasons in `blocked.yaml`):
- Stealth browser / scraping tools.
- Arbitrary shell execution tools.
- Tools requiring broad secret tokens (admin-scoped GH PATs, full GCP keys, etc.).
- Tools with unknown provenance.
- Tools that execute remote code as a feature.

No vendor names appear in product UI. The UI says "documentation grounding tool" and lets the user click through to the catalog entry for specifics.

### Phase 6 — Capability integrations (the actual point)

Define `ExternalCapabilityAdapter`:

```python
class ExternalCapabilityAdapter(Protocol):
    def prepare_context(self, request: CapabilityRequest) -> AdapterContext: ...
    def call_tool(self, ctx: AdapterContext) -> RawToolResult: ...
    def summarize_result(self, raw: RawToolResult) -> CompressedResult: ...
    def estimate_savings(self, raw: RawToolResult, summary: CompressedResult) -> Savings: ...
    def record_audit(self, ctx: AdapterContext, raw: RawToolResult) -> None: ...
    def apply_security_policy(self, ctx: AdapterContext) -> PolicyDecision: ...
```

Capability mappings:

- `reasoning_reuse` ← docs lookup, prior trace lookup, structured reasoning helper.
- `semantic_file_memory` ← repo / code context tools, docs tools, symbol summaries.
- `loop_detection` ← test / CI diagnostic tools, repeated command history.
- `tool_supervision` ← tool-call audit ingest, cached fetch / search / read, dedup.
- `context_compression` ← summarize external tool outputs before agent injection.
- `rubric_verification` ← browser validation, docs validation, CI/test validation.

Operating contract:
- Every adapter call passes through `apply_security_policy` first.
- Every result is summarized before injection — raw output never reaches the agent context.
- Every call lands in the audit log and contributes to the savings ledger.
- Mock adapters exist for tests; live adapters are opt-in.

Acceptance:
- A docs-lookup adapter can run end-to-end in dry-run / mock.
- A browser-validation adapter can run end-to-end in dry-run / mock.
- Compression happens on the Atelier side before the host sees the result.
- All calls are traceable through the existing trace store.

### Phase 7 — Skill importer

Build a safe importer for Claude / Agent Skills folders.

CLI:

```
atelier skill import <path>
atelier skill validate <path>
atelier skill convert-to-procedure <path>      # candidate, never auto-promotes
atelier skill list
```

Rules:
- Imported skills are validated against a schema (frontmatter, allowed file types, max size).
- Scripts inside skills are **not** executed during import.
- Resources are copied into a quarantined namespace `~/.atelier/skills/imported/<id>/`.
- Tool instructions are stored verbatim and surfaced for human review.
- Hidden / chain-of-thought-style prompts are rejected.
- `convert-to-procedure` produces a *draft ReasonBlock* that lands in the existing `lesson inbox`. A human still has to promote it. This is a hard line: no silent promotion.

Acceptance:
- An Anthropic-style skill folder validates.
- A skill can be converted to a candidate ReasonBlock that requires human approval.
- Scripts and resources are flagged for review with file paths and hashes.

### Phase 8 — Documentation

User-facing docs under `docs/external-tools/`:

- `overview.md` — what this is, what it isn't.
- `security.md` — the policy model, the blocked categories, the audit surface.
- `curated-tools.md` — the current curated list, by capability.
- `marketplaces.md` — how source imports work, why they are manual.
- `claude-skills.md` — skill import flow.
- `integration-policy.md` — how Atelier decides what is `recommended | candidate | blocked`.

README addition (one paragraph):
> External tools are optional and supervised. Atelier can discover and audit external MCP servers and Agent Skills, but does not run unknown tools by default and does not require any external tool to function.

No competitor names in product UI. No marketplace claims that aren't tested.

### Phase 9 — Tests

```
tests/test_external_tool_catalog.py
tests/test_external_tool_scoring.py
tests/test_external_tool_security.py
tests/test_external_tool_policy_emission.py        # replaces test_external_tool_proxy
tests/test_skill_importer.py
```

Coverage:
- Curated YAML loads. Malformed YAML rejected.
- Blocked tool cannot be emitted into a host config.
- Approved tool dry-run emits the expected snippet.
- External call audit ingest works against fixture traces.
- Shell-permission tool blocked unless on allowlist.
- Secrets-permission tool blocked unless on allowlist.
- High-risk tool hidden by default; appears only with `--include-blocked`.
- Recommendations deterministic across runs.
- Skill folder validates.
- Skill converts to a candidate ReasonBlock that lands in the inbox, not the live store.
- No network required for the default test suite.

### Phase 10 — Final gates

Standard Atelier gates:

```
uv run ruff check src tests
uv run black --check src tests
uv run mypy --strict src tests
uv run pytest -q
```

Final report shape (for whichever PR ships this):
1. Files changed.
2. External tool sources added.
3. Curated tools added.
4. Blocked tools added.
5. Security policies implemented.
6. Tests added.
7. Commands run.
8. Gate results.
9. Limitations and known gaps.

---

## 5. Recommended thin-slice MVP (when build is greenlit)

When the trigger condition is hit and we decide to build this, do **not** start at Phase 1 and march to Phase 10. Start with the smallest slice that proves the capability-uplift story:

1. **Phase 1 model** — minimum subset of fields needed for one tool.
2. **Phase 5 curated YAML** — exactly one tool, hand-picked for one capability.
3. **Phase 6 adapter contract** — implemented for *one* capability + that one tool, end-to-end mockable.
4. **Phase 7 skill validator** — validate-only, no auto-conversion.
5. **Phase 8 docs** — `overview.md` + `security.md` + `curated-tools.md` only.
6. **Phase 9 tests** — only for the above.

Defer until that slice is in real users' hands:
- Live source adapters (Phase 2 beyond curated YAML).
- Recommendation engine (Phase 3 beyond a stub).
- Policy emission / audit ingest (Phase 4) — until capability uplift proves valuable.
- Candidate-tier categories (Phase 5 beyond the seed).
- mypy --strict on the whole new tree (allow `# type: ignore` islands at first).

If the thin slice does not produce measurable savings or measurable user pull within a release cycle, **stop**. The catalog ambition was wrong for this product at this moment. That is a valid outcome.

---

## 6. Open questions to revisit at trigger time

1. Is "external tool intelligence" still a top-five user request, or has the ecosystem consolidated such that hosts ship their own curation? (If hosts solve it, Atelier shouldn't.)
2. Has the MCP spec stabilized enough that capability classification is mechanical, or is it still bespoke per server?
3. Have we shipped enough host integrations that emitting per-host config is straightforward, or is each host still an integration project?
4. Do we have telemetry showing that users *want* a recommender, vs. preferring to bring their own list?
5. Has Anthropic shipped first-party skill curation that would make our skill importer redundant?
6. Has any of the current marketplaces won decisively, such that we only need one source adapter, not five?

If most answers point toward "host or vendor solves it," shelve this permanently and link the doc as historical context.

---

## 7. Hard rules carried over from the original prompt

These remain non-negotiable whenever this work resumes:

- No third-party MCP server installed or run by default.
- No marketplace hype in product copy.
- No competitor names in product-facing UI.
- No external tool required for Atelier core to function.
- No secrets forwarded by default.
- No shell access by default.
- No network exfiltration paths added without explicit policy.
- Audit log records every external tool invocation Atelier observes.
- Blocked tools cannot be emitted, period.

---

## 8. Pointers for the future maintainer

- Read `README.md` first. If it still says "Not an agent framework — Atelier does not execute tools, manage model calls, or own the agent loop," §3.1 of this doc is still the binding constraint.
- Look at `src/atelier/core/capabilities/tool_supervision/` — that is the closest existing pattern. The external-tool layer should feel like a sibling, not a stranger.
- Look at `integrations/skills/` — that already exists. Reconcile before creating new directories.
- Look at `docs/internal/engineering/telemetry-implementation-plan.md` for the doc style and the bar for "approved design."
- This doc is **not** that bar. It is a parking-lot vision. Promote it explicitly when the trigger fires.
