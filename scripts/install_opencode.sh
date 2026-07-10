#!/usr/bin/env bash
# install_opencode.sh - Install Atelier into opencode
#
# What it does:
#   Global mode: installs opencode user config and user agent under ~/.config/opencode.
#   Workspace mode (--workspace DIR): installs project-local opencode artifacts under DIR.
#   Config merge includes both:
#     - MCP server entry (atelier mcp)
#     - OpenAI-compatible provider entry (Atelier service /v1 endpoint)
#
# Options:
#   --dry-run      Print what would happen, touch nothing
#   --print-only   Print config snippet for manual install, touch nothing
#   --workspace DIR  Install project-local artifacts into DIR instead of global user config
#   --strict       Exit nonzero if 'opencode' CLI not on PATH

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELIER_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
source "${SCRIPT_DIR}/lib/managed_context.sh"

DRY_RUN=false
PRINT_ONLY=false
STRICT=false
WORKSPACE=""
WORKSPACE_SET=false
ROLES="code"            # comma-separated role ids to install (--roles=code,explore,...)

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run)    DRY_RUN=true ;;
        --print-only) PRINT_ONLY=true ;;
        --strict)     STRICT=true ;;
        --workspace)
            if [ $# -lt 2 ]; then
                echo "Missing value for --workspace" >&2
                exit 1
            fi
            WORKSPACE="$2"
            WORKSPACE_SET=true
            shift
            ;;
        --roles)
            if [ $# -lt 2 ]; then
                echo "Missing value for --roles" >&2
                exit 1
            fi
            ROLES="$2"
            shift
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

if $WORKSPACE_SET; then
    WORKSPACE="$(cd "$WORKSPACE" && pwd)"
fi

OPENCODE_CONFIG_HOME="${OPENCODE_CONFIG_HOME:-${XDG_CONFIG_HOME:-${HOME}/.config}/opencode}"
if $WORKSPACE_SET; then
    INSTALL_SCOPE="workspace"
    OC_FILE="${WORKSPACE}/opencode.json"
    AGENT_DEST_DIR="${WORKSPACE}/.opencode/agents"
    PLUGIN_DEST_DIR="${WORKSPACE}/.opencode/plugins"
    else
    INSTALL_SCOPE="global"
    OC_FILE="${OPENCODE_CONFIG_HOME}/opencode.json"
    AGENT_DEST_DIR="${OPENCODE_CONFIG_HOME}/agents"
    PLUGIN_DEST_DIR="${OPENCODE_CONFIG_HOME}/plugins"
    fi

ATELIER_SERVICE_BASE="${ATELIER_SERVICE_URL:-http://127.0.0.1:8787}"
ATELIER_SERVICE_BASE="${ATELIER_SERVICE_BASE%/}"
if [[ "$ATELIER_SERVICE_BASE" == */v1 ]]; then
    ATELIER_OPENAI_BASE="$ATELIER_SERVICE_BASE"
else
    ATELIER_OPENAI_BASE="${ATELIER_SERVICE_BASE}/v1"
fi

info()  { [[ "${ATELIER_VERBOSE:-0}" == "1" ]] && echo "[atelier:opencode] $*" || true; }
warn()  { echo "[atelier:opencode] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }
if command -v uv >/dev/null 2>&1; then
    PYTHON_CMD=(uv run python)
else
    PYTHON_CMD=(python3)
fi
backup_file() {
    local f="$1"
    if $WORKSPACE_SET; then
        return
    fi
    if [ -f "$f" ]; then
        local bk="${f}.atelier-backup.$(date +%Y%m%dT%H%M%S)"
        run "cp $(printf %q "$f") $(printf %q "$bk")"
        info "backed up $f -> $bk"
    fi
}

if $WORKSPACE_SET; then
    NEW_ENTRY=$(cat <<JSON
{
  "default_agent": "atelier",
  "permission": {
    "atelier_*": "allow"
  },
  "provider": {
    "atelier": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Atelier",
      "options": {
        "baseURL": "${ATELIER_OPENAI_BASE}",
        "apiKey": "local"
      }
    }
  },
  "mcp": {
      "atelier": {
        "type": "local",
        "command": ["atelier", "mcp", "--host", "opencode"],
        "environment": {
          "ATELIER_WORKSPACE_ROOT": "${WORKSPACE}"
        }
      }
  }
}
JSON
)
else
    NEW_ENTRY=$(cat <<JSON
{
  "default_agent": "atelier",
  "permission": {
    "atelier_*": "allow"
  },
  "provider": {
    "atelier": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Atelier",
      "options": {
        "baseURL": "${ATELIER_OPENAI_BASE}",
        "apiKey": "local"
      }
    }
  },
  "mcp": {
    "atelier": {
      "type": "local",
      "command": ["atelier", "mcp", "--host", "opencode"]
    }
  }
}
JSON
)
fi

