#!/usr/bin/env python3
"""Write stable per-worktree environment values for local stack bootstraps."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def build_env(worktree: Path) -> dict[str, str]:
    digest = hashlib.sha256(str(worktree).encode("utf-8")).hexdigest()
    slot = int(digest[:6], 16) % 200
    worktree_id = f"atelier-{digest[:8]}"
    api_port = 8787 + slot
    frontend_port = 3125 + slot
    return {
        "ATELIER_WORKTREE_ID": worktree_id,
        "ATELIER_SERVICE_CONTAINER": f"{worktree_id}-service",
        "ATELIER_FRONTEND_CONTAINER": f"{worktree_id}-frontend",
        "ATELIER_SERVICE_PORT": str(api_port),
        "ATELIER_FRONTEND_PORT": str(frontend_port),
        "ATELIER_STACK_ROOT": str(worktree / ".atelier-worktree"),
        "ATELIER_WORKTREE_PATH": str(worktree),
        "VITE_API_URL": f"http://localhost:{api_port}",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worktree", type=Path, default=Path.cwd(), help="worktree path to hash")
    parser.add_argument("--env-file", type=Path, help="write KEY=value pairs to this file")
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    env = build_env(args.worktree.resolve())

    if args.env_file:
        args.env_file.parent.mkdir(parents=True, exist_ok=True)
        args.env_file.write_text("\n".join(f"{k}={v}" for k, v in env.items()) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(env, indent=2, sort_keys=True))
    else:
        for key, value in env.items():
            print(f"{key}={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
