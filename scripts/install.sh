#!/usr/bin/env bash
# install.sh — bootstrap Atelier from GitHub using a curl|bash-friendly flow.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/pankaj4u4m/atelier/main/scripts/install.sh | bash
#
# By default only the core service and frontend are installed natively.
# Pass --advanced --memory letta|openmemory to install one Docker sidecar.
#
# Optional environment variables:
#   ATELIER_REPO_URL   Git URL (default: https://github.com/pankaj4u4m/atelier.git)
#   ATELIER_REF        Git ref to install (default: main)
#   ATELIER_INSTALL_DIR Install location (default: ~/.local/share/atelier)
#   ATELIER_BIN_DIR    Global bin dir for console scripts (default: ~/.local/bin)
#   ATELIER_TOOL_DIR   uv tool environment dir (default: ~/.local/share/uv/tools)
#   ATELIER_NO_HOSTS   If set to 1, skip agent-host integration install scripts
#   ATELIER_NO_SERVICECTL If set to 1, skip starting the background service controller
#   ATELIER_SERVICECTL_INTERVAL_SECONDS Poll interval for servicectl (default: 60)
#   ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS Periodic maintenance interval (default: 21600)
#   ATELIER_DRY_RUN    If set to 1, print planned actions and exit
#   ATELIER_NO_STACK   If set to 1, skip starting the visualization stack (service + frontend)
#   ATELIER_ADVANCED   If set to 1, enable Docker sidecar install (requires --memory)
#   ATELIER_MEMORY_BACKEND  Memory sidecar to install: letta | openmemory (default: none)
#   ATELIER_ZOEKT      If set to 1, install the persistent Zoekt code-search sidecar (Docker)
#   ATELIER_LOCAL      If set to 1, install from the current checkout in editable mode
#   ATELIER_STRICT     If set to 1, treat selected post-install degradations as errors
#
# Notes:
#   Exactly one memory sidecar can be active at a time; the selection is
#   persisted to ~/.atelier/memory_backend for uninstall cleanup.
#
#   Codex host install manages its Atelier AGENTS block with explicit START/END
#   sentinels so re-install can replace that block without overwriting user content.

set -euo pipefail

if [[ -t 1 ]]; then
    C_RESET="$(printf '\033[0m')"
    C_BOLD="$(printf '\033[1m')"
    C_GREEN="$(printf '\033[32m')"
    C_RED="$(printf '\033[31m')"
    C_YELLOW="$(printf '\033[33m')"
    C_CYAN="$(printf '\033[36m')"
else
    C_RESET=""
    C_BOLD=""
    C_GREEN=""
    C_RED=""
    C_YELLOW=""
    C_CYAN=""
fi
if [[ -n "${FORCE_COLOR:-}${CLICOLOR_FORCE:-}" && -z "${NO_COLOR:-}" ]]; then
    C_RESET="$(printf '\033[0m')"
    C_BOLD="$(printf '\033[1m')"
    C_GREEN="$(printf '\033[32m')"
    C_RED="$(printf '\033[31m')"
    C_YELLOW="$(printf '\033[33m')"
    C_CYAN="$(printf '\033[36m')"
fi

