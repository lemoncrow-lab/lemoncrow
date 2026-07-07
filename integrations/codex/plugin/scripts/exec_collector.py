#!/usr/bin/env python3
"""Record ``codex exec --json`` events into the Atelier run ledger.

Pipe the JSON-Lines event stream from a headless Codex run through this to
backfill command/file telemetry for tools that do not fire interactive hooks
(Codex only instruments shell/apply_patch/mcp for PostToolUse):

    codex exec --json "<task>" | python exec_collector.py --session <run-id>

Session id resolution: ``--session`` arg, else $ATELIER_STATUS_SESSION_ID, else
$CODEX_SESSION_ID. Fail-open: prints nothing and exits 0 on any error.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _atelier_root() -> Path:
    root = os.environ.get("ATELIER_ROOT") or os.environ.get("ATELIER_STORE_ROOT")
    return Path(root) if root else Path.home() / ".atelier"


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest 'codex exec --json' events into the Atelier ledger.")
    parser.add_argument("--session", default="", help="Run-ledger session id to append events to.")
    args, _ = parser.parse_known_args()
    session_id = args.session or os.environ.get("ATELIER_STATUS_SESSION_ID") or os.environ.get("CODEX_SESSION_ID") or ""
    try:
        from atelier.core.capabilities.plugin_runtime import ingest_codex_exec_events

        lines = sys.stdin.read().splitlines()
        count = ingest_codex_exec_events(_atelier_root(), session_id, lines)
        if count:
            sys.stderr.write(f"recorded {count} codex exec events to the run ledger\n")
    except (ImportError, OSError, ValueError, TypeError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
