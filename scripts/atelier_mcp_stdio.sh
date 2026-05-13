#!/usr/bin/env bash
# atelier_mcp_stdio.sh — Stable MCP stdio wrapper for all agent hosts
#
# Usage: referenced directly in MCP host configs as the "command" field.
#
# Behaviour:
#   1. Locates the atelier repo root from this script's own location
#      (works regardless of which directory the host spawns the process from)
#   2. Sets ATELIER_SERVICE_URL to http://127.0.0.1:8787 if not already set
#   3. Sets ATELIER_KNOWLEDGE_ROOT to <workspace>/.knowledge if not already set
#   4. Runs: atelier-mcp
#   5. All log/debug output → stderr ONLY (never contaminates MCP JSON-RPC stdout)
#
# Environment variables honoured:
#   ATELIER_SERVICE_URL       — override local Atelier HTTP service URL
#   ATELIER_KNOWLEDGE_ROOT    — override knowledge root (default: <workspace>/.knowledge)
#   ATELIER_WORKSPACE_ROOT    — override workspace root (default: cwd at exec time)
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

# --- service URL ------------------------------------------------------------
if [ -z "${ATELIER_SERVICE_URL:-}" ]; then
    export ATELIER_SERVICE_URL="http://127.0.0.1:8787"
fi

if [ -z "${ATELIER_KNOWLEDGE_ROOT:-}" ]; then
    export ATELIER_KNOWLEDGE_ROOT="${ATELIER_WORKSPACE_ROOT}/.knowledge"
fi

# --- diagnostics → stderr only (never stdout) ------------------------------
>&2 echo "[atelier-mcp] repo=$ATELIER_REPO workspace=${ATELIER_WORKSPACE_ROOT} service=${ATELIER_SERVICE_URL}"

# --- exec MCP server --------------------------------------------------------
cd "$ATELIER_REPO"
exec atelier-mcp "$@"