ATELIER_REPO_URL="${ATELIER_REPO_URL:-https://github.com/pankaj4u4m/atelier.git}"
ATELIER_REF="${ATELIER_REF:-main}"
ATELIER_INSTALL_DIR="${ATELIER_INSTALL_DIR:-${HOME}/.local/share/atelier}"
ATELIER_BIN_DIR="${ATELIER_BIN_DIR:-${HOME}/.local/bin}"
ATELIER_TOOL_DIR="${ATELIER_TOOL_DIR:-${HOME}/.local/share/uv/tools}"
ATELIER_INSTALL_RECORD="${ATELIER_INSTALL_RECORD:-${HOME}/.atelier/install_dir}"
ATELIER_NO_HOSTS="${ATELIER_NO_HOSTS:-0}"
ATELIER_NO_SERVICECTL="${ATELIER_NO_SERVICECTL:-0}"
ATELIER_SERVICECTL_INTERVAL_SECONDS="${ATELIER_SERVICECTL_INTERVAL_SECONDS:-60}"
ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS="${ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS:-21600}"
ATELIER_DRY_RUN="${ATELIER_DRY_RUN:-0}"
ATELIER_NO_STACK="${ATELIER_NO_STACK:-0}"
ATELIER_ADVANCED="${ATELIER_ADVANCED:-0}"
ATELIER_MEMORY_BACKEND="${ATELIER_MEMORY_BACKEND:-}"   # letta | openmemory | (empty = none)
ATELIER_ZOEKT="${ATELIER_ZOEKT:-}"                     # 1 = install persistent Zoekt sidecar
ATELIER_LOCAL="${ATELIER_LOCAL:-0}"
ATELIER_STRICT="${ATELIER_STRICT:-0}"
STACK_STARTED=0
PASSTHROUGH=()
WARNINGS=()
ERRORS=()
FINAL_EXIT_CODE=0
HOST_FLAGS=()
HOST_SCOPE_ARGS=()
HOST_EXTRA_ARGS=()
SKIP_CLI_INSTALL=0

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --local) ATELIER_LOCAL=1 ;;
        --remote|--no-local) ATELIER_LOCAL=0 ;;
        --dry-run) ATELIER_DRY_RUN=1; PASSTHROUGH+=("$1") ;;
        --no-hosts) ATELIER_NO_HOSTS=1; PASSTHROUGH+=("$1") ;;
        --no-stack) ATELIER_NO_STACK=1; PASSTHROUGH+=("$1") ;;
        --advanced) ATELIER_ADVANCED=1 ;;
        --memory)
            if [[ $# -lt 2 ]]; then fail "--memory requires a value: letta or openmemory"; fi
            shift; ATELIER_MEMORY_BACKEND="$1" ;;
        --memory=*) ATELIER_MEMORY_BACKEND="${1#--memory=}" ;;
        --zoekt) ATELIER_ZOEKT=1; ATELIER_ADVANCED=1 ;;
        --strict) ATELIER_STRICT=1; PASSTHROUGH+=("$1") ;;
        *) PASSTHROUGH+=("$1") ;;
    esac
    shift
done

info() { echo "[atelier-install] $*"; }
warn() {
    WARNINGS+=("$*")
    printf "%b[atelier-install] WARN:%b %s\n" "$C_YELLOW" "$C_RESET" "$*" >&2
}
error() {
    ERRORS+=("$*")
    printf "%b[atelier-install] ERROR:%b %s\n" "$C_RED" "$C_RESET" "$*" >&2
}
fail() { error "$*"; exit 1; }
degrade() {
    if [[ "$ATELIER_STRICT" == "1" ]]; then
        ERRORS+=("$*")
        FINAL_EXIT_CODE=1
        printf "%b[atelier-install] ERROR:%b %s\n" "$C_RED" "$C_RESET" "$*" >&2
    else
        warn "$*"
    fi
}

collect_issues_from_output() {
    local output="$1"
    local line
    while IFS= read -r line; do
        line="$(printf "%s\n" "$line" | sed $'s/\x1b\\[[0-9;]*m//g')"
        case "$line" in
            *"] WARN:"*)
                WARNINGS+=("${line#*WARN: }")
                ;;
            *"] ERROR:"*)
                ERRORS+=("${line#*ERROR: }")
                ;;
        esac
    done <<<"$output"
}

print_issue_group() {
    local title="$1"
    local color="$2"
    shift 2
    local entries=("$@")
    local -A counted=()
    local -A printed=()
    local entry
    local count=0

    for entry in "${entries[@]+"${entries[@]}"}"; do
        [[ -n "$entry" && -z "${counted[$entry]+x}" ]] || continue
        counted["$entry"]=1
        count=$((count + 1))
    done

    [[ $count -gt 0 ]] || return 0
    printf "%b%s (%d)%b\n" "$color" "$title" "$count" "$C_RESET"
    for entry in "${entries[@]+"${entries[@]}"}"; do
        [[ -n "$entry" && -z "${printed[$entry]+x}" ]] || continue
        printed["$entry"]=1
        printf "  %b-%b %s\n" "$color" "$C_RESET" "$entry"
    done
}

