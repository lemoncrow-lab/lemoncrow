#!/usr/bin/env python3
"""agentStart hook for GitHub Copilot CLI.

Clears the workspace-scoped savings side log so each session starts fresh.
Payload: {sessionId, transcriptPath, timestamp, cwd}
"""

import hashlib
import json
import os
import sys
from pathlib import Path


def _atelier_root() -> Path:
    return Path(os.environ.get("ATELIER_ROOT", "") or Path.home() / ".atelier")


def _workspace_savings_path(workspace: str) -> Path:
    h = hashlib.sha256(str(Path(workspace).resolve()).encode()).hexdigest()[:12]
    return _atelier_root() / "workspaces" / h / "session_savings.jsonl"


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except Exception:
        payload = {}

    workspace = (
        payload.get("cwd")
        or os.environ.get("COPILOT_PROJECT_DIR")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )

    path = _workspace_savings_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    except Exception:
        pass


if __name__ == "__main__":
    main()
