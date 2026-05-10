#!/usr/bin/env bash
# install.sh — bootstrap Atelier from GitHub using a curl|bash-friendly flow.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/pankaj4u4m/atelier/main/scripts/install.sh | bash
#
# Optional environment variables:
#   ATELIER_REPO_URL   Git URL (default: https://github.com/pankaj4u4m/atelier.git)
#   ATELIER_REF        Git ref to install (default: main)
#   ATELIER_INSTALL_DIR Install location (default: ~/.local/share/atelier)
#   ATELIER_BIN_DIR    Global bin dir for command wrappers (default: ~/.local/bin)
#   ATELIER_NO_HOSTS   If set to 1, skip agent-host integration install scripts
#   ATELIER_NO_SERVICECTL If set to 1, skip starting the background service controller
#   ATELIER_SERVICECTL_INTERVAL_SECONDS Poll interval for servicectl (default: 60)
#   ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS Periodic maintenance interval (default: 21600)
#   ATELIER_DRY_RUN    If set to 1, print planned actions and exit
#   ATELIER_NO_STACK   If set to 1, skip starting the visualization stack (service + frontend)

set -euo pipefail

ATELIER_REPO_URL="${ATELIER_REPO_URL:-https://github.com/pankaj4u4m/atelier.git}"
ATELIER_REF="${ATELIER_REF:-main}"
ATELIER_INSTALL_DIR="${ATELIER_INSTALL_DIR:-${HOME}/.local/share/atelier}"
ATELIER_BIN_DIR="${ATELIER_BIN_DIR:-${HOME}/.local/bin}"
ATELIER_NO_HOSTS="${ATELIER_NO_HOSTS:-0}"
ATELIER_NO_SERVICECTL="${ATELIER_NO_SERVICECTL:-0}"
ATELIER_SERVICECTL_INTERVAL_SECONDS="${ATELIER_SERVICECTL_INTERVAL_SECONDS:-60}"
ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS="${ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS:-21600}"
ATELIER_DRY_RUN="${ATELIER_DRY_RUN:-0}"
ATELIER_NO_STACK="${ATELIER_NO_STACK:-0}"
# Default to local mode if running from within the Atelier repository
if [[ -f "uv.lock" && -d "src/atelier" && -f "scripts/install.sh" ]]; then
    ATELIER_LOCAL=1
else
    ATELIER_LOCAL=0
fi
PASSTHROUGH=()

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --local) ATELIER_LOCAL=1 ;;
        --no-local) ATELIER_LOCAL=0 ;;
        --dry-run) ATELIER_DRY_RUN=1; PASSTHROUGH+=("$1") ;;
        --no-hosts) ATELIER_NO_HOSTS=1; PASSTHROUGH+=("$1") ;;
        --no-stack) ATELIER_NO_STACK=1; PASSTHROUGH+=("$1") ;;
        *) PASSTHROUGH+=("$1") ;;
    esac
    shift
done

info() { echo "[atelier-install] $*"; }
warn() { echo "[atelier-install] WARN: $*" >&2; }
fail() { echo "[atelier-install] ERROR: $*" >&2; exit 1; }

run() {
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] $*"
    else
        "$@"
    fi
}

need_cmd() {
    command -v "$1" >/dev/null 2>&1 || fail "Missing required command: $1"
}

install_uv_if_needed() {
    if command -v uv >/dev/null 2>&1; then
        info "Found uv: $(uv --version 2>/dev/null || echo unknown)"
        return
    fi

    need_cmd curl
    info "Installing uv (official installer)..."
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] curl -LsSf https://astral.sh/uv/install.sh | sh"
    else
        # shellcheck disable=SC2016
        curl -LsSf https://astral.sh/uv/install.sh | sh
    fi

    if [[ -x "${HOME}/.local/bin/uv" ]]; then
        export PATH="${HOME}/.local/bin:${PATH}"
    fi

    command -v uv >/dev/null 2>&1 || fail "uv install completed but uv is still not on PATH"
    info "Installed uv: $(uv --version 2>/dev/null || echo unknown)"
}

prepare_repo() {
    local dir
    dir="$(dirname "$ATELIER_INSTALL_DIR")"
    run mkdir -p "$dir"

    if [[ -d "$ATELIER_INSTALL_DIR/.git" ]]; then
        info "Updating existing repository in $ATELIER_INSTALL_DIR"
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR fetch --tags --prune origin"
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR checkout $ATELIER_REF"
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR pull --ff-only origin $ATELIER_REF"
        else
            git -C "$ATELIER_INSTALL_DIR" fetch --tags --prune origin
            git -C "$ATELIER_INSTALL_DIR" checkout "$ATELIER_REF"
            git -C "$ATELIER_INSTALL_DIR" pull --ff-only origin "$ATELIER_REF"
        fi
    else
        info "Cloning $ATELIER_REPO_URL into $ATELIER_INSTALL_DIR"
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] git clone --depth=1 --branch $ATELIER_REF $ATELIER_REPO_URL $ATELIER_INSTALL_DIR"
        else
            git clone --depth=1 --branch "$ATELIER_REF" "$ATELIER_REPO_URL" "$ATELIER_INSTALL_DIR"
        fi
    fi
}

write_wrapper() {
    local cmd="$1"
    local target="$ATELIER_BIN_DIR/$cmd"

    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] write wrapper $target"
        return
    fi

    cat >"$target" <<EOF
