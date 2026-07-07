"""Telegraphic-budget regression gate.

Every LLM-facing instruction string ships on every session (tool schemas on
every request); the telegraphic rewrite cut them ~30-45%. These ceilings stop
the next edit from silently bloating them back. A trip here means: compress
the text (drop filler, keep contracts) -- do not raise the ceiling without a
deliberate decision.

See docs/architecture.md "Telegraphic instruction surface".
"""

from __future__ import annotations

import json
from pathlib import Path

from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.gateway.adapters import mcp_server

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Measured 2026-07 after the telegraphic rewrite: bash=387, code_search=246,
# edit=294, read=296, web_fetch=284 (core total 1507); personas 4919 tokens
# over 16 files. Ceilings = measured + ~10% headroom.
# SERVER_INSTRUCTIONS measured 235 after absorbing the TOOL-ROUTING half of
# the shared tool discipline (claude personas ship only the host-specific
# remainder against it — see _CLAUDE_TOOL_DISCIPLINE in
# scripts/sync_agent_context.py). Agent methodology (don't-thrash, batching)
# deliberately stays OUT — personas + runtime nudges own that.
PER_TOOL_CEILING = 450
CORE_SCHEMA_TOTAL_CEILING = 1_700
SERVER_INSTRUCTIONS_CEILING = 260
PERSONA_TOTAL_CEILING = 5_500

# The always-advertised 5-tool surface (other tools are hidden or
# conditionally visible, e.g. `search` behind an embedding backend).
_CORE_TOOLS = frozenset({"bash", "code_search", "edit", "read", "web_fetch"})


def _visible_tool_tokens() -> dict[str, int]:
    out: dict[str, int] = {}
    for name, spec in sorted(mcp_server.TOOLS.items()):
        if not mcp_server._tool_visible_to_llm(name, spec):
            continue
        desc = mcp_server._tool_description(spec)
        schema = json.dumps(spec.get("inputSchema") or {}, sort_keys=True)
        out[name] = count_tokens(desc) + count_tokens(schema)
    return out


def test_each_visible_tool_schema_stays_telegraphic() -> None:
    over = {name: tokens for name, tokens in _visible_tool_tokens().items() if tokens > PER_TOOL_CEILING}
    assert not over, (
        f"tool schema(s) over the {PER_TOOL_CEILING}-token telegraphic ceiling: {over}. "
        "Compress the description (drop filler, keep contracts) instead of raising the ceiling."
    )


def test_core_schema_total_stays_telegraphic() -> None:
    tokens = _visible_tool_tokens()
    missing = _CORE_TOOLS - tokens.keys()
    assert not missing, f"core tools missing from the advertised surface: {sorted(missing)}"
    total = sum(tokens[name] for name in _CORE_TOOLS)
    assert total <= CORE_SCHEMA_TOTAL_CEILING, (
        f"core 5-tool schema total {total} tokens > ceiling {CORE_SCHEMA_TOTAL_CEILING}. "
        "This text ships on EVERY request -- compress it, don't grow it."
    )


def test_server_instructions_stay_telegraphic() -> None:
    tokens = count_tokens(mcp_server.SERVER_INSTRUCTIONS)
    assert tokens <= SERVER_INSTRUCTIONS_CEILING, (
        f"SERVER_INSTRUCTIONS is {tokens} tokens > ceiling {SERVER_INSTRUCTIONS_CEILING}. "
        "It rides in every session's system prompt -- keep it telegraphic."
    )


def test_personas_stay_telegraphic() -> None:
    files = sorted((_REPO_ROOT / "integrations" / "agents").rglob("*.md"))
    assert files, "persona sources missing under integrations/agents"
    total = sum(count_tokens(path.read_text(encoding="utf-8")) for path in files)
    assert total <= PERSONA_TOTAL_CEILING, (
        f"persona sources total {total} tokens > ceiling {PERSONA_TOTAL_CEILING} "
        f"across {len(files)} files. Compress the prose, keep the contracts."
    )
