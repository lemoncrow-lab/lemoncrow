"""OpenAI-compatible chat completions gateway.

Exposes ``POST /v1/chat/completions`` and ``GET /v1/models`` so any standard
TUI (OpenCode, Crush, Codex, Claude Code) can use LemonCrow as its execution
brain — routing, caching, subagents, and memory stay inside LemonCrow while
the TUI is just a view layer.

Usage::

    lc serve-openai [--port 8787]

Then configure your TUI to point at ``http://localhost:8787/v1``.
"""