# ---- print-only mode --------------------------------------------------------
if $PRINT_ONLY; then
    echo ""
    echo "=== Atelier opencode - Manual Install ==="
    echo ""
    echo "Scope: ${INSTALL_SCOPE}"
    echo "Config target: ${OC_FILE}"
    echo "Agent target: ${AGENT_DEST_DIR}/atelier.md"
    echo ""
    echo "Merge/create config:"
    echo "$NEW_ENTRY"
    exit 0
fi

# ---- check CLI --------------------------------------------------------------
if ! command -v opencode &>/dev/null; then
    if $STRICT; then
        echo "[atelier:opencode] ERROR: 'opencode' not found. Install from https://opencode.ai" >&2
        exit 1
    fi
    warn "'opencode' not found - SKIPPING. Install from https://opencode.ai"
    echo "=== SKIPPED (opencode CLI absent) ==="
    exit 0
fi
info "Found opencode: $(opencode --version 2>/dev/null || echo 'version unknown')"

# ---- merge opencode config --------------------------------------------------
run "mkdir -p $(printf %q "$(dirname "$OC_FILE")")"

if [ -f "$OC_FILE" ]; then
    backup_file "$OC_FILE"
    if $DRY_RUN; then
        echo "  [dry-run] merge atelier into $OC_FILE"
    else
        ATELIER_OC_FILE="$OC_FILE" "${PYTHON_CMD[@]}" - <<PYEOF
import json
import os
import re
from pathlib import Path

path = Path(os.environ['ATELIER_OC_FILE'])
content = path.read_text(encoding='utf-8').strip()
stripped = re.sub(r'^\s*//.*', '', content, flags=re.M)
existing = json.loads(stripped) if stripped.strip() else {}
new_entry = json.loads('''$NEW_ENTRY''')
existing.setdefault('mcp', {}).update(new_entry['mcp'])
existing.setdefault('provider', {}).update(new_entry['provider'])
existing['default_agent'] = new_entry['default_agent']
existing.pop('model', None)
existing.setdefault('permission', {}).update(new_entry['permission'])
path.write_text(json.dumps(existing, indent=2) + '\n', encoding='utf-8')
print(f"[atelier:opencode] merged atelier entry into {path}")
PYEOF
    fi
else
    if $DRY_RUN; then
        echo "  [dry-run] create $OC_FILE"
    else
        echo "$NEW_ENTRY" > "$OC_FILE"
        info "created $OC_FILE"
    fi
fi

# ---- install opencode atelier agents ---------------------------------------
if $WORKSPACE_SET; then
    if $DRY_RUN; then
        echo "  [dry-run] project workspace-local OpenCode agents (${ROLES}) into '$AGENT_DEST_DIR'"
    else
        PYTHONPATH="${ATELIER_REPO}/src${PYTHONPATH:+:${PYTHONPATH}}" "${PYTHON_CMD[@]}" - <<PYEOF
from pathlib import Path
from atelier.core.capabilities.workspace_host_overrides import write_workspace_opencode_agents

written = write_workspace_opencode_agents(Path("${WORKSPACE}"), repo_root=Path("${ATELIER_REPO}"), role_ids=tuple(r for r in "${ROLES}".split(",") if r))
print(f"[atelier:opencode] projected {len(written)} workspace-local OpenCode agents into ${AGENT_DEST_DIR}")
PYEOF
    fi
