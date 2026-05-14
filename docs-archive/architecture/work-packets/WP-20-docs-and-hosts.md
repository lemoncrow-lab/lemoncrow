---
id: WP-20
title: Update host integration docs + skill files for the new tools
phase: E
pillar: 1, 2, 3
owner_agent: atelier:code
depends_on:
  [
    WP-07,
    WP-08,
    WP-13,
    WP-15,
    WP-17,
    WP-18,
    WP-19,
    WP-21,
    WP-22,
    WP-23,
    WP-24,
    WP-25,
    WP-26,
    WP-27,
    WP-28,
  ]
status: done
---

# WP-20 — Docs and hosts roll-out

## Why

The V2 capabilities only matter if every supported host knows how to invoke them. This packet
updates the per-host install/skill docs, the AGENT_README chain, the docs-site sidebar, the
QUICK_REFERENCE card, and the README.

## Implementation boundary

- **Host-native:** each host keeps its own install surface: Claude plugin/hooks, Codex MCP plus
  AGENTS/tasks/wrappers, Copilot MCP/chatmode/manual trace, and similar native surfaces for Gemini
  and opencode.
- **Atelier augmentation:** docs explain the same runtime concepts through each host's native
  integration path.
- **Not in scope:** do not claim a single identical plugin model across all hosts, and do not ask
  host integrations to duplicate features already built into the host CLI.

## Files touched

- `README.md` — edit (capability table; benchmarks section)
- `AGENT_README.md` — edit (extend the MCP-tools JSON to list the new tools)
- `QUICK_REFERENCE.md` — edit
- `docs/README.md` — edit (link the V2 plan)
- `docs-site/sidebars.ts` — edit (register the new architecture entries)
- `docs/hosts/claude-code-install.md` — edit (memory tools, compact lifecycle)
- `docs/hosts/codex-install.md` — edit
- `docs/hosts/copilot-install.md` — edit
- `docs/hosts/gemini-cli-install.md` — edit
- `docs/hosts/opencode-install.md` — edit
- `integrations/claude/ATELIER_SKILL.md` — edit (document `memory_*`, `search`, etc.)
- `integrations/claude/AGENTS.atelier.md` — edit
- `integrations/codex/AGENT_README.md` — edit
- `integrations/copilot/AGENT_README.md` — edit
- `integrations/codex/tasks/preflight.md` — edit (add memory + search_read to the preflight tool list)

## How to execute

1. For every host, list every new MCP tool from the data-model § 8 table with one-line description
   and a tiny example invocation.
2. In `AGENT_README.md`, the "EXTENDED TOOLS" JSON block gains every new entry from § 8.
3. Sidebar gets a new "Architecture / V2" category with `IMPLEMENTATION_PLAN_V2`,
   `IMPLEMENTATION_PLAN_V2_DATA_MODEL`, and a sub-category for `work-packets/INDEX`.
4. README capability table gains rows for memory, archival recall, lesson promotion, search_read,
   batch_edit.
5. For every overlapping feature, label the boundary as `Host-native`, `Atelier augmentation`, or
   `Future-only`.
6. Run every host's verify script and ensure they all still pass.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
make verify  # all installed hosts must verify

# Sidebar registers the new docs (Docusaurus build-check via dry-run)
cd docs-site && npx docusaurus build --no-minify --out-dir /tmp/docs-build && cd ..
test -d /tmp/docs-build/architecture
```

## Definition of done

- [ ] Every host integration doc updated and verified
- [ ] AGENT_README EXTENDED TOOLS block lists every new MCP tool
- [ ] Sidebar updated, docs site builds clean
- [ ] README and QUICK_REFERENCE reflect V2
- [ ] Host docs distinguish host-native behavior from Atelier augmentation
- [ ] `INDEX.md` updated; trace recorded