print_final_report() {
    echo ""
    echo "══════════════════════════════════════════════"
    echo " Atelier Install Report"
    echo "══════════════════════════════════════════════"
    if [[ ${#ERRORS[@]} -eq 0 && ${#WARNINGS[@]} -eq 0 ]]; then
        echo "  No warnings or errors detected."
        return
    fi
    print_issue_group "Errors" "$C_RED" "${ERRORS[@]+"${ERRORS[@]}"}"
    print_issue_group "Warnings" "$C_YELLOW" "${WARNINGS[@]+"${WARNINGS[@]}"}"
}

prompt_memory_selection() {
    [[ -t 0 ]] || return 0
    [[ -n "$ATELIER_MEMORY_BACKEND" || "$ATELIER_ADVANCED" == "1" ]] && return 0

    echo ""
    printf "%b[atelier-install]%b Choose a memory backend:\n" "$C_BOLD" "$C_RESET"
    printf "  0) SQLite      - local, no Docker needed (default)\n"
    printf "  1) letta       - Letta memory server (Docker)\n"
    printf "  2) openmemory  - OpenMemory MCP server (Docker + OpenAI key or ollama)\n"
    printf "Choice [0/1/2, default: 0]: "
    local choice
    read -r choice </dev/tty
    echo ""

    case "$choice" in
        1) ATELIER_MEMORY_BACKEND="letta"; ATELIER_ADVANCED=1 ;;
        2) ATELIER_MEMORY_BACKEND="openmemory"; ATELIER_ADVANCED=1 ;;
        *) ATELIER_MEMORY_BACKEND="" ;;
    esac
}

has_flag() {
    local needle="$1"
    local item
    for item in "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"; do
        [[ "$item" == "$needle" ]] && return 0
    done
    return 1
}

contains_any_host_flag() {
    has_flag "--all" && return 0
    has_flag "--claude" && return 0
    has_flag "--codex" && return 0
    has_flag "--opencode" && return 0
    has_flag "--copilot" && return 0
    has_flag "--antigravity" && return 0
    return 1
}

detect_hosts() {
    HOST_FLAGS=()
    HOST_SUMMARY=()

    if command -v claude >/dev/null 2>&1; then
        HOST_SUMMARY+=("Claude Code (detected)")
    else
        HOST_SUMMARY+=("Claude Code")
    fi

    if command -v codex >/dev/null 2>&1; then
        HOST_SUMMARY+=("Codex CLI (detected)")
    else
        HOST_SUMMARY+=("Codex CLI")
    fi

    if command -v opencode >/dev/null 2>&1; then
        HOST_SUMMARY+=("opencode (detected)")
    else
        HOST_SUMMARY+=("opencode")
    fi

    if command -v code >/dev/null 2>&1; then
        HOST_SUMMARY+=("Copilot/VS Code (detected)")
    else
        HOST_SUMMARY+=("Copilot/VS Code")
    fi

    if command -v antigravity >/dev/null 2>&1 || command -v agy >/dev/null 2>&1; then
        HOST_SUMMARY+=("Antigravity (detected)")
    else
        HOST_SUMMARY+=("Antigravity")
    fi
}

join_with_comma_space() {
    local joined=""
    local item
    for item in "$@"; do
        if [[ -z "$joined" ]]; then
            joined="$item"
        else
            joined="$joined, $item"
        fi
    done
    printf "%s" "$joined"
}

host_wizard() {
    [[ -t 0 && -t 1 ]] || return 0
    [[ "$ATELIER_NO_HOSTS" == "1" ]] && return 0
    contains_any_host_flag && return 0
    has_flag "--workspace" && return 0

    detect_hosts

    echo ""
    printf "%b┌  Atelier installer%b\n" "$C_CYAN" "$C_RESET"
    echo "│"
    printf "◇  Which agents should Atelier configure?\n"
    printf "│  %s\n" "$(join_with_comma_space "${HOST_SUMMARY[@]}")"
    echo "│"
    printf "│  1) Claude Code\n"
    printf "│  2) Codex CLI\n"
    printf "│  3) opencode\n"
    printf "│  4) Copilot/VS Code\n"
    printf "│  5) Antigravity\n"
    printf "│  a) All (default)\n"
    printf "│  n) None (skip host integrations)\n"
    printf "│\n"
    printf "Choice [a]: "

    local selection
    read -r selection </dev/tty || selection="a"
    selection="${selection:-a}"
    echo ""

    case "$selection" in
        a|A|all|ALL)
            HOST_FLAGS=(--all)
            ;;
        n|N|none|NONE|skip|SKIP|0)
            ATELIER_NO_HOSTS=1
            ;;
        *)
            local token
            IFS=',' read -ra _choices <<<"$selection"
            for token in "${_choices[@]}"; do
                token="$(echo "$token" | xargs)"
                case "$token" in
                    1) HOST_FLAGS+=(--claude) ;;
                    2) HOST_FLAGS+=(--codex) ;;
                    3) HOST_FLAGS+=(--opencode) ;;
                    4) HOST_FLAGS+=(--copilot) ;;
                    5) HOST_FLAGS+=(--antigravity) ;;
                esac
            done
            [[ ${#HOST_FLAGS[@]} -gt 0 ]] || ATELIER_NO_HOSTS=1
            ;;
    esac

    [[ "$ATELIER_NO_HOSTS" == "1" ]] && return 0

    echo "◇  Apply agent configs to all your projects, or just this one?"
    echo "│  1) All projects (global)"
    echo "│  2) Just this project"
    printf "Choice [1]: "

    local scope_choice
    read -r scope_choice </dev/tty || scope_choice="1"
    scope_choice="${scope_choice:-1}"
    echo ""

    local scope="global"
    if [[ "$scope_choice" == "2" ]]; then
        HOST_SCOPE_ARGS=(--workspace .)
        scope="local"
    fi

    local wants_claude=0
    local flag
    for flag in "${HOST_FLAGS[@]+"${HOST_FLAGS[@]}"}"; do
        if [[ "$flag" == "--all" || "$flag" == "--claude" ]]; then
            wants_claude=1
            break
        fi
    done

    if [[ "$wants_claude" == "1" && "$scope" == "global" ]]; then
        echo "◇  Auto-allow Atelier commands? (Skips permission prompts in Claude Code)"
        printf "Choice [Y/n]: "
        local auto_allow
        read -r auto_allow </dev/tty || auto_allow="y"
        auto_allow="${auto_allow:-y}"
        echo ""
        case "$auto_allow" in
            [Nn]*) ;;
            *)
                HOST_EXTRA_ARGS=(--claude-project "$(pwd)")
                ;;
        esac
    fi
}