elif [[ "$ROLES" == "code" ]]; then
    AGENT_SRC="${ATELIER_REPO}/integrations/opencode/agents/atelier.md"

    STAGING_DIR="${HOME}/.atelier/opencode"
    run "mkdir -p $(printf %q "$STAGING_DIR")"
    info "Staging opencode agent instructions"
    atelier_write_managed_copy "${AGENT_SRC}" "$STAGING_DIR/atelier.md" "$DRY_RUN"
    AGENT_SRC="$STAGING_DIR/atelier.md"

    if $DRY_RUN; then
        echo "  [dry-run] copy '$AGENT_SRC' to '$AGENT_DEST_DIR/atelier.md'"
    elif [ -f "$AGENT_SRC" ]; then
        run "mkdir -p $(printf %q "$AGENT_DEST_DIR")"
        run "cp -f $(printf %q "$AGENT_SRC") $(printf %q "$AGENT_DEST_DIR/atelier.md")"
        info "atelier agent installed -> $AGENT_DEST_DIR/atelier.md"
    else
        warn "agent source missing: $AGENT_SRC"
    fi
else
    # On-demand extra roles requested: migrate the global agent set from the
    # single legacy atelier.md (code only) to per-role atelier.<role>.md files
    # (mirrors workspace-mode naming), and point default_agent at atelier.code
    # since the primary agent's identity is now the per-role filename.
    if $DRY_RUN; then
        echo "  [dry-run] project global OpenCode agents (${ROLES}) into '$AGENT_DEST_DIR', set default_agent=atelier.code"
    else
        PYTHONPATH="${ATELIER_REPO}/src${PYTHONPATH:+:${PYTHONPATH}}" "${PYTHON_CMD[@]}" - <<PYEOF
from pathlib import Path
from atelier.core.capabilities.workspace_host_overrides import write_opencode_agents

written = write_opencode_agents(Path("${AGENT_DEST_DIR}"), repo_root=Path("${ATELIER_REPO}"), role_ids=tuple(r for r in "${ROLES}".split(",") if r))
print(f"[atelier:opencode] projected {len(written)} global OpenCode agents into ${AGENT_DEST_DIR}")
PYEOF
        "${PYTHON_CMD[@]}" - <<PYEOF2
import json
from pathlib import Path
path = Path("${OC_FILE}")
data = json.loads(path.read_text(encoding="utf-8") or "{}") if path.exists() else {}
data["default_agent"] = "atelier.code"
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF2
        info "default_agent set -> atelier.code (per-role agents installed)"
    fi
fi

# ---- install prompt-time nudge plugin ---------------------------------------
PLUGIN_SRC_DIR="${ATELIER_REPO}/integrations/opencode/plugins"
if $DRY_RUN; then
    echo "  [dry-run] copy Atelier nudge plugin to '$PLUGIN_DEST_DIR'"
else
    run "mkdir -p $(printf %q "$PLUGIN_DEST_DIR")"
    run "cp -f $(printf %q "$PLUGIN_SRC_DIR/atelier-nudge.js") $(printf %q "$PLUGIN_DEST_DIR/atelier-nudge.js")"
    run "cp -f $(printf %q "$PLUGIN_SRC_DIR/atelier_nudge.py") $(printf %q "$PLUGIN_DEST_DIR/atelier_nudge.py")"
    info "Atelier nudge plugin installed -> $PLUGIN_DEST_DIR/atelier-nudge.js"
fi

if $WORKSPACE_SET; then
    atelier_install_attribution_hook "$WORKSPACE" "$DRY_RUN"
fi

if $DRY_RUN; then
    info "Dry run complete; skipped post-install verification because no files were written."
    exit 0
fi

# ---- post-install verification ---------------------------------------------
info "Running post-install verification..."
VFAIL=0
vpass() { info "PASS: $*"; }
vfail() { echo "[atelier:opencode] FAIL: $*" >&2; VFAIL=1; }

