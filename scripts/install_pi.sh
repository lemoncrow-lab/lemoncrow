#!/usr/bin/env bash
# install_pi.sh — Install LemonCrow's checksum-pinned managed Pi frontend.
set -euo pipefail

DRY_RUN=false
PRINT_ONLY=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --print-only) PRINT_ONLY=true ;;
        --strict) : ;;
        --workspace)
            [[ $# -ge 2 ]] || { echo "Missing value for --workspace" >&2; exit 1; }
            shift  # Pi is a managed global frontend; workspace scope is irrelevant.
            ;;
        *) echo "Ignoring unsupported Pi installer option: $1" >&2 ;;
    esac
    shift
done

BIN_DIR="${LEMONCROW_BIN_DIR:-${HOME}/.lemoncrow/bin}"
if [[ -x "${BIN_DIR}/lc" ]]; then
    CLI="${BIN_DIR}/lc"
elif [[ -x "${BIN_DIR}/lemoncrow" ]]; then
    CLI="${BIN_DIR}/lemoncrow"
elif command -v lc >/dev/null 2>&1; then
    CLI="$(command -v lc)"
elif command -v lemoncrow >/dev/null 2>&1; then
    CLI="$(command -v lemoncrow)"
else
    echo "[lemoncrow:pi] ERROR: LemonCrow CLI is not installed yet" >&2
    exit 1
fi

if $DRY_RUN || $PRINT_ONLY; then
    printf '%q code host install --engine pi\n' "$CLI"
    exit 0
fi

# Reinstall the release pinned by this LemonCrow build. This upgrades an older
# managed Pi pin and re-verifies the release checksum on every LemonCrow install.
"$CLI" code host install --engine pi
echo "[lemoncrow:pi] managed Pi frontend ready"
