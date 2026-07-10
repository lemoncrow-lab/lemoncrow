#!/usr/bin/env bash
# Thin wrapper — the canonical installer lives at scripts/install_opencode.sh
# in the repository root (it sources scripts/lib/managed_context.sh and
# resolves repo paths relative to scripts/). Keep this file logic-free.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/../../scripts/install_opencode.sh" "$@"
