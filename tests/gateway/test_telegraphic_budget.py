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

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.pro.capabilities.repo_map.budget import count_tokens

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Measured 2026-07 after the second trim pass: bash=448, code_search=218,
# edit=255, read=280, web_fetch=247 (core total 1448); SERVER_INSTRUCTIONS 196;
# personas 4089 tokens over 17 files. Ceilings = measured + ~5-10% headroom.
#
# SERVER_INSTRUCTIONS absorbs the TOOL-ROUTING half of the shared tool
# discipline (claude personas ship only the host-specific remainder against it
# — see _CLAUDE_TOOL_DISCIPLINE in scripts/sync_agent_context.py). Agent
# methodology (don't-thrash, batching) deliberately stays OUT — personas +
# runtime nudges own that.
PER_TOOL_CEILING = 450
CORE_SCHEMA_TOTAL_CEILING = 1_550
SERVER_INSTRUCTIONS_CEILING = 215
PERSONA_TOTAL_CEILING = 4_400

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
