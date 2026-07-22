#!/usr/bin/env bash
# install_opencode.sh - Install LemonCrow into opencode
#
# What it does:
#   Global mode: installs opencode user config and user agent under ~/.config/opencode.
#   Workspace mode (--workspace DIR): installs project-local opencode artifacts under DIR.
#   Config merge includes both:
#     - MCP server entry (lc mcp)
#     - OpenAI-compatible provider entry (LemonCrow service /v1 endpoint)
#
# Options:
#   --dry-run      Print what would happen, touch nothing
#   --print-only   Print config snippet for manual install, touch nothing
#   --workspace DIR  Install project-local artifacts into DIR instead of global user config
#   --strict       Exit nonzero if 'opencode' CLI not on PATH

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEMONCROW_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
source "${SCRIPT_DIR}/lib/managed_context.sh"

# Resolve the Python binary early (before option parsing) so DIST_ROOT
# can use it to locate the installed lemoncrow package.
if [[ -x "${LEMONCROW_TOOL_DIR:-${HOME}/.lemoncrow/uv-tools}/lemoncrow/bin/python" ]]; then
    DIST_PYTHON=("${LEMONCROW_TOOL_DIR:-${HOME}/.lemoncrow/uv-tools}/lemoncrow/bin/python")
elif command -v uv >/dev/null 2>&1; then
    DIST_PYTHON=(uv run python)
else
    DIST_PYTHON=(python3)
fi

# In a distribution package, LEMONCROW_REPO points at an ephemeral extracted
# directory that does NOT contain integrations/ or src/.  Resolve the actual
# distribution root from the installed lemoncrow package so that agent and
# plugin asset copies work regardless of where the script was launched from.
if [[ -d "${LEMONCROW_REPO}/integrations" ]]; then
    DIST_ROOT="${LEMONCROW_REPO}"
else
    DIST_ROOT="$(${DIST_PYTHON[@]} - <<'_PY'
import importlib.resources
from pathlib import Path
print(Path(str(importlib.resources.files("lemoncrow"))))
_PY
    )"
    if [[ -z "$DIST_ROOT" || ! -d "$DIST_ROOT/integrations" ]]; then
        echo "[lemoncrow:opencode] ERROR: cannot locate lemoncrow distribution assets" >&2
        exit 1
    fi
fi

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

LEMONCROW_SERVICE_BASE="${LEMONCROW_SERVICE_URL:-http://127.0.0.1:8787}"
LEMONCROW_SERVICE_BASE="${LEMONCROW_SERVICE_BASE%/}"
if [[ "$LEMONCROW_SERVICE_BASE" == */v1 ]]; then
    LEMONCROW_OPENAI_BASE="$LEMONCROW_SERVICE_BASE"
else
    LEMONCROW_OPENAI_BASE="${LEMONCROW_SERVICE_BASE}/v1"
fi

