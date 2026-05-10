# Atelier Dashboard Pages

Dashboard pages displaying system features and monitoring data.

## Pages

### Overview.tsx

Main dashboard entry point. Displays system statistics (uptime, total traces, avg cost per call, calls per hour) and summary cards for 4 core features (ReasonBlocks, Traces, Rubrics, Savings). Simplified to avoid duplication - detailed feature info is in respective tab pages.

**Entry points:** `coreFeatures` array filters FEATURES for ids: reasonblocks, traces, rubrics, savings.

### Blocks.tsx

ReasonBlocks feature page. Displays domain-indexed reusable reasoning procedures with full-text search, status filtering (active/retired/deprecated), and domain filtering. Feature info section at top explains what blocks are, why they're used (7%+ token savings), and key benefits.

**API:** `api.blocks()` — fetches blocks array.

### Traces.tsx

Execution traces feature page. Shows detailed logs of what agents did (files touched, commands run, tools called, errors). Includes status/domain/host filtering and pagination.

**API:** `api.traces()`, `api.trace(id)`.

### Memory.tsx

Memory feature page. Two-column layout for core memory blocks (pinned + recent) and archival recall search. Supports optimistic-lock updates to blocks with conflict handling on 409 responses.

**API:** `api.memoryBlocks()`, `api.memoryUpsertBlock()`, `api.memoryRecall()`, `api.traces()`.

### Rubrics.tsx

Rubric Gates feature page. Domain-specific verification rubrics (YAML-defined quality gates for pre/post-execution checks). Shows required checks and usage examples.

**API:** `api.rubrics()`.

### Savings.tsx

Cost & Token Savings tracking page. Aggregate metrics (Operations Tracked, Total Calls, Would Have Cost, Saved) and per-call breakdown.

**API:** `api.savings()`, `api.calls()`.

### Failures.tsx

Failure Analyzer page. Shows failure clusters and error analysis. Conditional rendering: empty state if no failures, otherwise displays cluster list with expansion.

**API:** `api.failures()`.

### Gateway.tsx

MCP Server & Host Adapters page. Displays agent definitions (AgentCard), skill definitions (SkillCard), and host configurations (HostCard) across three tabs (agents/skills/hosts).

**API:** `api.hosts()`, `api.skills()`.

### Agents.tsx

Agent Definitions and monitoring page. Shows agent definitions, monitor alerts, and standing loop explanation. Data sourced from local AGENTS array (not API-fetched).

**Data source:** Local `AGENTS` array constant.

### Tools.tsx

MCP Tools & Capabilities page. Displays the minimal Core-4 MCP tools grouped by namespace (brain/capture).

**API:** `api.mcp_status()`.

### Plans.tsx

Plan Validation page. Shows validation results with pass/fail checks. Conditional: empty state or plan results with expansion.

**API:** `api.plans()`.

## Design Pattern: Feature Info Sections

All pages (except Overview) display a consistent feature info section at the top:

- Wrapper: `<section className="border border-neutral-800 bg-neutral-900/50 p-5">`
- Icon: text-3xl emoji
- Title + Status badge (emerald-900/30 background, text-emerald-300)
- Subtitle: monospace, 11px, text-neutral-500
- Description: neutral-300, leading-relaxed
- 3 Benefits: text-emerald-300/90, space-y-1, with ✓ checkmarks

Implemented identically across all pages for consistency.

## Related

- `api.ts` — API client for all endpoints
- `components/` — Reusable React components (StatCard, etc.)
- `components/MemoryBlockCard.tsx` — Inline memory block display + edit trigger
- `components/ArchivalSearchBox.tsx` — Query box for archival recall
- `components/RunInspectorDrawer.tsx` — Right-side drawer for run memory context
- `App.tsx` — Route definitions
