#!/usr/bin/env bash
# uninstall_codex.sh - Remove LemonCrow from Codex CLI
#
# Options:
#   --workspace DIR  Remove project-local artifacts from DIR instead of global user config
#   --dry-run        Print what would happen, touch nothing

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "${SCRIPT_DIR}/lib/managed_context.sh"

DRY_RUN=false
WORKSPACE=""
WORKSPACE_SET=false

while [[ $# -gt 0 ]]; do
    case "$1" in
        --dry-run) DRY_RUN=true ;;
        --workspace)
            if [ $# -lt 2 ]; then
                echo "Missing value for --workspace" >&2
                exit 1
            fi
            WORKSPACE="$2"
            WORKSPACE_SET=true
            shift
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
    shift
done

if $WORKSPACE_SET; then
    WORKSPACE="$(cd "$WORKSPACE" && pwd)"
    CODEX_HOME="${WORKSPACE}/.codex"
    MARKETPLACE_JSON="${WORKSPACE}/.agents/plugins/marketplace.json"
    AGENTS_FILE="${WORKSPACE}/AGENTS.md"
    TASKS_DIR="${WORKSPACE}/.codex/tasks"
else
    CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"
    MARKETPLACE_JSON="${HOME}/.agents/plugins/marketplace.json"
    AGENTS_FILE="${CODEX_HOME}/AGENTS.md"
    TASKS_DIR=""
fi

PLUGIN_DIR="${CODEX_HOME}/plugins/lemoncrow"
PLUGIN_CACHE_DIR="${HOME}/.codex/plugins/cache/lemoncrow"
OPENAI_CURATED_PLUGIN_CACHE_DIR="${CODEX_HOME}/plugins/cache/openai-curated/lemoncrow"
AGENTS_DIR="${CODEX_HOME}/agents"
AGENT_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/integrations/AGENTS.lemoncrow.md"
STAGING_DIRS=("${HOME}/.lemoncrow/codex-plugin" "${HOME}/.lemoncrow/codex-plugin-stable" "${HOME}/.lemoncrow/codex-plugin-dev")

info()  { echo "[lc:uninstall:codex] $*"; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

codex_cmd() {
    if $WORKSPACE_SET; then
        (cd "$WORKSPACE" && codex "$@")
    else
        codex "$@"
    fi
}

if command -v codex >/dev/null 2>&1; then
    if $DRY_RUN; then
        if $WORKSPACE_SET; then
            echo "  [dry-run] (cd '$WORKSPACE' && codex mcp remove lemoncrow)"
            echo "  [dry-run] (cd '$WORKSPACE' && codex plugin remove lemoncrow@lemoncrow-local)"
            echo "  [dry-run] (cd '$WORKSPACE' && codex plugin remove lemoncrow --marketplace lemoncrow-local)"
        else
            echo "  [dry-run] codex mcp remove lemoncrow"
            echo "  [dry-run] codex plugin remove lemoncrow@lemoncrow-local"
            echo "  [dry-run] codex plugin remove lemoncrow --marketplace lemoncrow-local"
        fi
    else
        codex_cmd mcp remove lemoncrow >/dev/null 2>&1 || true
        codex_cmd plugin remove lemoncrow@lemoncrow-local >/dev/null 2>&1 || true
        codex_cmd plugin remove lemoncrow --marketplace lemoncrow-local >/dev/null 2>&1 || true
        codex_cmd plugin remove lemoncrow --marketplace lemoncrow >/dev/null 2>&1 || true
        codex_cmd plugin remove lemoncrow@openai-curated >/dev/null 2>&1 || true
    fi
fi

if [ -f "$MARKETPLACE_JSON" ]; then
    run "python3 -c '
import json, sys
from pathlib import Path
path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding=\"utf-8\") or \"{}\")
plugins = [plugin for plugin in data.get(\"plugins\", []) if plugin.get(\"name\") != \"lemoncrow\"]
if plugins:
    data[\"plugins\"] = plugins
    path.write_text(json.dumps(data, indent=2) + \"\\n\", encoding=\"utf-8\")
else:
    path.unlink()
' $(printf %q "$MARKETPLACE_JSON")"
    info "Removed LemonCrow marketplace entry from $MARKETPLACE_JSON"
fi

if [ -d "$PLUGIN_DIR" ]; then
    run "rm -rf $(printf %q "$PLUGIN_DIR")"
    info "Removed $PLUGIN_DIR"
fi

if [ -d "$PLUGIN_CACHE_DIR" ]; then
    run "rm -rf $(printf %q "$PLUGIN_CACHE_DIR")"
    info "Removed $PLUGIN_CACHE_DIR"
fi

if [ -d "$OPENAI_CURATED_PLUGIN_CACHE_DIR" ]; then
    run "rm -rf $(printf %q "$OPENAI_CURATED_PLUGIN_CACHE_DIR")"
    info "Removed $OPENAI_CURATED_PLUGIN_CACHE_DIR"
fi

CODEX_CONFIG="${CODEX_HOME}/config.toml"
if [ -f "$CODEX_CONFIG" ] && grep -q 'plugins."lemoncrow@' "$CODEX_CONFIG" 2>/dev/null; then
    run "python3 -c '
import sys
from pathlib import Path

path = Path(sys.argv[1])
remove_headers = {
    \"[plugins.\\\"lemoncrow@lemoncrow-local\\\"]\",
    \"[plugins.\\\"lemoncrow@openai-curated\\\"]\",
}
lines = path.read_text(encoding=\"utf-8\").splitlines()
out = []
skip = False
removed = 0
for line in lines:
    stripped = line.strip()
    if stripped in remove_headers:
        skip = True
        removed += 1
        continue
    if skip and stripped.startswith(\"[\") and stripped.endswith(\"]\"):
        skip = False
    if not skip:
        out.append(line)
text = \"\\n\".join(out).rstrip()
if text:
    path.write_text(text + \"\\n\", encoding=\"utf-8\")
else:
    path.unlink()
print(removed)
' $(printf %q "$CODEX_CONFIG")"
    info "Removed LemonCrow plugin config from $CODEX_CONFIG"
fi

for staging_dir in "${STAGING_DIRS[@]}"; do
    if [ -d "$staging_dir" ]; then
        run "rm -rf $(printf %q "$staging_dir")"
        info "Removed $staging_dir"
    fi
done

if [ -d "$AGENTS_DIR" ]; then
    for f in "$AGENTS_DIR"/lemoncrow.*.toml; do
        [ -f "$f" ] || continue
        run "rm -f $(printf %q "$f")"
        info "Removed agent file: $f"
    done
fi

if [ -f "$AGENTS_FILE" ]; then
    if $DRY_RUN; then
        if grep -q "$LEMONCROW_CODE_BLOCK_START" "$AGENTS_FILE" 2>/dev/null; then
            echo "  [dry-run] remove managed LemonCrow Codex instructions from $AGENTS_FILE"
        elif grep -q "lc:code" "$AGENTS_FILE" 2>/dev/null; then
            echo "  [dry-run] remove legacy LemonCrow Codex instructions file $AGENTS_FILE"
        fi
    else
        REMOVE_RESULT="$(lemoncrow_remove_managed_block "$AGENTS_FILE" "false")"
        if [ "$REMOVE_RESULT" = "unchanged" ] && [ -f "$AGENTS_FILE" ]; then
            REMOVE_RESULT=$(python3 - <<PYEOF
from pathlib import Path

agents_path = Path("$AGENTS_FILE")
source_path = Path("$AGENT_SRC")
text = agents_path.read_text(encoding="utf-8")
source = source_path.read_text(encoding="utf-8").strip()

if text.strip() == source:
    agents_path.unlink()
    print("removed-legacy-exact")
elif "lc:code" in text:
    backup_path = agents_path.with_suffix(agents_path.suffix + ".lemoncrow-removed-backup")
    backup_path.write_text(text, encoding="utf-8")
    agents_path.unlink()
    print("removed-legacy-unmanaged")
else:
    print("unchanged")
PYEOF
)
        fi
        case "$REMOVE_RESULT" in
            updated)
                info "Removed managed LemonCrow Codex instructions from $AGENTS_FILE"
                ;;
            removed|removed-legacy-exact|removed-legacy-unmanaged)
                info "Removed $AGENTS_FILE"
                ;;
        esac
    fi
fi

if [ -n "$TASKS_DIR" ] && [ -d "$TASKS_DIR" ]; then
    run "rm -rf $(printf %q "$TASKS_DIR")"
    info "Removed $TASKS_DIR"
fi

info "Done."