info()  { [[ "${LEMONCROW_VERBOSE:-0}" == "1" ]] && echo "[lemoncrow:opencode] $*" || true; }
warn()  { echo "[lemoncrow:opencode] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }
PYTHON_CMD=("${DIST_PYTHON[@]}")
backup_file() {
    local f="$1"
    if $WORKSPACE_SET; then
        return
    fi
    if [ -f "$f" ]; then
        local bk="${f}.lemoncrow-backup.$(date +%Y%m%dT%H%M%S)"
        run "cp $(printf %q "$f") $(printf %q "$bk")"
        info "backed up $f -> $bk"
    fi
}

if $WORKSPACE_SET; then
    NEW_ENTRY=$(cat <<JSON
{
  "default_agent": "code",
  "permission": {
    "lc_*": "allow",
    "read": "deny",
    "edit": "deny",
    "grep": "deny",
    "glob": "deny",
    "list": "deny",
    "bash": "deny",
    "webfetch": "deny",
    "lsp": "deny"
  },
  "provider": {
    "lc": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LemonCrow",
      "options": {
        "baseURL": "${LEMONCROW_OPENAI_BASE}",
        "apiKey": "local"
      }
    }
  },
  "mcp": {
      "lc": {
        "type": "local",
        "command": ["lemoncrow", "mcp", "--host", "opencode"],
        "environment": {
          "LEMONCROW_WORKSPACE_ROOT": "${WORKSPACE}"
        }
      }
  }
}
JSON
)
else
    NEW_ENTRY=$(cat <<JSON
{
  "default_agent": "code",
  "permission": {
    "lc_*": "allow",
    "read": "deny",
    "edit": "deny",
    "grep": "deny",
    "glob": "deny",
    "list": "deny",
    "bash": "deny",
    "webfetch": "deny",
    "lsp": "deny"
  },
  "provider": {
    "lc": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "LemonCrow",
      "options": {
        "baseURL": "${LEMONCROW_OPENAI_BASE}",
        "apiKey": "local"
      }
    }
  },
  "mcp": {
    "lc": {
      "type": "local",
      "command": ["lemoncrow", "mcp", "--host", "opencode"]
    }
  }
}
JSON
)
fi

# ---- print-only mode --------------------------------------------------------
if $PRINT_ONLY; then
    echo ""
    echo "=== LemonCrow opencode - Manual Install ==="
    echo ""
    echo "Scope: ${INSTALL_SCOPE}"
    echo "Config target: ${OC_FILE}"
    echo "Agent target: ${AGENT_DEST_DIR}/code.md"
    echo ""
    echo "Merge/create config:"
    echo "$NEW_ENTRY"
    exit 0
fi

# ---- check CLI --------------------------------------------------------------
if ! command -v opencode &>/dev/null; then
    if $STRICT; then
        echo "[lemoncrow:opencode] ERROR: 'opencode' not found. Install from https://opencode.ai" >&2
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
        echo "  [dry-run] merge lc into $OC_FILE"
    else
        LEMONCROW_OC_FILE="$OC_FILE" "${PYTHON_CMD[@]}" - <<PYEOF
import json
import os
import re
from pathlib import Path

path = Path(os.environ['LEMONCROW_OC_FILE'])
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
print(f"[lemoncrow:opencode] merged lc entry into {path}")
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

# ---- install opencode lemoncrow agents ---------------------------------------
if $WORKSPACE_SET; then
    if $DRY_RUN; then
        echo "  [dry-run] project workspace-local OpenCode agents (${ROLES}) into '$AGENT_DEST_DIR'"
    else
        "${PYTHON_CMD[@]}" - <<PYEOF
from pathlib import Path
from lemoncrow.core.capabilities.workspace_host_overrides import write_workspace_opencode_agents

written = write_workspace_opencode_agents(Path("${WORKSPACE}"), role_ids=tuple(r for r in "${ROLES}".split(",") if r))
print(f"[lemoncrow:opencode] projected {len(written)} workspace-local OpenCode agents into ${AGENT_DEST_DIR}")
PYEOF
    fi
elif [[ "$ROLES" == "code" ]]; then
    AGENT_SRC="${DIST_ROOT}/integrations/opencode/agents/code.md"

    STAGING_DIR="${HOME}/.lemoncrow/opencode"
    run "mkdir -p $(printf %q "$STAGING_DIR")"
    info "Staging opencode agent instructions"
    lemoncrow_write_managed_copy "${AGENT_SRC}" "$STAGING_DIR/code.md" "$DRY_RUN"
    AGENT_SRC="$STAGING_DIR/code.md"

    # Clean up the pre-rename bare filename so it doesn't linger alongside code.md.
    run "rm -f $(printf %q "$AGENT_DEST_DIR/lemoncrow.md")"

    if $DRY_RUN; then
        echo "  [dry-run] copy '$AGENT_SRC' to '$AGENT_DEST_DIR/code.md'"
    elif [ -f "$AGENT_SRC" ]; then
        run "mkdir -p $(printf %q "$AGENT_DEST_DIR")"
        run "cp -f $(printf %q "$AGENT_SRC") $(printf %q "$AGENT_DEST_DIR/code.md")"
        info "code agent installed -> $AGENT_DEST_DIR/code.md"
    else
        warn "agent source missing: $AGENT_SRC"
    fi
