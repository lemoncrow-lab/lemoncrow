#!/usr/bin/env bash
# install_hermes.sh - Install LemonCrow into Hermes Agent
#
# What it does:
#   Adds LemonCrow to $HERMES_HOME/config.yaml (defaults to ~/.hermes/config.yaml).
#   Merges mcp_servers.lemoncrow entry and adds mcp-lemoncrow to platform_toolsets.cli.
#
# Options:
#   --dry-run      Print what would happen, touch nothing
#   --print-only   Print config snippet for manual install, touch nothing
#   --strict       Exit nonzero if hermes config file not found
#   --workspace    Not supported (Hermes is global-only)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEMONCROW_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
source "${SCRIPT_DIR}/lib/managed_context.sh"

DRY_RUN=false
PRINT_ONLY=false
STRICT=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=true ;;
        --print-only) PRINT_ONLY=true ;;
        --strict)     STRICT=true ;;
        --workspace)
            # Hermes Agent is global-only; a workspace-scoped install/verify
            # sweep (verify_agent_clis.sh --workspace DIR) must skip, not fail.
            echo "[lemoncrow:hermes] WARN: --workspace not supported (Hermes is global-only)" >&2
            echo "=== SKIPPED (workspace mode unsupported) ==="
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

HERMES_HOME="${HERMES_HOME:-${HOME}/.hermes}"
CONFIG_FILE="${HERMES_HOME}/config.yaml"

info()  { [[ "${LEMONCROW_VERBOSE:-0}" == "1" ]] && echo "[lemoncrow:hermes] $*" || true; }
warn()  { echo "[lemoncrow:hermes] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }
# PyYAML is not guaranteed in the system python; prefer the project env.
if command -v uv >/dev/null 2>&1; then
    PYTHON_CMD=(uv run python)
else
    PYTHON_CMD=(python3)
fi
backup_file() {
    local f="$1"
    if [ -f "$f" ]; then
        local bk="${f}.lemoncrow-backup.$(date +%Y%m%dT%H%M%S)"
        run "cp $(printf %q "$f") $(printf %q "$bk")"
        info "backed up $f -> $bk"
    fi
}

# ---- print-only mode --------------------------------------------------------
if $PRINT_ONLY; then
    echo ""
    echo "=== LemonCrow Hermes Agent - Manual Install ==="
    echo ""
    echo "Config target: ${CONFIG_FILE}"
    echo ""
    echo "Add to mcp_servers:"
    echo "  mcp_servers:"
    echo "    lc:"
    echo "      command: lemoncrow"
    echo "      args:"
    echo "        - mcp"
    echo "        - --host"
    echo "        - hermes"
    echo "      timeout: 120"
    echo "      connect_timeout: 60"
    echo "      enabled: true"
    echo ""
    echo "Add mcp-lemoncrow to platform_toolsets.cli:"
    echo "  platform_toolsets:"
    echo "    cli:"
    echo "      - mcp-lemoncrow"
    echo "      - hermes-cli"
    exit 0
fi

# ---- check hermes installation ----------------------------------------------
if [ ! -f "$CONFIG_FILE" ]; then
    if $STRICT; then
        echo "[lemoncrow:hermes] ERROR: Hermes config not found at $CONFIG_FILE" >&2
        exit 1
    fi
    warn "Hermes config not found at $CONFIG_FILE - creating default config"
    run "mkdir -p $(printf %q "$HERMES_HOME")"
    if ! $DRY_RUN; then
        cat > "$CONFIG_FILE" <<YAML
# Hermes Agent configuration

mcp_servers:
  lc:
    command: lemoncrow
    args:
      - mcp
      - --host
      - hermes
    timeout: 120
    connect_timeout: 60
    enabled: true

platform_toolsets:
  cli:
    - mcp-lemoncrow
    - hermes-cli
YAML
        info "created default config at $CONFIG_FILE"
    fi
    if $DRY_RUN; then
        echo "  [dry-run] create $CONFIG_FILE with LemonCrow mcp_servers entry"
    fi
    echo "=== CREATED ==="
    exit 0
fi

# ---- backup and merge config ------------------------------------------------
backup_file "$CONFIG_FILE"

if $DRY_RUN; then
    echo "  [dry-run] merge LemonCrow into $CONFIG_FILE"
else
    "${PYTHON_CMD[@]}" - <<PYEOF
import yaml
from pathlib import Path

path = Path('$CONFIG_FILE')
content = path.read_text(encoding='utf-8')
config = yaml.safe_load(content) or {}

# Add MCP server entry
config.setdefault('mcp_servers', {})
config['mcp_servers']['lemoncrow'] = {
    'command': 'lc',
    'args': ['mcp', '--host', 'hermes'],
    'timeout': 120,
    'connect_timeout': 60,
    'enabled': True,
}

# Add toolset entry
config.setdefault('platform_toolsets', {})
toolsets = config['platform_toolsets'].setdefault('cli', [])
toolsets = [item for item in toolsets if item != 'mcp-lemoncrow']
toolsets.insert(0, 'mcp-lemoncrow')
config['platform_toolsets']['cli'] = toolsets

with path.open('w', encoding='utf-8') as f:
    yaml.dump(config, f, default_flow_style=False, sort_keys=False)
print("[lemoncrow:hermes] merged LemonCrow into $CONFIG_FILE")
PYEOF
fi

if $DRY_RUN; then
    info "Dry run complete; skipped post-install verification because no files were written."
    exit 0
fi

# ---- post-install verification ---------------------------------------------
info "Running post-install verification..."
VFAIL=0
vpass() { info "PASS: $*"; }
vwarn() { echo "[lemoncrow:hermes] WARN: $*" >&2; }
vfail() { echo "[lemoncrow:hermes] FAIL: $*" >&2; VFAIL=1; }

if [ -f "$CONFIG_FILE" ]; then
    HAS=$("${PYTHON_CMD[@]}" - <<PYEOF
import yaml
from pathlib import Path
try:
    d = yaml.safe_load(Path('$CONFIG_FILE').read_text(encoding='utf-8')) or {}
    has_mcp = 'lemoncrow' in d.get('mcp_servers', {})
    toolsets = d.get('platform_toolsets', {}).get('cli', [])
    has_toolset = 'mcp-lemoncrow' in toolsets
    print(f"{'mcp' if has_mcp else ''} {'toolset' if has_toolset else ''}".strip() or 'none')
except Exception:
    print('parse-error')
PYEOF
)
    if [ "$HAS" = "mcp toolset" ] || [ "$HAS" = "toolset mcp" ]; then
        vpass "Hermes config contains LemonCrow MCP and toolset entry"
    elif [ "$HAS" = "mcp" ]; then
        vwarn "Hermes config missing mcp-lemoncrow in platform_toolsets.cli"
    elif [ "$HAS" = "toolset" ]; then
        vwarn "Hermes config missing LemonCrow in mcp_servers"
    elif [ "$HAS" = "parse-error" ]; then
        vfail "Hermes config parse error: $CONFIG_FILE"
    else
        vfail "Hermes config missing LemonCrow entries"
    fi
else
    vfail "Hermes config not found: $CONFIG_FILE"
fi

if command -v lc &>/dev/null; then
    vpass "lc is available on PATH"
else
    vfail "lc NOT found on PATH"
fi

if [ "$VFAIL" -ne 0 ]; then
    echo "[lemoncrow:hermes] ERROR: post-install verification failed." >&2
    exit 1
fi
info "All post-install checks passed"

info "Done. Start a new Hermes session for MCP changes to take effect."
info "Tip: run 'lc status' in any shell to see the runs dashboard."
