#!/usr/bin/env python3
"""Codex Stop hook session summary backed by LemonCrow runtime state."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _lemoncrow_root() -> Path:
    root = os.environ.get("LEMONCROW_ROOT") or os.environ.get("LEMONCROW_STORE_ROOT")
    if root:
        return Path(root)
    return Path.home() / ".lemoncrow"


def main() -> int:
    try:
        from lemoncrow.core.capabilities.plugin_runtime import (
            build_codex_stop_output,
            build_codex_verify_output,
        )

        payload = json.loads(sys.stdin.read() or "{}")
        root = _lemoncrow_root()
        # Verify-before-done leads the savings summary, both as one systemMessage
        # -- the only Codex Stop output proven safe. Codex rejects unsupported
        # hook decisions, so a Claude-style {"decision":"block"} is not emitted
        # here until confirmed supported (it would error the hook).
        messages: list[str] = []
        verify = build_codex_verify_output(root, payload)
        if not verify.get("no_output"):
            messages.append(str(verify["systemMessage"]))
        output = build_codex_stop_output(root, payload)
        if not output.get("no_output"):
            messages.append(str(output["systemMessage"]))
        if messages:
            sys.stdout.write(json.dumps({"systemMessage": "\n\n".join(messages)}) + "\n")
    except (ImportError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
