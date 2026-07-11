#!/usr/bin/env python3
"""Codex UserPromptSubmit hook for a display-only compaction warning."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    return Path(root) if root else Path.home() / ".lemoncrow"


def main() -> int:
    try:
        from lemoncrow.core.capabilities.plugin_runtime import build_codex_user_prompt_output

        payload = json.loads(sys.stdin.read() or "{}")
        output = build_codex_user_prompt_output(_lemoncrow_root(), payload)
        rendered: dict[str, object] = {}
        message = output.get("uiMessage")
        if isinstance(message, str) and message.startswith("LemonCrow context guard: high context"):
            rendered["systemMessage"] = message.replace("LemonCrow context guard: high context", "Context high").replace(
                "consider compacting", "run /compact"
            )
        if rendered:
            sys.stdout.write(json.dumps(rendered) + "\n")
    except (ImportError, json.JSONDecodeError, KeyError, TypeError, ValueError, OSError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