prompt_cli_install_choice() {
    [[ -t 0 && -t 1 ]] || return 0
    [[ "$ATELIER_DRY_RUN" == "1" ]] && return 0

    echo "◇  Install/update Atelier CLI on your PATH? (Required so agents can launch the MCP server)"
    printf "Choice [Y/n]: "
    local answer
    read -r answer </dev/tty || answer="y"
    answer="${answer:-y}"
    echo ""

    case "$answer" in
        [Nn]*) SKIP_CLI_INSTALL=1 ;;
        *) SKIP_CLI_INSTALL=0 ;;
    esac
}

prompt_zoekt_selection() {
    [[ -t 0 ]] || return 0
    [[ -n "$ATELIER_ZOEKT" ]] && return 0

    echo ""
    printf "%b[atelier-install]%b Enable persistent Zoekt code-search sidecar?\n" "$C_BOLD" "$C_RESET"
    printf "  0) No (default)\n"
    printf "  1) Yes (Docker)\n"
    printf "Choice [0/1, default: 0]: "
    local choice
    read -r choice </dev/tty
    echo ""

    case "$choice" in
        1) ATELIER_ZOEKT=1; ATELIER_ADVANCED=1 ;;
        *) ATELIER_ZOEKT="" ;;
    esac
}

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
        info "Updating existing repository in $ATELIER_INSTALL_DIR (force-overwrite local changes)"
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR fetch --tags --prune origin"
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR checkout -f $ATELIER_REF"
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR reset --hard origin/$ATELIER_REF"
            echo "[dry-run] git -C $ATELIER_INSTALL_DIR clean -fd"
        else
            git -C "$ATELIER_INSTALL_DIR" fetch --tags --prune origin
            git -C "$ATELIER_INSTALL_DIR" checkout -f "$ATELIER_REF"
            if git -C "$ATELIER_INSTALL_DIR" rev-parse --verify "origin/$ATELIER_REF" >/dev/null 2>&1; then
                git -C "$ATELIER_INSTALL_DIR" reset --hard "origin/$ATELIER_REF"
            else
                git -C "$ATELIER_INSTALL_DIR" reset --hard "$ATELIER_REF"
            fi
            git -C "$ATELIER_INSTALL_DIR" clean -fd
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

