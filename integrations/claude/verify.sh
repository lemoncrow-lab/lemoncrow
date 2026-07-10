#!/usr/bin/env bash
# Thin wrapper — the canonical verifier lives at scripts/verify_claude.sh
# in the repository root. Keep this file logic-free.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/../../scripts/verify_claude.sh" "$@"