#!/usr/bin/env bash
set -euo pipefail
# Default to global store in home directory if not set
export ATELIER_ROOT="\${ATELIER_ROOT:-\${HOME}/.atelier}"
export ATELIER_INSTALL_DIR="$ATELIER_INSTALL_DIR"
exec uv --directory "$ATELIER_INSTALL_DIR" run "$cmd" "\$@"
EOF
    chmod +x "$target"
}

main() {
    case "$(uname -s)" in
        Linux|Darwin) ;;
        *) fail "Unsupported OS: $(uname -s). This installer supports Linux/macOS." ;;
    esac

    need_cmd git
    need_cmd bash
    install_uv_if_needed

    if [[ "$ATELIER_LOCAL" == "1" ]]; then
        info "Local mode: using current directory as install source"
        ATELIER_INSTALL_DIR="$(pwd)"
    else
        prepare_repo
    fi

    info "Installing Atelier Python environment..."
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] uv --directory $ATELIER_INSTALL_DIR sync --all-extras"
    else
        uv --directory "$ATELIER_INSTALL_DIR" sync --all-extras
    fi

    if [[ "$ATELIER_NO_HOSTS" != "1" ]]; then
        info "Installing Atelier host integrations (skip if host CLI is missing)..."
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] bash $ATELIER_INSTALL_DIR/scripts/install_agent_clis.sh ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"
        else
            bash "$ATELIER_INSTALL_DIR/scripts/install_agent_clis.sh" ${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}
        fi
        # Persist host detection results for the Docker service
        if [[ "$ATELIER_DRY_RUN" != "1" && -f "$ATELIER_INSTALL_DIR/scripts/status.sh" ]]; then
            bash "$ATELIER_INSTALL_DIR/scripts/status.sh" --write 2>/dev/null || true
        fi
    else
        info "Skipping host integrations because ATELIER_NO_HOSTS=1"
        # Still persist current detection state even when skipping install
        if [[ "$ATELIER_DRY_RUN" != "1" && -f "$ATELIER_INSTALL_DIR/scripts/status.sh" ]]; then
            bash "$ATELIER_INSTALL_DIR/scripts/status.sh" --write 2>/dev/null || true
        fi
    fi

    run mkdir -p "$ATELIER_BIN_DIR"
    write_wrapper atelier
    write_wrapper atelier-mcp
    write_wrapper atelier-api
    write_wrapper atelier-codex

    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] ln -sf $ATELIER_INSTALL_DIR/bin/atelier-status $ATELIER_BIN_DIR/atelier-status"
    elif [[ -f "$ATELIER_INSTALL_DIR/bin/atelier-status" ]]; then
        run ln -sf "$ATELIER_INSTALL_DIR/bin/atelier-status" "$ATELIER_BIN_DIR/atelier-status"
    else
        warn "atelier-status helper not found at $ATELIER_INSTALL_DIR/bin/atelier-status"
    fi

    if [[ ":$PATH:" != *":$ATELIER_BIN_DIR:"* ]]; then
        warn "$ATELIER_BIN_DIR is not currently on PATH"
        echo ""
        echo "Add this to your shell profile, then restart your shell:"
        echo "  export PATH=\"$ATELIER_BIN_DIR:\$PATH\""
        echo ""
    fi

    info "Initializing Atelier runtime store..."
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] $ATELIER_BIN_DIR/atelier init"
    else
        "$ATELIER_BIN_DIR/atelier" init >/dev/null
    fi

    if [[ "$ATELIER_NO_SERVICECTL" != "1" ]]; then
        info "Starting Atelier background service controller..."
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] $ATELIER_BIN_DIR/atelier servicectl start --interval-seconds $ATELIER_SERVICECTL_INTERVAL_SECONDS --maintenance-interval-seconds $ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS"
        else
            "$ATELIER_BIN_DIR/atelier" servicectl start \
                --interval-seconds "$ATELIER_SERVICECTL_INTERVAL_SECONDS" \
                --maintenance-interval-seconds "$ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS" >/dev/null
        fi
    else
        info "Skipping background service controller because ATELIER_NO_SERVICECTL=1"
    fi

    STACK_STARTED=0
    if [[ "$ATELIER_NO_STACK" != "1" ]]; then
        if command -v docker >/dev/null 2>&1; then
            info "Starting Atelier visualization stack (service + frontend)..."
            if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
                echo "[dry-run] $ATELIER_BIN_DIR/atelier stack start"
            else
                "$ATELIER_BIN_DIR/atelier" stack start 2>/dev/null \
                    && STACK_STARTED=1 \
                    || warn "Visualization stack did not start (Docker daemon may not be running)"
            fi
        else
            info "Skipping visualization stack because Docker is not installed"
        fi
    else
        info "Skipping visualization stack because ATELIER_NO_STACK=1"
    fi

    info "Install complete."
    echo ""
    if [[ "$STACK_STARTED" == "1" ]]; then
        echo "  Visualization stack is running:"
        echo "    frontend: http://localhost:3125"
        echo "    service:  http://localhost:8787"
        echo ""
    fi
    echo "  Commands:"
    echo "    atelier --version           - Check core CLI version"
    echo "    atelier-mcp --version       - Check MCP server version"
    echo "    atelier servicectl status   - View background service and systemctl status"
    echo "    atelier stack start         - Start production API and Frontend (requires Docker)"
    echo "    atelier stack stop          - Stop the visualization stack"
    echo "    atelier stack logs          - View stack logs"
    echo "    atelier-status              - Show one-line status of the active reasoning run"
}

main "$@"
