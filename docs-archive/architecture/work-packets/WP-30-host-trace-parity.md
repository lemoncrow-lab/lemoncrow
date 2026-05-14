---
id: WP-30
title: Host trace parity and confidence levels
phase: G
pillar: proof
owner_agent: atelier:code
depends_on: [WP-13, WP-14, WP-15, WP-20, WP-29]
status: done
---

# WP-30 - Host Trace Parity

## Why

The cost/performance claim needs trace evidence that explains how a run worked. Claude Code has
hooks that can capture prompts, tools, bash, edits, compaction, and stop events. Other hosts expose
different surfaces. This packet defines and tests trace confidence per host instead of implying
perfect live-hook parity everywhere.

## Files touched

- **Edit** `docs/hosts/host-capability-matrix.md`
- **Edit** `src/atelier/gateway/adapters/mcp_server.py` if trace metadata needs host fields
- **Edit** `src/atelier/gateway/adapters/cli.py` if import/report commands need confidence output
- **Edit** `integrations/claude/plugin/hooks/*.py` only for trace metadata gaps
- **Edit** `integrations/codex/AGENT_README.md`
- **Edit** `integrations/copilot/AGENT_README.md`
- **Create** `tests/gateway/test_host_trace_confidence.py`
- **Create** `docs/engineering/trace-confidence.md`

## How to execute

1. Define trace confidence levels:
   - `full_live`: live hooks record prompt/tool/edit/command/stop events.
   - `mcp_live`: MCP calls and Atelier tool outputs are recorded, but native host edits/commands may
     require import or manual trace.
   - `wrapper_live`: wrapper captures task start/end and validations, but not every native event.
   - `imported`: host session is imported after the run.
   - `manual`: agent must call `trace` with observable facts.
2. Map each host to one or more confidence modes:
   - Claude Code: `full_live` when plugin hooks are enabled; `mcp_live` otherwise.
   - Codex: `mcp_live` plus `wrapper_live`; imported session parser where available.
   - Copilot: `mcp_live` plus VS Code task output; imported/manual for native chat edits.
   - opencode/Gemini: `mcp_live` plus host-specific import or command presets.
3. Add trace metadata fields to reports, not hidden state:
   - `host`
   - `trace_confidence`
   - `capture_sources`
   - `missing_surfaces`
4. Tests must assert that the final proof report cannot mark trace coverage as full unless the
   required host surfaces are present.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/gateway/test_host_trace_confidence.py -v
rg -n "trace_confidence|capture_sources|missing_surfaces" docs/hosts docs/engineering
make verify
```

## Definition of done

- [ ] Trace confidence levels are documented
- [ ] Every host has an explicit trace confidence and fallback path
- [ ] Proof reports expose capture sources and missing surfaces
- [ ] Tests prevent false `full_live` trace claims
- [ ] `INDEX.md` updated; trace recorded
