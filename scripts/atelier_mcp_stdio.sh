#!/usr/bin/env bash
# atelier_mcp_stdio.sh — Stable MCP stdio wrapper for all agent hosts
#
# Usage: referenced directly in MCP host configs as the "command" field.
#
# Behaviour:
#   1. Locates the atelier repo root from this script's own location
#      (works regardless of which directory the host spawns the process from)
#   2. Sets ATELIER_ROOT to ~/.atelier if not already set
#   3. Sets ATELIER_KNOWLEDGE_ROOT to <workspace>/.knowledge if not already set
#   4. Runs: uv run python -m atelier.gateway.adapters.mcp_server
#   5. All log/debug output → stderr ONLY (never contaminates MCP JSON-RPC stdout)
#
# Environment variables honoured:
#   ATELIER_ROOT              — override store root (default: ~/.atelier)
#   ATELIER_KNOWLEDGE_ROOT    — override knowledge root (default: <workspace>/.knowledge)
#   ATELIER_WORKSPACE_ROOT    — override workspace root (default: cwd at exec time)
#   ATELIER_STORE_ROOT        — explicit store root alias when ATELIER_ROOT is unset
#
# This script intentionally never writes non-JSON to stdout.

set -euo pipefail

# --- locate atelier repo root (parent of scripts/) --------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELIER_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

# --- workspace root (provided by host or fallback to cwd) -------------------
if [ -z "${ATELIER_WORKSPACE_ROOT:-}" ]; then
    export ATELIER_WORKSPACE_ROOT="${PWD}"
fi

# --- store root -------------------------------------------------------------
# The MCP server reads ATELIER_ROOT. Accept ATELIER_STORE_ROOT as a friendlier
# host-config alias, then fall back to the global ~/.atelier store.
if [ -z "${ATELIER_ROOT:-}" ]; then
    if [ -n "${ATELIER_STORE_ROOT:-}" ]; then
        export ATELIER_ROOT="${ATELIER_STORE_ROOT}"
    else
        export ATELIER_ROOT="${HOME}/.atelier"
    fi
fi

if [ -z "${ATELIER_KNOWLEDGE_ROOT:-}" ]; then
    export ATELIER_KNOWLEDGE_ROOT="${ATELIER_WORKSPACE_ROOT}/.knowledge"
fi

# --- diagnostics → stderr only (never stdout) ------------------------------
>&2 echo "[atelier-mcp] repo=$ATELIER_REPO workspace=${ATELIER_WORKSPACE_ROOT} root=${ATELIER_ROOT:-${ATELIER_STORE_ROOT:-unset}}"

# --- exec MCP server --------------------------------------------------------
cd "$ATELIER_REPO"
exec uv run python -m atelier.gateway.adapters.mcp_server "$@"