else
    # On-demand extra roles requested: migrate the global agent set from the
    # single legacy code.md (code only) to per-role lemoncrow.<role>.md files
    # (mirrors workspace-mode naming), and point default_agent at lemoncrow.code
    # since the primary agent's identity is now the per-role filename.
    if $DRY_RUN; then
        echo "  [dry-run] project global OpenCode agents (${ROLES}) into '$AGENT_DEST_DIR', set default_agent=lemoncrow.code"
    else
        "${PYTHON_CMD[@]}" - <<PYEOF
from pathlib import Path
from lemoncrow.core.capabilities.workspace_host_overrides import write_opencode_agents

written = write_opencode_agents(Path("${AGENT_DEST_DIR}"), role_ids=tuple(r for r in "${ROLES}".split(",") if r))
print(f"[lemoncrow:opencode] projected {len(written)} global OpenCode agents into ${AGENT_DEST_DIR}")
PYEOF
        "${PYTHON_CMD[@]}" - <<PYEOF2
import json
from pathlib import Path
path = Path("${OC_FILE}")
data = json.loads(path.read_text(encoding="utf-8") or "{}") if path.exists() else {}
data["default_agent"] = "lemoncrow.code"
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF2
        info "default_agent set -> lemoncrow.code (per-role agents installed)"
    fi
fi

# ---- install prompt-time nudge plugin ---------------------------------------
PLUGIN_SRC_DIR="${DIST_ROOT}/integrations/opencode/plugins"
if $DRY_RUN; then
    echo "  [dry-run] copy LemonCrow nudge plugin to '$PLUGIN_DEST_DIR'"
else
    run "mkdir -p $(printf %q "$PLUGIN_DEST_DIR")"
    run "cp -f $(printf %q "$PLUGIN_SRC_DIR/lemoncrow-nudge.js") $(printf %q "$PLUGIN_DEST_DIR/lemoncrow-nudge.js")"
    run "cp -f $(printf %q "$PLUGIN_SRC_DIR/lemoncrow_nudge.py") $(printf %q "$PLUGIN_DEST_DIR/lemoncrow_nudge.py")"
    info "LemonCrow nudge plugin installed -> $PLUGIN_DEST_DIR/lemoncrow-nudge.js"
fi


if $DRY_RUN; then
    info "Dry run complete; skipped post-install verification because no files were written."
    exit 0
fi

# ---- post-install verification ---------------------------------------------
info "Running post-install verification..."
VFAIL=0
vpass() { info "PASS: $*"; }
vfail() { echo "[lemoncrow:opencode] FAIL: $*" >&2; VFAIL=1; }