install_console_scripts() {
    local extras="mcp,memory,smart,cloud,repo-map,api,postgres,vector,parsers,rename,telemetry"
    local package_spec="${ATELIER_INSTALL_DIR}[${extras}]"
    local install_args=(tool install --quiet --force)

    if [[ "$ATELIER_LOCAL" == "1" ]]; then
        install_args+=(--editable)
    fi
    install_args+=("$package_spec")

    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        printf '[dry-run] UV_TOOL_BIN_DIR=%q UV_TOOL_DIR=%q uv' "$ATELIER_BIN_DIR" "$ATELIER_TOOL_DIR"
        printf ' %q' "${install_args[@]}"
        printf '\n'
        return
    fi

    mkdir -p "$ATELIER_BIN_DIR" "$ATELIER_TOOL_DIR"
    UV_TOOL_BIN_DIR="$ATELIER_BIN_DIR" \
        UV_TOOL_DIR="$ATELIER_TOOL_DIR" \
        uv "${install_args[@]}"

    local mcp_path="$ATELIER_BIN_DIR/atelier-mcp"
    local wrapped_path="$ATELIER_BIN_DIR/atelier-mcp.real"
    if [[ -f "$mcp_path" || -L "$mcp_path" ]]; then
        rm -f "$wrapped_path"
        mv "$mcp_path" "$wrapped_path"
        cat >"$mcp_path" <<EOF
#!/usr/bin/env bash
export ATELIER_DEV_MODE="\${ATELIER_DEV_MODE:-1}"
exec "$wrapped_path" "\$@"
EOF
        chmod +x "$mcp_path"
    fi
}

persist_install_record() {
    local record_dir
    record_dir="$(dirname "$ATELIER_INSTALL_RECORD")"

    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] mkdir -p $record_dir"
        echo "[dry-run] printf '%s\\n' '$ATELIER_INSTALL_DIR' > '$ATELIER_INSTALL_RECORD'"
        return
    fi

    mkdir -p "$record_dir"
    printf '%s\n' "$ATELIER_INSTALL_DIR" > "$ATELIER_INSTALL_RECORD"
}

install_code_tools() {
    # Install optional code-quality tools used by the post-edit hook pipeline and
    # the rename backend.  All steps are best-effort: missing tools are warned about
    # but do not abort the install.

    local os_type
    os_type="$(uname -s)"


    # prettier + eslint + ts-morph (TypeScript/JavaScript tools, require npm)
    if command -v npm >/dev/null 2>&1; then
        info "Installing prettier (JS/TS formatter)..."
        run npm install -g prettier
        info "Installing eslint, ts-morph, and typescript (JS/TS linter and rename backend)..."
        run npm install -g eslint ts-morph typescript
    else
        warn "npm not found — skipping prettier, eslint, and ts-morph (install Node.js 20+ to enable)"
    fi

    # rustfmt + cargo (Rust formatter and lint-fix backend, via rustup)
    if ! command -v cargo >/dev/null 2>&1; then
        info "cargo not found — installing Rust toolchain via rustup..."
        if [[ "$os_type" == "Darwin" ]]; then
            if command -v brew >/dev/null 2>&1; then
                run brew install rustup
                if [[ "$ATELIER_DRY_RUN" != "1" ]]; then
                    rustup-init -y --no-modify-path 2>/dev/null || true
                fi
            else
                warn "Homebrew not found — skipping Rust install on macOS (install from https://rustup.rs)"
            fi
        else
            # Linux
            if command -v curl >/dev/null 2>&1; then
                if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
                    echo "[dry-run] curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y"
                else
                    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --no-modify-path 2>/dev/null \
                        || warn "rustup install failed — Rust post-edit hooks will be skipped"
                fi
            else
                warn "curl not found — skipping Rust toolchain install"
            fi
        fi
    else
        info "Found cargo: $(cargo --version 2>/dev/null || echo unknown)"
    fi

}

