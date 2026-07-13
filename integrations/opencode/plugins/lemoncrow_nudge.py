"""OpenCode chat.message adapter for LemonCrow prompt-time nudges."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Stamp the host before any lemoncrow.core import runs. Unlike Codex (which sets
# CODEX_SESSION_ID for its hook subprocesses), OpenCode launches this helper
# (lemoncrow-nudge.js -> spawnSync) with no host-identifying env var at all, so
# lemoncrow.core.foundation.paths.detect_host() falls through every check and
# defaults to "claude". That silently misattributes this session's stats.json
# (tool/turn counts, ctx-notice state, idle-report dedup -- everything routed
# through plugin_runtime.session_stats_path) to sessions/.../claude/<sid>/
# instead of sessions/.../opencode/<sid>/, splitting it from the savings.jsonl
# the MCP server writes correctly (it's launched with `lemoncrow mcp --host
# opencode`, which sets this same env var for its own process). Set
# unconditionally: this process only ever handles OpenCode hook events.
os.environ["LEMONCROW_AGENT"] = "opencode"


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    return Path(root) if root else Path.home() / ".lemoncrow"


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        root = _lemoncrow_root()
        event = str(payload.pop("event", None) or "prompt")
        output: dict[str, object] = {"no_output": True}
        try:
            if event == "post_tool":
                from lemoncrow.core.capabilities.plugin_runtime import build_opencode_post_tool_use_output

                output = build_opencode_post_tool_use_output(root, payload)
            elif event == "idle":
                from lemoncrow.core.capabilities.plugin_runtime import build_opencode_stop_output

                output = build_opencode_stop_output(root, payload)
            else:
                from lemoncrow.core.capabilities.plugin_runtime import build_opencode_user_prompt_output

                output = build_opencode_user_prompt_output(root, payload)
        except (ImportError, KeyError, TypeError, ValueError, OSError):
            pass
        if output and not output.get("no_output"):
            sys.stdout.write(json.dumps(output) + "\n")
    except (ImportError, json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