if [ -f "$OC_FILE" ]; then
    HAS=$(ATELIER_OC_FILE="$OC_FILE" "${PYTHON_CMD[@]}" - <<PYEOF
import json
import os
import re
from pathlib import Path

content = Path(os.environ['ATELIER_OC_FILE']).read_text(encoding='utf-8')
stripped = re.sub(r'^\s*//.*', '', content, flags=re.M)
try:
    d = json.loads(stripped)
    print('yes' if 'atelier' in d.get('mcp', {}) else 'no')
except Exception:
    print('parse-error')
PYEOF
)
    if [ "$HAS" = "yes" ]; then
        vpass "opencode config contains atelier MCP entry ($OC_FILE)"
    elif [ "$HAS" = "parse-error" ]; then
        vfail "opencode config parse error: $OC_FILE"
    else
        vfail "opencode config missing atelier entry"
    fi

    DEFAULT_AGENT=$(ATELIER_OC_FILE="$OC_FILE" "${PYTHON_CMD[@]}" - <<PYEOF
import json
import os
import re
from pathlib import Path

content = Path(os.environ['ATELIER_OC_FILE']).read_text(encoding='utf-8')
stripped = re.sub(r'^\s*//.*', '', content, flags=re.M)
try:
    d = json.loads(stripped)
    print(d.get('default_agent', ''))
except Exception:
    print('')
PYEOF
)
    if [ "$DEFAULT_AGENT" = "atelier" ] || { [ "$INSTALL_SCOPE" = "global" ] && [ "$ROLES" != "code" ] && [ "$DEFAULT_AGENT" = "atelier.code" ]; }; then
        vpass "opencode default_agent = $DEFAULT_AGENT"
    else
        vfail "opencode default_agent is '$DEFAULT_AGENT' (expected 'atelier' or 'atelier.code')"
    fi

    HAS_PROVIDER=$(ATELIER_OC_FILE="$OC_FILE" "${PYTHON_CMD[@]}" - <<PYEOF
import json
import os
import re
from pathlib import Path

content = Path(os.environ['ATELIER_OC_FILE']).read_text(encoding='utf-8')
stripped = re.sub(r'^\s*//.*', '', content, flags=re.M)
try:
    d = json.loads(stripped)
    provider = d.get('provider', {}).get('atelier', {})
    base_url = provider.get('options', {}).get('baseURL')
    print('yes' if provider and base_url else 'no')
except Exception:
    print('parse-error')
PYEOF
)
    if [ "$HAS_PROVIDER" = "yes" ]; then
        vpass "opencode provider.atelier and model are configured for Atelier OpenAI gateway"
    elif [ "$HAS_PROVIDER" = "parse-error" ]; then
        vfail "opencode config parse error while validating provider settings"
    else
        vfail "opencode provider/model config for Atelier gateway is missing"
    fi
else
    vfail "opencode config not found: $OC_FILE"
fi

PLUGIN_FILE="${PLUGIN_DEST_DIR}/atelier-nudge.js"
PLUGIN_HELPER="${PLUGIN_DEST_DIR}/atelier_nudge.py"
if [ -f "$PLUGIN_FILE" ] && [ -f "$PLUGIN_HELPER" ]; then
    vpass "opencode Atelier prompt nudge plugin installed: $PLUGIN_FILE"
else
    vfail "opencode Atelier prompt nudge plugin missing from $PLUGIN_DEST_DIR"
fi

# Global mode installs a single primary agent as atelier.md; workspace mode
# projects per-role files (atelier.<role>.md) with atelier.code.md as the
# primary, so verify the name the writer actually produces for this scope.
if [ "$INSTALL_SCOPE" = "workspace" ] || [[ "$ROLES" != "code" ]]; then
    AGENT_FILE="${AGENT_DEST_DIR}/atelier.code.md"
else
    AGENT_FILE="${AGENT_DEST_DIR}/atelier.md"
fi
if [ -f "$AGENT_FILE" ]; then
    vpass "opencode atelier agent installed: $AGENT_FILE"
else
    vfail "opencode atelier agent missing: $AGENT_FILE"
fi

if command -v atelier &>/dev/null; then
    vpass "atelier is available on PATH"
else
    vfail "atelier NOT found on PATH"
fi

if command -v atelier >/dev/null 2>&1 && atelier status --help >/dev/null 2>&1; then
    vpass "atelier status command is available"
else
    vfail "atelier status command unavailable"
fi

if [ "$VFAIL" -ne 0 ]; then
    echo "[atelier:opencode] ERROR: post-install verification failed." >&2
    exit 1
fi
info "All post-install checks passed"

info "Done. Restart opencode - Atelier agent and MCP are available."
info "Tip: run 'atelier status' in any shell to see the runs dashboard."
