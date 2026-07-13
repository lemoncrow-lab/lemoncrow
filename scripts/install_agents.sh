#!/usr/bin/env bash
# install_agents.sh — Universal project-local LemonCrow installer
#
# Creates/updates the universally-respected AGENTS.md config file.
# AGENTS.md is respected by opencode, codex, copilot, gemini, claude, etc.
#
# This script is host-agnostic. Run it once per project regardless of which
# agent CLI(s) you use. Per-host installers (install_opencode.sh, etc.) each
# add their own host-specific configs but do NOT touch AGENTS.md.
#
# Usage:
#   bash scripts/install_agents.sh --workspace /path/to/project
#   bash scripts/install_agents.sh --workspace . --dry-run
#   bash scripts/install_agents.sh --print-only
#
# Options:
#   --workspace DIR  Project root to install into (default: current directory)
#   --dry-run        Print what would happen, touch nothing
#   --print-only     Print manual steps, touch nothing

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEMONCROW_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
source "${SCRIPT_DIR}/lib/managed_context.sh"

DRY_RUN=false
PRINT_ONLY=false
WORKSPACE=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=true ;;
        --print-only) PRINT_ONLY=true ;;
        --workspace)
            if [ $# -lt 2 ]; then
                echo "Missing value for --workspace" >&2
                exit 1
            fi
            WORKSPACE="$2"
            shift
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

if [ -z "$WORKSPACE" ]; then
    WORKSPACE="$(pwd)"
fi
WORKSPACE="$(cd "$WORKSPACE" && pwd)"

# Host-neutral lemoncrow:code persona that ships with the distribution. Never source
# the repo's own AGENTS.md (that is LemonCrow's dev entrypoint, not a user persona).
AGENTS_SOURCE="${LEMONCROW_REPO}/integrations/AGENTS.lemoncrow.md"

info()  { [[ "${LEMONCROW_VERBOSE:-0}" == "1" ]] && echo "[lemoncrow:agents] $*" || true; }
warn()  { echo "[lemoncrow:agents] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || "$@"; }

if $PRINT_ONLY; then
    echo ""
    echo "=== LemonCrow Universal Agents Install ==="
    echo ""
    echo "Project: ${WORKSPACE}"
    echo ""
    echo "1. Ensure ${WORKSPACE}/AGENTS.md has lemoncrow:code persona"
    echo "   Source: ${AGENTS_SOURCE}"
    echo ""
    echo "2. Install the Git prepare-commit-msg hook for LemonCrow co-author attribution"
    echo "   Trailer: Co-Authored-By: lemoncrow <302591943+lemoncrow-agent[bot]@users.noreply.github.com>"
    echo ""
    echo "After install, AGENTS.md will contain the lemoncrow:code agent persona and commits will carry LemonCrow attribution."
    exit 0
fi

# ── 1. AGENTS.md ──────────────────────────────────────────────────────────────
# Ensures the project's AGENTS.md includes the lemoncrow:code persona via
# sentinel markers so re-install updates in place without destroying user content.

AGENTS_FILE="${WORKSPACE}/AGENTS.md"

if [ -f "$AGENTS_SOURCE" ]; then
    if [ -f "$AGENTS_FILE" ]; then
        if $DRY_RUN; then
            lemoncrow_upsert_managed_block "$AGENTS_SOURCE" "$AGENTS_FILE" "true"
            info "[dry-run] would ensure lemoncrow:code persona in $AGENTS_FILE"
        else
            lemoncrow_upsert_managed_block "$AGENTS_SOURCE" "$AGENTS_FILE" "false"
            info "ensured lemoncrow:code persona in $AGENTS_FILE"
        fi
    else
        if $DRY_RUN; then
            lemoncrow_write_managed_copy "$AGENTS_SOURCE" "$AGENTS_FILE" "true"
            info "[dry-run] would create $AGENTS_FILE with lemoncrow:code persona"
        else
            lemoncrow_write_managed_copy "$AGENTS_SOURCE" "$AGENTS_FILE" "false"
            info "created $AGENTS_FILE with lemoncrow:code persona"
        fi
    fi
else
    warn "LemonCrow persona source not found: $AGENTS_SOURCE"
fi

# ── 2. Git attribution ───────────────────────────────────────────────────────
# Host-agnostic co-author attribution for commits made by any LemonCrow-backed
# agent in this workspace.
lemoncrow_install_attribution_hook "$WORKSPACE" "$DRY_RUN"

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
info "Universal agents config installed in ${WORKSPACE}"
info "  ${AGENTS_FILE}  — lemoncrow:code persona (respected by all agent CLIs)"
info "  Git hook       — LemonCrow co-author attribution for agent commits"
echo ""
info "Next: install per-host configs (if needed)"
info "  bash scripts/install_opencode.sh --workspace '${WORKSPACE}'"
info "  bash scripts/install_codex.sh --workspace '${WORKSPACE}'"
info "  bash scripts/install_copilot.sh --workspace '${WORKSPACE}'"
info "  bash scripts/install_claude.sh --workspace '${WORKSPACE}'"
