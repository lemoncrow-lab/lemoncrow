#!/usr/bin/env python3
"""Optional post-commit hook to trigger lineage incremental update.

Install into .git/hooks/post-commit (via scripts/install_claude.sh):
    echo 'python /path/to/post_commit.py' >> .git/hooks/post-commit

Falls back gracefully if atelier is not installed or the DB is locked.
The primary incremental path is startup catch-up via _ensure_lineage_ready()
called during code op="search". This hook only accelerates updates when
Claude is actively running.

Fail-open: exit 0 always — git commit must not be blocked by this hook.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    try:
        repo_root = os.getcwd()
        import hashlib
        import pathlib

        from atelier.core.capabilities.code_context.engine import CodeContextEngine

        atelier_root = pathlib.Path(os.environ.get("ATELIER_ROOT") or pathlib.Path.home() / ".atelier")
        repo_id = hashlib.sha256(repo_root.encode()).hexdigest()[:16]
        db_path = atelier_root / "repos" / repo_id / "code.db"
        if not db_path.exists():
            return 0  # no DB for this repo yet; startup catch-up will handle it

        engine = CodeContextEngine(
            repo_root=pathlib.Path(repo_root),
            db_path=db_path,
        )
        engine._ensure_lineage_ready()
    except Exception:
        pass  # always fail-open
    return 0


if __name__ == "__main__":
    sys.exit(main())