main() {
    case "$(uname -s)" in
        Linux|Darwin) ;;
        *) fail "Unsupported OS: $(uname -s). This installer supports Linux/macOS." ;;
    esac

    need_cmd git
    need_cmd bash

    host_wizard
    prompt_cli_install_choice
    prompt_memory_selection
    prompt_zoekt_selection

    case "$ATELIER_MEMORY_BACKEND" in
        letta|openmemory|"") ;;
        *) fail "--memory must be 'letta' or 'openmemory', got: '$ATELIER_MEMORY_BACKEND'" ;;
    esac
    if [[ -n "$ATELIER_MEMORY_BACKEND" ]]; then
        ATELIER_ADVANCED=1
    fi

    install_uv_if_needed

    local stack_available=0
    if [[ "$ATELIER_NO_STACK" != "1" ]] && command -v npm >/dev/null 2>&1; then
        stack_available=1
    elif [[ "$ATELIER_NO_STACK" != "1" ]]; then
        warn "npm is required to run the optional visualization stack; skipping stack setup"
    fi

    local stack_expected=0
    if [[ "$ATELIER_NO_SERVICECTL" != "1" && "$stack_available" == "1" ]] && { command -v systemctl >/dev/null 2>&1 || [[ "$(uname -s)" == "Darwin" ]]; }; then
        stack_expected=1
    fi

    if [[ "$ATELIER_LOCAL" == "1" ]]; then
        info "Local mode: using current directory as an editable install source"
        ATELIER_INSTALL_DIR="$(pwd)"
    else
        prepare_repo
    fi
    export ATELIER_INSTALL_DIR

    if [[ "$SKIP_CLI_INSTALL" == "1" ]]; then
        warn "Skipped CLI install; installer will use existing 'atelier'/'atelier-mcp' on PATH."
    else
        info "Installing Atelier console commands..."
        install_console_scripts
        persist_install_record
    fi

    info "Installing optional code-quality tools (format, lint, rename)..."
    install_code_tools

    if command -v npm >/dev/null 2>&1; then
        info "Installing codeburn (token/cost reporting)..."
        run npm install -g codeburn
        info "Installing tokscale (token/cost reporting)..."
        run npm install -g tokscale
    else
        warn "npm not found — skipping codeburn and tokscale (install Node.js 20+ to enable)"
    fi

    local selected_memory=""
    if [[ "$ATELIER_ADVANCED" == "1" ]]; then
        if [[ -z "$ATELIER_MEMORY_BACKEND" ]]; then
            warn "--advanced set but no --memory selected; no memory sidecar will be installed"
        elif [[ "$ATELIER_MEMORY_BACKEND" == "letta" ]]; then
            if command -v docker >/dev/null 2>&1; then
                selected_memory="letta"
                info "Memory sidecar: Letta (Docker)"
            else
                warn "--memory letta requires Docker - skipping Letta sidecar"
            fi
        elif [[ "$ATELIER_MEMORY_BACKEND" == "openmemory" ]]; then
            local _om_missing=()
            command -v docker >/dev/null 2>&1 || _om_missing+=("docker")
            command -v git >/dev/null 2>&1 || _om_missing+=("git")
            command -v make >/dev/null 2>&1 || _om_missing+=("make")
            local _has_llm=0
            [[ -n "${ATELIER_OPENMEMORY_OPENAI_API_KEY:-}${OPENAI_API_KEY:-}" ]] && _has_llm=1
            command -v ollama >/dev/null 2>&1 && _has_llm=1
            [[ -n "${OLLAMA_HOST:-}" ]] && _has_llm=1
            [[ "$_has_llm" == "1" ]] || _om_missing+=("OPENAI_API_KEY or ollama")
            if [[ ${#_om_missing[@]} -gt 0 ]]; then
                warn "OpenMemory prerequisites missing (${_om_missing[*]}) - skipping memory sidecar"
            else
                selected_memory="openmemory"
                info "Memory sidecar: OpenMemory (Docker)"
            fi
        fi
    fi

    local selected_zoekt=""
    if [[ "$ATELIER_ZOEKT" == "1" ]]; then
        if command -v docker >/dev/null 2>&1; then
            selected_zoekt="1"
            info "Zoekt sidecar: enabled (Docker)"
        else
            warn "--zoekt requires Docker - skipping Zoekt sidecar"
        fi
    fi

    local memory_record="${HOME}/.atelier/memory_backend"
    if [[ -n "$selected_memory" ]]; then
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] printf '%s\\n' '$selected_memory' > '$memory_record'"
        else
            mkdir -p "${HOME}/.atelier"
            printf '%s\n' "$selected_memory" > "$memory_record"
        fi
    elif [[ -f "$memory_record" && "$ATELIER_DRY_RUN" != "1" ]]; then
        : >"$memory_record"
    fi

    local zoekt_record="${HOME}/.atelier/zoekt_enabled"
    if [[ "$selected_zoekt" == "1" ]]; then
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] printf '1\\n' > '$zoekt_record'"
        else
            mkdir -p "${HOME}/.atelier"
            printf '1\n' > "$zoekt_record"
        fi
    elif [[ -f "$zoekt_record" && "$ATELIER_DRY_RUN" != "1" ]]; then
        : >"$zoekt_record"
    fi

    # atelier-status was folded into `atelier status` — no separate binary needed

    if [[ ":$PATH:" != *":$ATELIER_BIN_DIR:"* ]]; then
        warn "$ATELIER_BIN_DIR is not currently on PATH"
        echo ""
        echo "Add this to your shell profile, then restart your shell:"
        echo "  export PATH=\"$ATELIER_BIN_DIR:\$PATH\""
        echo ""
    fi

    local atelier_cli="$ATELIER_BIN_DIR/atelier"
    if [[ "$SKIP_CLI_INSTALL" == "1" && ! -x "$atelier_cli" ]]; then
        atelier_cli="$(command -v atelier || true)"
        [[ -n "$atelier_cli" ]] || fail "'atelier' CLI is required but was not found on PATH."
    fi

    info "Initializing Atelier runtime store..."
    if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
        echo "[dry-run] $atelier_cli init"
    else
        "$atelier_cli" init >/dev/null
    fi

    # Persist the selected memory backend so uninstall knows what to clean up.
    local memory_record="${HOME}/.atelier/memory_backend"
    if [[ -n "$selected_memory" ]]; then
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] printf '%s\\n' '$selected_memory' > '$memory_record'"
        else
            mkdir -p "${HOME}/.atelier"
            printf '%s\n' "$selected_memory" > "$memory_record"
            info "Persisted memory backend: $selected_memory"
        fi
    elif [[ -f "$memory_record" && "$ATELIER_DRY_RUN" != "1" ]]; then
        # No sidecar selected on this run — clear any previous selection so
        # uninstall does not try to tear down a sidecar that is no longer managed.
        : > "$memory_record"
    fi

    if [[ "$ATELIER_NO_HOSTS" != "1" ]]; then
        info "Installing Atelier host integrations (skip if host CLI is missing)..."
        local host_install_args=()
        local passthrough
        for passthrough in "${PASSTHROUGH[@]+"${PASSTHROUGH[@]}"}"; do
            case "$passthrough" in
                --dry-run|--print-only|--strict)
                    host_install_args+=("$passthrough")
                    ;;
            esac
        done
        if [[ ${#HOST_FLAGS[@]} -gt 0 ]]; then
            host_install_args+=("${HOST_FLAGS[@]}")
        fi
        if [[ ${#HOST_SCOPE_ARGS[@]} -gt 0 ]]; then
            host_install_args+=("${HOST_SCOPE_ARGS[@]}")
        fi
        if [[ ${#HOST_EXTRA_ARGS[@]} -gt 0 ]]; then
            host_install_args+=("${HOST_EXTRA_ARGS[@]}")
        fi
        if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
            echo "[dry-run] bash $ATELIER_INSTALL_DIR/scripts/install_agent_clis.sh ${host_install_args[*]}"
        else
            local host_output host_output_file host_ret
            host_output_file="$(mktemp "${TMPDIR:-/tmp}/atelier-hosts.XXXXXX")"
            set +e
            if [[ -n "$C_RESET" ]]; then
                FORCE_COLOR=1 bash "$ATELIER_INSTALL_DIR/scripts/install_agent_clis.sh" "${host_install_args[@]}" 2>&1 | tee "$host_output_file"
            else
                bash "$ATELIER_INSTALL_DIR/scripts/install_agent_clis.sh" "${host_install_args[@]}" 2>&1 | tee "$host_output_file"
            fi
            host_ret=$?
            set -e
            host_output="$(cat "$host_output_file")"
            rm -f "$host_output_file"
            collect_issues_from_output "$host_output"
            if [[ $host_ret -ne 0 ]]; then
                ERRORS+=("One or more host integrations failed")
                FINAL_EXIT_CODE=1
            fi
        fi
        # Persist host detection results for the local service/UI surfaces
        if [[ "$ATELIER_DRY_RUN" != "1" && -f "$ATELIER_INSTALL_DIR/scripts/status.sh" ]]; then
            bash "$ATELIER_INSTALL_DIR/scripts/status.sh" --write 2>/dev/null \
                || degrade "Failed to persist host detection status"
        fi
    else
        info "Skipping host integrations because ATELIER_NO_HOSTS=1"
        # Still persist current detection state even when skipping install
        if [[ "$ATELIER_DRY_RUN" != "1" && -f "$ATELIER_INSTALL_DIR/scripts/status.sh" ]]; then
            bash "$ATELIER_INSTALL_DIR/scripts/status.sh" --write 2>/dev/null \
                || degrade "Failed to persist host detection status"
        fi
    fi

    if [[ "$ATELIER_NO_SERVICECTL" != "1" ]]; then
        if command -v systemctl >/dev/null 2>&1 || [[ "$(uname -s)" == "Darwin" ]]; then
            info "Registering Atelier services with background manager..."
            local background_args=()
            if [[ "$stack_available" == "1" ]]; then
                background_args+=("--with-stack")
            fi
            case "$selected_memory" in
                letta) background_args+=("--with-letta") ;;
                openmemory) background_args+=("--with-openmemory") ;;
            esac
            if [[ "$selected_zoekt" == "1" ]]; then
                background_args+=("--with-zoekt")
            fi

            if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
                echo "[dry-run] $ATELIER_BIN_DIR/atelier background install ${background_args[*]}"
            else
                "$ATELIER_BIN_DIR/atelier" background install "${background_args[@]}" >/dev/null
            fi
        else
            info "Starting Atelier background service controller (loose process)..."
            if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
                echo "[dry-run] $ATELIER_BIN_DIR/atelier servicectl start --interval-seconds $ATELIER_SERVICECTL_INTERVAL_SECONDS --maintenance-interval-seconds $ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS"
            else
                "$ATELIER_BIN_DIR/atelier" servicectl start \
                    --interval-seconds "$ATELIER_SERVICECTL_INTERVAL_SECONDS" \
                    --maintenance-interval-seconds "$ATELIER_SERVICECTL_MAINTENANCE_INTERVAL_SECONDS" >/dev/null
            fi

            if [[ "$stack_available" == "1" ]]; then
                info "Starting Atelier visualization stack (service + frontend)..."
                if [[ "$ATELIER_DRY_RUN" == "1" ]]; then
                    echo "[dry-run] $ATELIER_BIN_DIR/atelier stack start"
                else
                    "$ATELIER_BIN_DIR/atelier" stack start \
                        && STACK_STARTED=1 \
                        || degrade "Visualization stack did not start cleanly"
                fi
            fi
        fi
    else
        info "Skipping background services because ATELIER_NO_SERVICECTL=1"
    fi

    if [[ "$STACK_STARTED" == "1" || "$stack_expected" == "1" ]]; then
        echo "  Visualization stack is running:"
        echo "    frontend: http://localhost:3125"
        echo "    service:  http://localhost:8787"
        echo ""
    fi
    echo "  Commands:"
    echo "    atelier --version           - Check core CLI version"
    echo "    atelier-mcp --version       - Check MCP server version"
    echo "    atelier background status   - View background service status"
    echo "    atelier stack start         - Start production API and frontend (requires npm)"
    echo "    atelier stack stop          - Stop the visualization stack"
    echo "    atelier stack logs          - View stack logs"
    echo "    atelier status              - Show one-line status of the active reasoning run"
    echo "    atelier import              - Import agent sessions from all available history sources (CLI, VS Code, etc.)"
    echo ""
    echo "  Docker sidecars (available with --advanced, require Docker):"
    echo "    atelier letta up/down       - Start/stop the Letta memory server container"
    echo "    atelier openmemory up/down  - Start/stop the OpenMemory MCP container"
    echo "    atelier letta status        - Check Letta health"
    echo "    atelier openmemory status   - Check OpenMemory container status"
    if [[ "$ATELIER_ADVANCED" != "1" ]]; then
        echo ""
        echo "  Tip: re-run with --advanced to install Letta + OpenMemory Docker sidecars."
    fi
    echo ""
    echo "  Memory sidecar (Docker, opt-in via --advanced --memory <backend>):"
    case "$selected_memory" in
        letta) echo "    ACTIVE: Letta - atelier letta up/down/status/reset" ;;
        openmemory) echo "    ACTIVE: OpenMemory - atelier openmemory up/down/status/logs" ;;
        *)
            echo "    None selected."
            echo "      --advanced --memory letta"
            echo "      --advanced --memory openmemory"
            ;;
    esac
    echo ""
    echo "  Zoekt code-search sidecar (Docker, opt-in via --zoekt):"
    if [[ "$selected_zoekt" == "1" ]]; then
        echo "    ACTIVE - atelier zoekt up/down/status/reindex/reset"
    else
        echo "    Not enabled. Re-run with --zoekt."
    fi

    print_final_report
    if [[ ${#ERRORS[@]} -gt 0 ]]; then
        info "${C_BOLD}${C_RED}Completed with errors.${C_RESET}"
    elif [[ ${#WARNINGS[@]} -gt 0 ]]; then
        info "${C_BOLD}${C_YELLOW}Completed with warnings.${C_RESET}"
    else
        info "Installation complete."
    fi

    return "$FINAL_EXIT_CODE"
}

main "$@"
