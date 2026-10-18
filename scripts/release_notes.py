#!/usr/bin/env python3
"""Render GitHub release notes from the matching CHANGELOG section."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def extract_section(text: str, version: str) -> str:
    pattern = re.compile(rf"(?ms)^## \[{re.escape(version)}\](?:\s+-\s+[^\n]+)?\s*$\n(.*?)(?=^## |\Z)")
    match = pattern.search(text)
    if match is None:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    body = match.group(1).strip()
    if not body:
        raise ValueError(f"CHANGELOG.md section for {version} is empty")
    return body


def render_release_notes(text: str, version: str) -> str:
    changes = extract_section(text, version)
    return f"""## LemonCrow {version}

{changes}

### Installation

Install Review with the hosted service:

~~~bash
curl -fsSL https://lemoncrow.com/install.sh | bash
~~~

Scripted product choices:

~~~bash
# Review only (cloud)
curl -fsSL https://lemoncrow.com/install.sh | bash -s -- --product review

# Runtime only (cloud; coding-agent registration remains opt-in)
curl -fsSL https://lemoncrow.com/install.sh | bash -s -- --product runtime

# Review + Runtime (cloud)
curl -fsSL https://lemoncrow.com/install.sh | bash -s -- --product both

# Review + Runtime with local services
curl -fsSL https://lemoncrow.com/install.sh | bash -s -- --product both --local
~~~

After a cloud install:

~~~bash
# New terminals are configured automatically. In the current terminal:
source "$HOME/.lemoncrow/shell-env.sh"
lc auth login
lcr

# If Runtime was installed with --product runtime or --product both:
lc runtime status
lc runtime enable codex  # optional; or claude / opencode / auto
~~~

### Assets

- `hosted.sh` — hosted LemonCrow Review installer
- `lemoncrow-review-client.tar.gz` — architecture-neutral zero-dependency Review client
- `lemoncrow-review-engine.tar.gz` — Review-only local engine + shared product-aware frontend; no Runtime/MCP implementation
- `runtime.sh` — LemonCrow Runtime client installer (agent integration remains opt-in)
- `local_review.sh` — install and compose the isolated local Review engine + Review client
- `local_runtime.sh` — install serverless Runtime Lite; no local API server, frontend, daemon, or listener
- `lemoncrow-runtime-client.tar.gz` — architecture-neutral zero-dependency Runtime client carrying the Lite kernel
- `uninstall_product.sh` — unified backend for `lc uninstall review|runtime|all`
- `uninstall_review.sh` / `uninstall_runtime.sh` — independent low-level product uninstallers
- `install.sh` — unified public installer for Review, Runtime, or both; cloud or local
- `sessions.sh` — session management utility for managed/full local distributions
- `lemoncrow-distribution-*.tar.gz` — internal full local-mode bundles for supported platforms
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    parser.add_argument("--changelog", type=Path, default=REPO_ROOT / "CHANGELOG.md")
    args = parser.parse_args(argv)
    try:
        text = args.changelog.read_text(encoding="utf-8")
        sys.stdout.write(render_release_notes(text, args.version))
    except (OSError, ValueError) as exc:
        print(f"release notes failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