if [ -f "$OC_FILE" ]; then
    HAS=$(LEMONCROW_OC_FILE="$OC_FILE" "${PYTHON_CMD[@]}" - <<PYEOF
import json
import os
import re
from pathlib import Path

content = Path(os.environ['LEMONCROW_OC_FILE']).read_text(encoding='utf-8')
stripped = re.sub(r'^\s*//.*', '', content, flags=re.M)
try:
    d = json.loads(stripped)
    print('yes' if 'lc' in d.get('mcp', {}) else 'no')
except Exception:
    print('parse-error')
PYEOF
)
    if [ "$HAS" = "yes" ]; then
        vpass "opencode config contains lc MCP entry ($OC_FILE)"
    elif [ "$HAS" = "parse-error" ]; then
        vfail "opencode config parse error: $OC_FILE"
    else
        vfail "opencode config missing lc entry"
    fi

    DEFAULT_AGENT=$(LEMONCROW_OC_FILE="$OC_FILE" "${PYTHON_CMD[@]}" - <<PYEOF
import json
import os
import re
from pathlib import Path

content = Path(os.environ['LEMONCROW_OC_FILE']).read_text(encoding='utf-8')
stripped = re.sub(r'^\s*//.*', '', content, flags=re.M)
try:
    d = json.loads(stripped)
    print(d.get('default_agent', ''))
except Exception:
    print('')
PYEOF
)
    if [ "$DEFAULT_AGENT" = "code" ] || { [ "$INSTALL_SCOPE" = "global" ] && [ "$ROLES" != "code" ] && [ "$DEFAULT_AGENT" = "lemoncrow.code" ]; }; then
        vpass "opencode default_agent = $DEFAULT_AGENT"
    else
        vfail "opencode default_agent is '$DEFAULT_AGENT' (expected 'code' or 'lemoncrow.code')"
    fi

    HAS_PROVIDER=$(LEMONCROW_OC_FILE="$OC_FILE" "${PYTHON_CMD[@]}" - <<PYEOF
import json
import os
import re
from pathlib import Path

content = Path(os.environ['LEMONCROW_OC_FILE']).read_text(encoding='utf-8')
stripped = re.sub(r'^\s*//.*', '', content, flags=re.M)
try:
    d = json.loads(stripped)
    provider = d.get('provider', {}).get('lc', {})
    base_url = provider.get('options', {}).get('baseURL')
    print('yes' if provider and base_url else 'no')
except Exception:
    print('parse-error')
PYEOF
)
    if [ "$HAS_PROVIDER" = "yes" ]; then
        vpass "opencode provider.lc and model are configured for LemonCrow OpenAI gateway"
    elif [ "$HAS_PROVIDER" = "parse-error" ]; then
        vfail "opencode config parse error while validating provider settings"
    else
        vfail "opencode provider/model config for LemonCrow gateway is missing"
    fi
else
    vfail "opencode config not found: $OC_FILE"
fi

PLUGIN_FILE="${PLUGIN_DEST_DIR}/lemoncrow-nudge.js"
PLUGIN_HELPER="${PLUGIN_DEST_DIR}/lemoncrow_nudge.py"
if [ -f "$PLUGIN_FILE" ] && [ -f "$PLUGIN_HELPER" ]; then
    vpass "opencode LemonCrow prompt nudge plugin installed: $PLUGIN_FILE"
else
    vfail "opencode LemonCrow prompt nudge plugin missing from $PLUGIN_DEST_DIR"
fi

# Global mode installs a single primary agent as code.md; workspace mode
# projects per-role files (lemoncrow.<role>.md) with lemoncrow.code.md as the
# primary, so verify the name the writer actually produces for this scope.
if [ "$INSTALL_SCOPE" = "workspace" ] || [[ "$ROLES" != "code" ]]; then
    AGENT_FILE="${AGENT_DEST_DIR}/lemoncrow.code.md"
else
    AGENT_FILE="${AGENT_DEST_DIR}/code.md"
fi
if [ -f "$AGENT_FILE" ]; then
    vpass "opencode lemoncrow agent installed: $AGENT_FILE"
else
    vfail "opencode lemoncrow agent missing: $AGENT_FILE"
fi

if command -v lemoncrow &>/dev/null; then
    vpass "lemoncrow is available on PATH"
else
    vfail "lemoncrow NOT found on PATH"
fi

if command -v lemoncrow >/dev/null 2>&1 && lemoncrow status --help >/dev/null 2>&1; then
    vpass "lemoncrow status command is available"
else
    vfail "lemoncrow status command unavailable"
fi

if [ "$VFAIL" -ne 0 ]; then
    echo "[lemoncrow:opencode] ERROR: post-install verification failed." >&2
    exit 1
fi
info "All post-install checks passed"

info "Done. Restart opencode - LemonCrow agent and MCP are available."
info "Tip: run 'lemoncrow status' in any shell to see the runs dashboard."
