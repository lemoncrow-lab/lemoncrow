"""OpenCode chat.message adapter for LemonCrow prompt-time nudges."""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    return Path(root) if root else Path.home() / ".lemoncrow"


def _stale_nudge_message(root: Path) -> str | None:
    """At most one stale-optional-agent nudge per day (per calendar day).

    OpenCode has no skills concept (see agents_skills._skill_dir), so only
    installed agent roles are checked here. Reuses the exact same
    stale_optional_items/format_stale_nudge the Claude statusline tip and
    `lemon stale-nudge` CLI use -- one shared threshold/cost calculation
    for every host. Fail-open: any error means no nudge, never a crash.
    """
    try:
        from lemoncrow.gateway.cli.commands.agents_skills import (
            format_stale_nudge,
            stale_optional_items,
        )

        items = stale_optional_items("opencode", None, root=root)
        if not items:
            return None
        today = datetime.date.today().isoformat()
        marker = root / "opencode_stale_nudge_shown" / "last_shown"
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == today:
            return None
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(today, encoding="utf-8")
        return format_stale_nudge(items[0])
    except (ImportError, OSError, KeyError, TypeError, ValueError):
        pass
    return None


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        root = _lemoncrow_root()
        event = str(payload.pop("event", None) or "prompt")
        stale_message = _stale_nudge_message(root) if event == "prompt" else None
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
        if stale_message and (output.get("no_output") or not output.get("uiMessage")):
            output = {"uiMessage": stale_message}
        if output and not output.get("no_output"):
            sys.stdout.write(json.dumps(output) + "\n")
    except (ImportError, json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
