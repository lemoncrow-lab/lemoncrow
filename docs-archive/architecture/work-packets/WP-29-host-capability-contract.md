---
id: WP-29
title: Host capability and enforcement contract
phase: G
pillar: proof
owner_agent: atelier:code
depends_on: [WP-20, WP-25, WP-26, WP-27, WP-28]
status: done
---

# WP-29 - Host Capability Contract

## Why

Atelier must work on top of each host's native extension surface instead of pretending every host
shares one uniform plugin model. This packet turns the host matrix into a contract that states exactly
what Atelier can enforce in Claude Code, Codex, Copilot, opencode, and Gemini.

## Implementation boundary

- **Host-native:** extension surfaces, model execution, edit UX, compaction behavior, and agent/task
  orchestration remain facts supplied by each host.
- **Atelier augmentation:** the contract records what Atelier can use, enforce, trace, and fall back
  to for each host.
- **Future-only:** unsupported enforcement capabilities must be represented as disabled contract
  fields, not silently implemented in this packet.

## Files touched

- **Edit** `docs/hosts/host-capability-matrix.md`
- **Edit** `docs/integrations/host-matrix.md`
- **Edit** `README.md` host integration section if it still says every host is a plugin
- **Create** `tests/gateway/test_host_capability_contract_docs.py`

## How to execute

1. Define enforcement levels in the host matrix:
   - `advisory`: Atelier returns guidance; the host/user chooses whether to follow it.
   - `hook_enforced`: host events can block, warn, or inject required context.
   - `wrapper_enforced`: Atelier wrapper gates task start, model choice, or completion.
   - `provider_enforced`: Atelier owns the model call path. This is future-only unless a packet
     implements provider execution.
2. For each host, record:
   - native surfaces used: MCP, instructions, skills/agents, hooks, tasks, chat modes, wrappers;
   - routing enforcement level;
   - trace coverage level;
   - unsupported controls;
   - fallback behavior when the host cannot enforce a decision.
3. Replace broad claims like "plugin works everywhere" with "same runtime, host-native integration
   per CLI."
4. Add boundary labels for overlapping capabilities: `Host-native`, `Atelier augmentation`, and
   `Future-only`.
5. Add a docs test that fails if the matrix omits any supported host or any required contract
   column.

## Acceptance tests

```bash
cd /home/pankaj/Projects/leanchain/atelier
LOCAL=1 uv run pytest tests/gateway/test_host_capability_contract_docs.py -v
rg -n "plugin works everywhere|same plugin" README.md docs/hosts docs/integrations integrations && exit 1 || true
make verify
```

## Definition of done

- [ ] Host matrix states enforcement level, trace coverage, unsupported controls, and fallback
- [ ] README/docs avoid claiming identical plugin behavior across hosts
- [ ] Unsupported host controls are represented as disabled/fallback, not reimplemented
- [ ] Docs test covers all supported hosts
- [ ] Host verify scripts still pass
- [ ] `INDEX.md` updated; trace recorded
