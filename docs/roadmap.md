# LemonCrow Roadmap

> Last revised 2026-06-15. Cadence: reviewed every 2 weeks.

This roadmap tracks shipped capabilities against what's in active development. Detailed execution specs are maintained internally.

## Shipped capabilities

### Model routing
- Per-turn model routing (`ModelRouter`) with session-phase awareness
- Cross-vendor routing — scores across available providers per turn
- Complexity scoring, cache-cost awareness, stickiness, success prediction
- Quality-aware routing with execution contracts

### Context & memory
- Dynamic context compaction with LLM hints (task type, risk level, must-keep)
- Sleeptime summarization and deduplication
- Persistent memory store (SQLite/PostgreSQL) with archival recall
- Cross-vendor memory adapters (Claude, Codex)
- Memory arbitration with staleness detection
- Symbol-based memory recall

### Cost tracking
- Per-session cost reports with actual vs counterfactual costs
- `lemon savings` and `lemon dashboard` commands
- Aggregate cost and token savings with reset support
- Counterfactual pricing engine

### Code intelligence
- Symbol-first code index with multi-language support
- AST pattern matching (ast-grep) with rewrite support
- Call graph (callers/callees) with centrality scoring
- Usages and reference resolution
- Cross-language edge resolution (ctypes, subprocess, dynamic import)
- Git history analysis (blame, graveyard, renames, walker)
- Zoekt backend for large repos
- Repo-map with PageRank

### CLI & service surface
- MCP server (local and remote modes)
- OpenAI-compatible `/v1/chat/completions` gateway
- Runtime commands: runs, ledger, swarm, lessons, benchmarks
- Outcome capture (feedback loop for routing decisions)
- `lemon insights` weekly summary with spend trends and opportunities
- Lesson promotion with PR bot
- Live reviewer agent
- Background services with auto-update

### Host integrations
- Claude Code, Codex, Copilot, OpenCode, Cursor, Antigravity, Hermes
- SDK adapters (Anthropic tools, OpenAI SDK hooks, LangChain middleware)

### Storage & telemetry
- SQLite and PostgreSQL storage backends
- pgvector for embedding similarity search
- OpenTelemetry → PostHog + GCP, local-first, anonymous

## Active development

| Area | Description |
|------|-------------|
| Optimization advisor | `lemon optimize` with compaction type taxonomy, golden tests, policy presets, shadow runner |
| Cross-machine sync | Encrypted workspace sync across machines |
| Web dashboard | Browser-based spend trends and management |
| Benchmark publication | One-command export to publishable JSON + markdown |

## Not planned

- Custom models, fine-tuning, or in-house embeddings
- IDE plugins
- Enterprise sales motion before Team tier is repeating
- Mobile companion
