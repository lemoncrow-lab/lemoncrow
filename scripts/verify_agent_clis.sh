#!/usr/bin/env bash
# verify_agent_clis.sh — Verify LemonCrow installation across all agent CLIs
#
# Runs verification for each host. Each host uses its dedicated verify_<host>.sh
# wrapper when one exists (verify_claude.sh, verify_codex.sh, verify_copilot.sh,
# verify_opencode.sh); otherwise it falls back to install_<host>.sh, which runs
# its own post-install checks (e.g. antigravity).
# Hosts that were skipped (CLI absent) do not count as failures.
#
# Options:
#   --workspace DIR  Pass through to all verify scripts

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PASSTHROUGH=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --workspace)
            if [ $# -lt 2 ]; then
                echo "Missing value for --workspace" >&2
                exit 1
            fi
            PASSTHROUGH+=("$1" "$2")
            shift
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

declare -A RESULTS
HOSTS=(claude codex opencode copilot antigravity cursor hermes)

for host in "${HOSTS[@]}"; do
    echo ""
    echo "──────────────────────────────────────────"
    echo " Verifying LemonCrow in: ${host}"
    echo "──────────────────────────────────────────"
    case "$host" in
        claude) script="${SCRIPT_DIR}/verify_claude.sh" ;;
        *)
            # Prefer a dedicated verify_<host>.sh wrapper when present (wired the
            # same way as verify_claude.sh); otherwise fall back to install_<host>.sh.
            if [ -f "${SCRIPT_DIR}/verify_${host}.sh" ]; then
                script="${SCRIPT_DIR}/verify_${host}.sh"
            else
                script="${SCRIPT_DIR}/install_${host}.sh"
            fi
            ;;
    esac
    if [ ! -f "$script" ]; then
        echo "SKIPPED (no script found for $host)"
        RESULTS[$host]="skipped"
        continue
    fi
    output=$(bash "$script" "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}" 2>&1)
    exit_code=$?
    echo "$output"
    if echo "$output" | grep -q "=== SKIPPED"; then
        RESULTS[$host]="skipped"
    elif [ $exit_code -eq 0 ]; then
        RESULTS[$host]="pass"
    else
        RESULTS[$host]="fail"
    fi
done

echo ""
echo "══════════════════════════════════════════════"
echo " LemonCrow Verification Summary"
echo "══════════════════════════════════════════════"
FAIL_COUNT=0
for h in "${HOSTS[@]}"; do
    status="${RESULTS[$h]:-unknown}"
    case "$status" in
        pass)    echo "  PASS     $h" ;;
        skipped) echo "  SKIPPED  $h (CLI not found)" ;;
        fail)    echo "  FAIL     $h"; ((FAIL_COUNT++)) ;;
        *)       echo "  UNKNOWN  $h" ;;
    esac
done
echo ""

if [ "$FAIL_COUNT" -gt 0 ]; then
    echo "Verification FAILED for $FAIL_COUNT host(s). Re-run install scripts to fix."
    exit 1
fi
echo "All installed hosts passed verification."
