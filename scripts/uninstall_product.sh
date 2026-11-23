#!/usr/bin/env bash
# Unified product-aware LemonCrow uninstaller used by `lc uninstall`.
set -euo pipefail

# Siblings are trusted only beside a real script file; a piped copy has none and
# never falls back to the current directory.
if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
else
    SCRIPT_DIR=""
fi
[[ -n "$SCRIPT_DIR" ]] || {
    echo "uninstall_product.sh: run the installed copy (lc uninstall), not a piped one" >&2; exit 2;
}
PRODUCT="${1:-}"
[[ -n "$PRODUCT" ]] && shift || true
DRY_RUN=0
NO_HOSTS=0
PURGE=0
WORKSPACE=""

usage() {
    cat <<'EOF'
usage: uninstall_product.sh review|runtime|all [OPTIONS]

  review            remove Review, preserve Workspaces and account
  runtime           remove Workspaces, preserve Review and account
  all               remove all LemonCrow products and managed state

Options:
  --dry-run          show what would change
  --no-hosts         with runtime/all, leave coding-agent registrations untouched
  --workspace DIR    with all, scope workspace host cleanup
  --purge            accepted for all (all already removes managed state)
EOF
}

case "$PRODUCT" in review|runtime|all) ;; -h|--help|help|"") usage; [[ -n "$PRODUCT" ]] && exit 0 || exit 2 ;; *) echo "unknown product: $PRODUCT" >&2; usage >&2; exit 2 ;; esac
while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=1; shift ;;
        --no-hosts|--keep-hosts) NO_HOSTS=1; shift ;;
        --purge) PURGE=1; shift ;;
        --workspace)
            [[ $# -ge 2 ]] || { echo "--workspace needs a value" >&2; exit 2; }
            WORKSPACE="$2"; shift 2 ;;
        --workspace=*) WORKSPACE="${1#--workspace=}"; shift ;;
        -h|--help|help) usage; exit 0 ;;
        *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

case "$PRODUCT" in
    review)
        [[ "$NO_HOSTS" == "0" && "$PURGE" == "0" && -z "$WORKSPACE" ]] || {
            echo "review uninstall accepts only --dry-run; it never touches Workspaces hosts or shared account state" >&2; exit 2;
        }
        args=(); [[ "$DRY_RUN" == "1" ]] && args+=(--dry-run)
        exec bash "$SCRIPT_DIR/uninstall_review.sh" "${args[@]}"
        ;;
    runtime)
        [[ "$PURGE" == "0" && -z "$WORKSPACE" ]] || {
            echo "runtime uninstall accepts --dry-run/--no-hosts; Review and shared account state are preserved" >&2; exit 2;
        }
        args=(); [[ "$DRY_RUN" == "1" ]] && args+=(--dry-run); [[ "$NO_HOSTS" == "1" ]] && args+=(--keep-hosts)
        exec bash "$SCRIPT_DIR/uninstall_runtime.sh" "${args[@]}"
        ;;
    all)
        args=(--purge)
        [[ "$DRY_RUN" == "1" ]] && args+=(--dry-run)
        [[ "$NO_HOSTS" == "1" ]] && args+=(--no-hosts)
        [[ -n "$WORKSPACE" ]] && args+=(--workspace "$WORKSPACE")
        exec bash "$SCRIPT_DIR/uninstall.sh" "${args[@]}"
        ;;
esac
