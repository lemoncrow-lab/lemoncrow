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
        # Verify-before-done first. A block re-prompts the turn (Claude Stop
        # protocol, which Codex honours), so emit it ALONE and skip the savings
        # summary -- the turn isn't ending, and mixing a block decision with a
        # systemMessage would muddy the signal.
        verify = build_codex_verify_output(root, payload)
        if verify.get("decision") == "block":
            sys.stdout.write(json.dumps({"decision": "block", "reason": verify["reason"]}) + "\n")
            return 0
        output = build_codex_stop_output(root, payload)
        if not output.get("no_output"):
            sys.stdout.write(json.dumps({"systemMessage": output["systemMessage"]}) + "\n")
    except (ImportError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
