"""OpenCode chat.message adapter for Atelier prompt-time nudges."""

from __future__ import annotations

import datetime
import json
import os
import sys
from pathlib import Path


def _atelier_root() -> Path:
    root = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT")
    return Path(root) if root else Path.home() / ".atelier"


def _stale_nudge_message(root: Path) -> str | None:
    """Once-per-item-per-day staleness nudge for installed OPTIONAL agents.

    OpenCode has no skills concept (see agents_skills._skill_dir), so only
    installed agent roles are checked here. Reuses the exact same
    stale_optional_items/format_stale_nudge the Claude statusline tip and
    `atelier stale-nudge` CLI use -- one shared threshold/cost calculation
    for every host. Fail-open: any error means no nudge, never a crash.
    """
    try:
        from atelier.gateway.cli.commands.agents_skills import (
            format_stale_nudge,
            stale_optional_items,
        )

        items = stale_optional_items("opencode", None, root=root)
        if not items:
            return None
        today = datetime.date.today().isoformat()
        marker_dir = root / "opencode_stale_nudge_shown"
        for item in items:
            marker = marker_dir / f"{item['kind']}_{item['name']}"
            if marker.exists() and marker.read_text(encoding="utf-8").strip() == today:
                continue
            marker_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(today, encoding="utf-8")
            return format_stale_nudge(item)
    except (ImportError, OSError, KeyError, TypeError, ValueError):
        pass
    return None


def main() -> int:
    try:
        from atelier.core.capabilities.plugin_runtime import build_opencode_user_prompt_output

        payload = json.loads(sys.stdin.read() or "{}")
        root = _atelier_root()
        output = build_opencode_user_prompt_output(root, payload)
        if output.get("no_output") or not output.get("uiMessage"):
            stale_message = _stale_nudge_message(root)
            if stale_message:
                output = {"uiMessage": stale_message}
        if output and not output.get("no_output"):
            sys.stdout.write(json.dumps(output) + "\n")
    except (ImportError, json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
