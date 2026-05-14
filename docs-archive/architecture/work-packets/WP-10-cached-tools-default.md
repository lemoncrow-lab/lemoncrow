---
id: WP-10
title: Promote `smart_read` / `cached_grep` to default-on
phase: C
pillar: 3
owner_agent: atelier:code
depends_on: []
status: done
---

# WP-10 — Cached tools default-on

## Why
The `tool_supervision` capability already ships `smart_read`, `smart_search`, and `cached_grep`,
but the host integrations only wire them in opt-in. We make them default. The largest single drop in
cache-read tokens comes from collapsing repeat reads — this is the lever that pays the rent.

## Implementation boundary

- **Host-native:** raw file reads, shell search, host `Read`, host `Grep`, and direct `rg` remain
  available and remain the right tool when the agent needs exact raw access.
- **Atelier augmentation:** `smart_read`, `smart_search`, and `cached_grep` reduce repeated context
  cost by returning cached, bounded, traceable results through MCP/CLI.
- **Not in scope:** do not replace the host's file tools, shell, or repository search behavior.

## Files touched

- `src/atelier/core/capabilities/tool_supervision/capability.py` — edit
- `integrations/claude/plugin/.claude-plugin/plugin.json` — edit
- `integrations/claude/plugin/hooks/post_tool_use_bash.py` — edit (extend cache to bash invocations)
- `integrations/codex/AGENTS.atelier.md` and `integrations/codex/tasks/preflight.md` — edit
- `integrations/copilot/AGENT_README.md` — edit
- `integrations/opencode/...` — edit (mirror the Codex change)
- `tests/infra/test_cached_grep_hit_rate.py`
- `docs/engineering/architecture.md` — edit (note default-on)

## How to execute

1. Read the existing capability to understand the cache key (it's content-hash + arg-hash). Verify
   that the cache TTL is sane (default 600s; reset on git HEAD change).

2. Bump the **default** for `cache_enabled` from `False` to `True` in
   `tool_supervision/capability.py`. Add a kill-switch env var `ATELIER_CACHE_DISABLED=1`.

3. Update each host's integration config so the cached MCP tools are listed in the agent's
   default tool allow-list. Keep the underlying `read`/`grep` tools available for cases where the
   agent needs raw access.

4. Test: simulate 20 reads of the same file with content-stable hash → assert cache_hit ≥ 19/20.
   Simulate the same 20 reads with one mutation in between → assert exactly 1 miss after the
   mutation.

5. Update the architecture doc to flip the wording from "optional" to "default-on, kill-switch
   `ATELIER_CACHE_DISABLED=1`."

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/infra/test_cached_grep_hit_rate.py -v

# Plugin sanity check (structural validation now runs inside install_claude.sh)
LOCAL=1 uv run scripts/verify_codex.sh

make verify
```

## Definition of done
- [ ] Cache default flipped on
- [ ] All host configs updated
- [ ] Host-native read/search tools remain available and documented as fallback
- [ ] Hit-rate test ≥ 95 %
- [ ] Kill-switch documented and respected
- [ ] `make verify` green
- [ ] `INDEX.md` updated; trace recorded
