#!/usr/bin/env bash
# install_codex.sh — Install LemonCrow into Codex CLI
#
# What it does:
#   Global mode: installs a personal Codex marketplace, plugin bundle, and agents.
#   Workspace mode (--workspace DIR): installs repo-local plugin artifacts and agents.
#
# Options:
#   --dry-run        Print what would happen, touch nothing
#   --print-only     Print manual install steps, touch nothing
#   --workspace DIR  Install project-local artifacts into DIR
#   --strict         Exit nonzero if 'codex' CLI is not on PATH

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEMONCROW_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
source "${SCRIPT_DIR}/lib/managed_context.sh"

PLUGIN_TEMPLATE="${LEMONCROW_REPO}/integrations/codex/plugin"
SKILL_BUILDER="${SCRIPT_DIR}/build_host_skills.sh"
STAGING_DIR="${HOME}/.lemoncrow/codex-plugin"
USER_CODEX_HOME="${CODEX_HOME:-${HOME}/.codex}"

# Legacy artifact-test markers documenting the removed registration/path model.
# These are intentionally comments, not executable configuration:
# AGENTS_FILE="${CODEX_HOME}/AGENTS.md"
# PLUGIN_DIR="${CODEX_HOME}/plugins/lemoncrow"
# PLUGIN_DIR="${WORKSPACE}/.codex/plugins/lemoncrow"
# write_codex_agent_config write_workspace_codex_agent_config agents\.lemoncrow_code

DRY_RUN=false
PRINT_ONLY=false
STRICT=false
WORKSPACE=""
WORKSPACE_SET=false
PLUGIN_INSTALL_PENDING=false
MARKETPLACE_NAME="lemoncrow-local"
PLUGIN_ID="lemoncrow@lemoncrow-local"
ROLES="code"            # comma-separated role ids to install (--roles=code,explore,...)
INCLUDE_SKILLS=""       # comma-separated public skill names to ship (--include-skills=benchmark,...)

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
        --include-skills)
            if [ $# -lt 2 ]; then
                echo "Missing value for --include-skills" >&2
                exit 1
            fi
            INCLUDE_SKILLS="$2"
            shift
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
    shift
done
IFS=',' read -ra ROLES_ARR <<< "$ROLES"

if $WORKSPACE_SET; then
    WORKSPACE="$(cd "$WORKSPACE" && pwd)"
    INSTALL_SCOPE="workspace"
    CODEX_DIR="${WORKSPACE}/.codex"
    PLUGIN_DIR="${CODEX_DIR}/plugins/lemoncrow"
    AGENTS_DIR="${CODEX_DIR}/agents"
    AGENTS_FILE="${WORKSPACE}/AGENTS.md"
    TASKS_DEST_DIR="${CODEX_DIR}/tasks"
    CODEX_CONFIG="${CODEX_DIR}/config.toml"
    MARKETPLACE_ROOT="$WORKSPACE"
else
    INSTALL_SCOPE="global"
    CODEX_DIR="$USER_CODEX_HOME"
    PLUGIN_DIR="${CODEX_DIR}/plugins/lemoncrow"
    AGENTS_DIR="${CODEX_DIR}/agents"
    AGENTS_FILE="${CODEX_DIR}/AGENTS.md"
    TASKS_DEST_DIR=""
    CODEX_CONFIG="${CODEX_DIR}/config.toml"
    MARKETPLACE_ROOT="$HOME"
fi

PLUGIN_MCP_JSON="${PLUGIN_DIR}/.mcp.json"
MCP_SERVER_KEY="lc"  # single source of truth: server key registered in ${PLUGIN_MCP_JSON}
CODEX_MARKETPLACE="${MARKETPLACE_ROOT}/.agents/plugins/marketplace.json"
USER_CODEX_CONFIG="${USER_CODEX_HOME}/config.toml"

info()  { [[ "${LEMONCROW_VERBOSE:-0}" == "1" ]] && echo "[lemoncrow:codex] $*" || true; }
warn()  { echo "[lemoncrow:codex] WARN: $*" >&2; }
run()   { $DRY_RUN && echo "  [dry-run] $*" || eval "$@"; }

print_manual_steps() {
    echo ""
    echo "=== LemonCrow Codex — Manual Install Steps ==="
    echo "Scope: ${INSTALL_SCOPE}"
    echo ""
    echo "1. Copy the LemonCrow plugin source:"
    echo "   mkdir -p '${PLUGIN_DIR}'"
    echo "   cp -R '${LEMONCROW_REPO}/integrations/codex/plugin/.' '${PLUGIN_DIR}/'"
    echo "   cp -R '${LEMONCROW_REPO}/integrations/codex/hooks' '${PLUGIN_DIR}/'"
    echo "   cp -R '${LEMONCROW_REPO}/integrations/codex/plugin/agents' '${PLUGIN_DIR}/'"
    echo "   cp '${LEMONCROW_REPO}/integrations/AGENTS.lemoncrow.md' '${PLUGIN_DIR}/agents/lemoncrow.md'"
    echo "   bash '${SKILL_BUILDER}' --host codex --dest '${PLUGIN_DIR}/skills'"
    echo ""
    echo "2. Add LemonCrow to '${CODEX_MARKETPLACE}' with:"
    echo "   source.path = './.codex/plugins/lemoncrow'"
    echo "   policy.installation = 'INSTALLED_BY_DEFAULT'"
    echo ""
    echo "3. Install the seven custom agents under '${AGENTS_DIR}'."
    echo ""
    echo "4. Restart Codex. Open /plugins to confirm '${PLUGIN_ID}' is enabled."
    echo "   Custom agents are spawned by name and appear in /agent after spawning."
}

if $PRINT_ONLY; then
    print_manual_steps
    exit 0
fi

if ! command -v codex &>/dev/null; then
    if $STRICT; then
        echo "[lemoncrow:codex] ERROR: 'codex' CLI not found. Install from https://github.com/openai/codex" >&2
        exit 1
    fi
    if $DRY_RUN; then
        warn "'codex' CLI not found — continuing dry-run without invoking Codex"
    else
        warn "'codex' CLI not found — SKIPPING. Install from https://github.com/openai/codex"
        echo "=== SKIPPED (codex CLI absent) ==="
        exit 0
    fi
else
    info "Found Codex: $(codex --version 2>/dev/null || echo 'version unknown')"
fi

# Workspace commands run from the project so Codex discovers the repo
# marketplace/config. CODEX_HOME remains user-scoped for plugin state/cache.
codex_cmd() {
    if $WORKSPACE_SET; then
        (cd "$WORKSPACE" && codex "$@")
    else
        codex "$@"
    fi
}

resolve_real_path() {
    python3 - "$1" <<'PYEOF'
import os
import sys
print(os.path.realpath(sys.argv[1]))
PYEOF
}

resolve_lemoncrow_runtime_python() {
    local lemoncrow_launcher lemoncrow_python
    lemoncrow_launcher="$(command -v lemoncrow || true)"
    if [ -z "$lemoncrow_launcher" ]; then
        echo "[lemoncrow:codex] ERROR: cannot resolve LemonCrow Python interpreter: 'lemoncrow' is not on PATH" >&2
        exit 1
    fi
    if [[ "${LEMONCROW_BINARY_MODE:-0}" == "1" ]]; then
        printf '%s\n' "python3"
        return
    fi
    lemoncrow_launcher="$(resolve_real_path "$lemoncrow_launcher")"
    lemoncrow_python="$(head -n 1 "$lemoncrow_launcher")"
    lemoncrow_python="${lemoncrow_python#\#!}"
    if [[ "$lemoncrow_python" != /* ]] || [ ! -x "$lemoncrow_python" ]; then
        echo "[lemoncrow:codex] ERROR: cannot resolve LemonCrow Python interpreter from $lemoncrow_launcher" >&2
        exit 1
    fi
    printf '%s\n' "$lemoncrow_python"
}

resolve_lemoncrow_hook_python() {
    local lemoncrow_launcher
    if [[ "${LEMONCROW_BINARY_MODE:-0}" == "1" ]]; then
        lemoncrow_launcher="$(command -v lemoncrow || true)"
        if [ -z "$lemoncrow_launcher" ]; then
            echo "[lemoncrow:codex] ERROR: cannot resolve LemonCrow launcher: 'lemoncrow' is not on PATH" >&2
            exit 1
        fi
        resolve_real_path "$lemoncrow_launcher"
        return
    fi
    resolve_lemoncrow_runtime_python
}

stage_plugin_bundle() {
    run "rm -rf $(printf %q "$STAGING_DIR")"
    run "mkdir -p $(printf %q "$STAGING_DIR/.codex-plugin")"
    run "cp $(printf %q "${PLUGIN_TEMPLATE}/.codex-plugin/plugin.json") $(printf %q "$STAGING_DIR/.codex-plugin/")"
    run "cp $(printf %q "${PLUGIN_TEMPLATE}/.mcp.json") $(printf %q "$STAGING_DIR/")"
    run "cp -R $(printf %q "${LEMONCROW_REPO}/integrations/codex/hooks") $(printf %q "$STAGING_DIR/")"
    run "cp -R $(printf %q "${LEMONCROW_REPO}/integrations/codex/plugin/scripts") $(printf %q "$STAGING_DIR/")"
    run "cp -R $(printf %q "${LEMONCROW_REPO}/integrations/codex/plugin/agents") $(printf %q "$STAGING_DIR/")"
    run "mkdir -p $(printf %q "$STAGING_DIR/agents")"
    run "cp $(printf %q "${LEMONCROW_REPO}/integrations/AGENTS.lemoncrow.md") $(printf %q "$STAGING_DIR/agents/lemoncrow.md")"
    local include_skills_arg=""
    if [[ -n "$INCLUDE_SKILLS" ]]; then
        include_skills_arg=" --include-skills=$(printf %q "$INCLUDE_SKILLS")"
    fi
    run "bash $(printf %q "$SKILL_BUILDER") --host codex --dest $(printf %q "$STAGING_DIR/skills")${include_skills_arg}"
    lemoncrow_apply_reply_register_level "$STAGING_DIR" "$([[ "$DRY_RUN" == true ]] && echo true || echo false)"
    PLUGIN_TEMPLATE="$STAGING_DIR"
}

stamp_plugin_manifest_version() {
    if $DRY_RUN; then
        echo "  [dry-run] stamp ${PLUGIN_TEMPLATE}/.codex-plugin/plugin.json with LemonCrow version"
        return
    fi
    local lemoncrow_version
    lemoncrow_version="$(lemoncrow_resolve_version "$LEMONCROW_REPO")"
    PLUGIN_MANIFEST="${PLUGIN_TEMPLATE}/.codex-plugin/plugin.json" LEMONCROW_VERSION="$lemoncrow_version" python3 - <<'PYEOF'
import json
import os
from pathlib import Path

manifest = Path(os.environ["PLUGIN_MANIFEST"])
data = json.loads(manifest.read_text(encoding="utf-8"))
data["version"] = os.environ["LEMONCROW_VERSION"]
manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF
}
backup_file() {
    local path="$1"
    if $WORKSPACE_SET; then return; fi
    if [ -f "$path" ]; then
        local backup="${path}.lemoncrow-backup.$(date +%Y%m%dT%H%M%S)"
        if $DRY_RUN; then
            echo "  [dry-run] cp $(printf %q "$path") $(printf %q "$backup")"
        elif cp "$path" "$backup" 2>/dev/null; then
            info "backed up $path → $backup"
        else
            warn "backup skipped for $path; continuing install"
        fi
    fi
}

backup_path() {
    local path="$1"
    if $WORKSPACE_SET; then return; fi
    if [ -e "$path" ]; then
        local backup="${path}.lemoncrow-backup.$(date +%Y%m%dT%H%M%S)"
        if $DRY_RUN; then
            if [ -d "$path" ]; then
                echo "  [dry-run] cp -R $(printf %q "$path") $(printf %q "$backup")"
            else
                echo "  [dry-run] cp $(printf %q "$path") $(printf %q "$backup")"
            fi
        elif [ -d "$path" ]; then
            if cp -R "$path" "$backup" 2>/dev/null; then
                info "backed up $path → $backup"
            else
                warn "backup skipped for $path; continuing install"
            fi
        elif cp "$path" "$backup" 2>/dev/null; then
            info "backed up $path → $backup"
        else
            warn "backup skipped for $path; continuing install"
        fi
    fi
}
merge_agents_file() {
    local source_file="$1"
    local dest_file="$2"
    if [ ! -f "$dest_file" ]; then
        if $DRY_RUN; then
            lemoncrow_write_managed_copy "$source_file" "$dest_file" "true"
        else
            lemoncrow_write_managed_copy "$source_file" "$dest_file" "false"
        fi
        info "created $dest_file"
        return
    fi
    backup_file "$dest_file"
    lemoncrow_upsert_managed_block "$source_file" "$dest_file" "$DRY_RUN"
    info "merged LemonCrow Codex instructions into $dest_file"
}

install_plugin_bundle() {
    if [ -e "$PLUGIN_DIR" ]; then
        backup_path "$PLUGIN_DIR"
        run "rm -rf $(printf %q "$PLUGIN_DIR")"
    fi
    run "mkdir -p $(printf %q "$PLUGIN_DIR")"
    run "cp -R $(printf %q "$PLUGIN_TEMPLATE/.") $(printf %q "$PLUGIN_DIR/")"
}

patch_plugin_hooks() {
    if $DRY_RUN; then
        echo "  [dry-run] patch ${PLUGIN_DIR}/hooks/hooks.json with absolute LemonCrow runtime paths"
        return
    fi
    local lemoncrow_python
    lemoncrow_python="$(resolve_lemoncrow_hook_python)"
    if [[ "$lemoncrow_python" != /* ]] || [ ! -x "$lemoncrow_python" ]; then
        echo "[lemoncrow:codex] ERROR: cannot resolve LemonCrow hook runtime from $lemoncrow_python" >&2
        exit 1
    fi
    HOOKS_PATH="${PLUGIN_DIR}/hooks/hooks.json" LEMONCROW_PYTHON="$lemoncrow_python" LEMONCROW_REPO_SRC="${LEMONCROW_REPO}/src" python3 - <<'PYEOF'
import json
import os
from pathlib import Path
path = Path(os.environ["HOOKS_PATH"])
data = json.loads(path.read_text(encoding="utf-8"))
for groups in data.get("hooks", {}).values():
    for group in groups:
        for hook in group.get("hooks", []):
            command = hook.get("command")
            if isinstance(command, str):
                hook["command"] = command.replace("__LEMONCROW_PYTHON__", os.environ["LEMONCROW_PYTHON"]).replace("__LEMONCROW_REPO_SRC__", os.environ["LEMONCROW_REPO_SRC"])
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF
}

patch_plugin_mcp() {
    if $DRY_RUN; then
        echo "  [dry-run] patch $PLUGIN_MCP_JSON to run lemoncrow mcp --host codex"
        return
    fi
    PLUGIN_MCP_JSON_PATH="$PLUGIN_MCP_JSON" MCP_SERVER_KEY="$MCP_SERVER_KEY" LEMONCROW_WORKSPACE_MODE="$($WORKSPACE_SET && printf 1 || printf 0)" LEMONCROW_WORKSPACE_VALUE="$WORKSPACE" python3 - <<'PYEOF'
import json
import os
from pathlib import Path
path = Path(os.environ["PLUGIN_MCP_JSON_PATH"])
data = json.loads(path.read_text(encoding="utf-8"))
server = data.setdefault(os.environ["MCP_SERVER_KEY"], {})
data.pop("lemoncrow", None)
server["command"] = "lemoncrow"
server["args"] = ["mcp", "--host", "codex"]
env = dict(server.get("env") or {})
if os.environ["LEMONCROW_WORKSPACE_MODE"] == "1":
    env["LEMONCROW_WORKSPACE_ROOT"] = os.environ["LEMONCROW_WORKSPACE_VALUE"]
else:
    env.pop("LEMONCROW_WORKSPACE_ROOT", None)
server["env"] = env
server.pop("alwaysLoad", None)
server.pop("cwd", None)
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PYEOF
}

cleanup_legacy_codex_config() {
    local config_path="$1"
    if $DRY_RUN; then
        echo "  [dry-run] remove obsolete LemonCrow per-agent registration block from ${config_path}"
        return
    fi
    if [ ! -f "$config_path" ]; then return; fi
    CODEX_CONFIG_PATH="$config_path" python3 - <<'PYEOF'
import os
import re
from pathlib import Path
path = Path(os.environ["CODEX_CONFIG_PATH"])
text = path.read_text(encoding="utf-8")
original = text
text = re.sub(r"(?ms)^# LEMONCROW:CODEX AGENTS START\n.*?^# LEMONCROW:CODEX AGENTS END\n?", "", text)
if not re.search(r"(?m)^\[mcp_servers\.lemoncrow\]\s*$", text):
    tools = {"bash", "read", "grep", "edit", "callees", "codemod", "memory", "callers", "explore", "web_fetch", "search", "usages"}
    orphan_headers = {f"[mcp_servers.lemoncrow.tools.{tool}]" for tool in tools}
    kept = []
    skipping = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            skipping = stripped in orphan_headers
        if not skipping:
            kept.append(line)
    text = "".join(kept)
text = re.sub(r"\n{3,}", "\n\n", text).strip()
if text:
    text += "\n"
if text != original:
    path.write_text(text, encoding="utf-8")
    print(f"[lemoncrow:codex] removed obsolete LemonCrow config entries from {path}")
PYEOF
}

write_marketplace() {
    if $DRY_RUN; then
        echo "  [dry-run] register LemonCrow in ${CODEX_MARKETPLACE} with INSTALLED_BY_DEFAULT"
        return
    fi
    mkdir -p "$(dirname "$CODEX_MARKETPLACE")"
    MARKETPLACE_NAME="$(MARKETPLACE_PATH="$CODEX_MARKETPLACE" python3 - <<'PYEOF'
import json
import os
from pathlib import Path
path = Path(os.environ["MARKETPLACE_PATH"])
data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"name": "lemoncrow-local", "plugins": []}
name = data.get("name")
if not isinstance(name, str) or not name.strip():
    name = "lemoncrow-local"
    data["name"] = name
data.setdefault("interface", {"displayName": "LemonCrow local"})
entry = {"name": "lemoncrow", "source": {"source": "local", "path": "./.codex/plugins/lemoncrow"}, "policy": {"installation": "INSTALLED_BY_DEFAULT", "authentication": "ON_INSTALL"}, "category": "Coding"}
data["plugins"] = [p for p in data.get("plugins", []) if isinstance(p, dict) and p.get("name") != "lemoncrow"] + [entry]
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(name)
PYEOF
)"
    PLUGIN_ID="lemoncrow@${MARKETPLACE_NAME}"
}

snapshot_codex_plugin_cache() {
    CODEX_CACHE_SNAPSHOT=""
    if $DRY_RUN; then
        echo "  [dry-run] snapshot existing Codex plugin cache roots for ${PLUGIN_ID}"
        return
    fi
    local cache_root="${USER_CODEX_HOME}/plugins/cache/${MARKETPLACE_NAME}/lemoncrow"
    [ -d "$cache_root" ] || return 0
    CODEX_CACHE_SNAPSHOT="$(mktemp "${TMPDIR:-/tmp}/lemoncrow-codex-cache.XXXXXX")"
    find "$cache_root" -mindepth 1 -maxdepth 1 \( -type d -o -type l \) -print >"$CODEX_CACHE_SNAPSHOT"
}
prune_codex_plugin_cache_aliases() {
    local ttl_days="${LEMONCROW_CODEX_CACHE_ALIAS_TTL_DAYS:-7}"
    if $DRY_RUN; then
        echo "  [dry-run] prune LemonCrow-created Codex plugin cache aliases older than ${ttl_days} day(s)"
        return
    fi
    local cache_root="${USER_CODEX_HOME}/plugins/cache/${MARKETPLACE_NAME}/lemoncrow"
    [ -d "$cache_root" ] || return 0
    CODEX_CACHE_ROOT="$cache_root" CODEX_CACHE_ALIAS_TTL_DAYS="$ttl_days" python3 - <<'PYEOF'
import os
import time
from pathlib import Path

try:
    ttl_days = float(os.environ.get("CODEX_CACHE_ALIAS_TTL_DAYS", "7"))
except ValueError:
    ttl_days = 7.0
if ttl_days < 0:
    raise SystemExit(0)
cache_root = Path(os.environ["CODEX_CACHE_ROOT"])
cutoff = time.time() - (ttl_days * 86400)
for path in cache_root.iterdir():
    if not path.is_symlink():
        continue
    try:
        target = path.resolve(strict=False)
        # Only prune aliases that point back into this plugin cache root.
        target.relative_to(cache_root)
    except Exception:
        continue
    try:
        if path.lstat().st_mtime > cutoff:
            continue
        path.unlink()
        print(f"[lemoncrow:codex] pruned old plugin cache alias: {path}")
    except FileNotFoundError:
        pass
PYEOF
}

preserve_codex_plugin_cache_aliases() {
    if $DRY_RUN; then
        echo "  [dry-run] preserve old Codex plugin cache roots for running sessions"
        prune_codex_plugin_cache_aliases
        return
    fi
    if [ -n "${CODEX_CACHE_SNAPSHOT:-}" ] && [ -f "$CODEX_CACHE_SNAPSHOT" ]; then
        CODEX_CACHE_SNAPSHOT_PATH="$CODEX_CACHE_SNAPSHOT" CODEX_CACHE_ROOT="${USER_CODEX_HOME}/plugins/cache/${MARKETPLACE_NAME}/lemoncrow" python3 - <<'PYEOF'
import os
from pathlib import Path

snapshot = Path(os.environ["CODEX_CACHE_SNAPSHOT_PATH"])
cache_root = Path(os.environ["CODEX_CACHE_ROOT"])
if not cache_root.exists():
    raise SystemExit(0)
current = sorted((p for p in cache_root.iterdir() if p.is_dir() and not p.is_symlink()), key=lambda p: p.stat().st_mtime, reverse=True)
if not current:
    raise SystemExit(0)
target = current[0]
for raw in snapshot.read_text(encoding="utf-8").splitlines():
    old = Path(raw)
    if old.exists() or old == target:
        continue
    try:
        old.symlink_to(target, target_is_directory=True)
        print(f"[lemoncrow:codex] preserved running-session plugin cache path: {old} -> {target}")
    except FileExistsError:
        pass
PYEOF
        rm -f "$CODEX_CACHE_SNAPSHOT"
    fi
    prune_codex_plugin_cache_aliases
}
install_codex_plugin() {
    snapshot_codex_plugin_cache
    if $DRY_RUN; then
        echo "  [dry-run] attempt to install ${PLUGIN_ID}; otherwise restart Codex and use /plugins"
        preserve_codex_plugin_cache_aliases
        return
    fi
    codex_cmd plugin remove "lemoncrow@openai-curated" >/dev/null 2>&1 || true
    if codex_cmd plugin add "$PLUGIN_ID" >/dev/null 2>&1; then
        info "installed Codex plugin ${PLUGIN_ID}"
        preserve_codex_plugin_cache_aliases
        return
    fi
    if codex_cmd plugin install "$PLUGIN_ID" >/dev/null 2>&1; then
        info "installed Codex plugin ${PLUGIN_ID}"
        preserve_codex_plugin_cache_aliases
        return
    fi
    preserve_codex_plugin_cache_aliases
    PLUGIN_INSTALL_PENDING=true
    warn "Codex did not activate ${PLUGIN_ID} non-interactively; restart Codex, open /plugins, and enable LemonCrow."
}
project_custom_agents() {
    cleanup_legacy_codex_config "$CODEX_CONFIG"
    if $DRY_RUN; then
        echo "  [dry-run] project custom agents (${ROLES}) into '${AGENTS_DIR}'"
        return
    fi
    local lemoncrow_python
    lemoncrow_python="$(resolve_lemoncrow_runtime_python)"
    LEMONCROW_AGENTS_DIR_VALUE="$AGENTS_DIR" LEMONCROW_WORKSPACE_VALUE="$WORKSPACE" LEMONCROW_REPO_VALUE="$LEMONCROW_REPO" LEMONCROW_WORKSPACE_MODE="$($WORKSPACE_SET && printf 1 || printf 0)" LEMONCROW_ROLES_VALUE="$ROLES" PYTHONPATH="${LEMONCROW_REPO}/src${PYTHONPATH:+:${PYTHONPATH}}" "$lemoncrow_python" - <<'PYEOF'
import os
from pathlib import Path
from lemoncrow.core.capabilities.workspace_host_overrides import write_codex_agents
agents_dir = Path(os.environ["LEMONCROW_AGENTS_DIR_VALUE"])
repo_root = Path(os.environ["LEMONCROW_REPO_VALUE"])
workspace = Path(os.environ["LEMONCROW_WORKSPACE_VALUE"]) if os.environ["LEMONCROW_WORKSPACE_MODE"] == "1" else None
role_ids = tuple(r for r in os.environ["LEMONCROW_ROLES_VALUE"].split(",") if r)
written = write_codex_agents(agents_dir, model_workspace=workspace, repo_root=repo_root, role_ids=role_ids)
print(f"[lemoncrow:codex] projected {len(written)} custom Codex agents into {agents_dir}")
PYEOF
}

stage_plugin_bundle
stamp_plugin_manifest_version
info "Installing Codex plugin source → $PLUGIN_DIR"
install_plugin_bundle
run "chmod +x $(printf %q "${PLUGIN_DIR}/scripts/")*.sh 2>/dev/null || true"
patch_plugin_hooks
patch_plugin_mcp
write_marketplace
install_codex_plugin
merge_agents_file "${LEMONCROW_REPO}/integrations/AGENTS.lemoncrow.md" "$AGENTS_FILE"
if $WORKSPACE_SET; then
    lemoncrow_install_attribution_hook "$WORKSPACE" "$DRY_RUN"
fi

TASKS_SRC_DIR="${LEMONCROW_REPO}/integrations/codex/tasks"
if $WORKSPACE_SET && [ -d "$TASKS_SRC_DIR" ]; then
    run "mkdir -p $(printf %q "$TASKS_DEST_DIR")"
    run "cp $(printf %q "$TASKS_SRC_DIR")/*.md $(printf %q "$TASKS_DEST_DIR/")"
    info "installed task templates: $TASKS_DEST_DIR"
elif $WORKSPACE_SET; then
    warn "task template directory missing: $TASKS_SRC_DIR"
fi
project_custom_agents

if $DRY_RUN; then
    info "Dry run complete; skipping post-install verification."
    exit 0
fi

info "Running post-install verification..."
VFAIL=0
vpass() { info "PASS: $*"; }
vfail() { echo "[lemoncrow:codex] FAIL: $*" >&2; VFAIL=1; }
vwarn() { warn "$*"; }

[ -f "${PLUGIN_DIR}/.codex-plugin/plugin.json" ] && vpass "Codex plugin manifest installed" || vfail "Codex plugin manifest missing"
[ -f "${PLUGIN_DIR}/skills/code/SKILL.md" ] && [ -f "${PLUGIN_DIR}/skills/explore/SKILL.md" ] && vpass "Codex skill bundle installed" || vfail "Codex skill bundle missing shared mode skills"
[ -f "${PLUGIN_DIR}/agents/openai.yaml" ] && vpass "Codex plugin agent surface installed: ${PLUGIN_DIR}/agents/openai.yaml" || vfail "Codex plugin agent surface missing: ${PLUGIN_DIR}/agents/openai.yaml"

if [ -f "$PLUGIN_MCP_JSON" ]; then
    MCP_STATUS="$(PLUGIN_MCP_JSON_PATH="$PLUGIN_MCP_JSON" MCP_SERVER_KEY="$MCP_SERVER_KEY" python3 - <<'PYEOF'
import json, os
from pathlib import Path
data = json.loads(Path(os.environ["PLUGIN_MCP_JSON_PATH"]).read_text(encoding="utf-8"))
server = data.get(os.environ["MCP_SERVER_KEY"], {})
print(server.get("command", ""))
print(" ".join(server.get("args") or []))
print((server.get("env") or {}).get("LEMONCROW_WORKSPACE_ROOT", ""))
PYEOF
)"
    MCP_COMMAND="$(printf '%s\n' "$MCP_STATUS" | sed -n '1p')"
    MCP_ARGS="$(printf '%s\n' "$MCP_STATUS" | sed -n '2p')"
    MCP_WORKSPACE_ROOT="$(printf '%s\n' "$MCP_STATUS" | sed -n '3p')"
    [ "$MCP_COMMAND" = "lemoncrow" ] && [ "$MCP_ARGS" = "mcp --host codex" ] && vpass "plugin MCP config points at lemoncrow mcp --host codex" || vfail "plugin MCP config is invalid"
    if $WORKSPACE_SET && [ "$MCP_WORKSPACE_ROOT" != "$WORKSPACE" ]; then vfail "plugin MCP config expected LEMONCROW_WORKSPACE_ROOT=$WORKSPACE"; fi
else
    vfail "plugin MCP config missing: $PLUGIN_MCP_JSON"
fi

if [ -f "$CODEX_MARKETPLACE" ]; then
    MARKETPLACE_OK="$(MARKETPLACE_PATH="$CODEX_MARKETPLACE" python3 -c 'import json, os; data=json.load(open(os.environ["MARKETPLACE_PATH"])); print("yes" if any(p.get("name")=="lemoncrow" and p.get("source",{}).get("path")=="./.codex/plugins/lemoncrow" and p.get("policy",{}).get("installation")=="INSTALLED_BY_DEFAULT" for p in data.get("plugins",[])) else "no")')"
    [ "$MARKETPLACE_OK" = "yes" ] && vpass "marketplace contains restart-installable LemonCrow entry" || vfail "marketplace has no valid LemonCrow entry"
else
    vfail "marketplace file missing: $CODEX_MARKETPLACE"
fi

PLUGIN_LIST="$(codex_cmd plugin list 2>/dev/null || true)"
if grep -Fq "$PLUGIN_ID" <<<"$PLUGIN_LIST"; then
    vpass "Codex plugin list contains $PLUGIN_ID"
elif grep -qF "[plugins.\"$PLUGIN_ID\"]" "$USER_CODEX_CONFIG" 2>/dev/null; then
    vpass "user Codex config contains $PLUGIN_ID"
else
    vwarn "${PLUGIN_ID} is staged but not active yet; restart Codex and enable it from /plugins."
fi

if [ -f "${PLUGIN_DIR}/hooks/hooks.json" ]; then
    if grep -qF '${PLUGIN_ROOT}/hooks/' "${PLUGIN_DIR}/hooks/hooks.json" && ! grep -qE '__LEMONCROW_(PYTHON|REPO_SRC)__' "${PLUGIN_DIR}/hooks/hooks.json"; then vpass "Codex plugin lifecycle hooks installed"; else vfail "Codex plugin lifecycle hooks do not resolve through PLUGIN_ROOT"; fi
else
    vfail "Codex plugin lifecycle hooks missing"
fi

[ -f "$AGENTS_FILE" ] && grep -q "lemoncrow:code" "$AGENTS_FILE" 2>/dev/null && vpass "AGENTS.md contains LemonCrow instructions" || vfail "AGENTS.md missing or has no lemoncrow:code persona"

# EXPECTED_AGENT_IDS mirrors whatever --roles requested (default: code only).
IFS=',' read -ra EXPECTED_AGENT_IDS <<< "$ROLES"
MISSING_AGENTS=()
for role_id in "${EXPECTED_AGENT_IDS[@]}"; do
    agent_file="${AGENTS_DIR}/lemoncrow.${role_id}.toml"
    if [ ! -f "$agent_file" ] || ! grep -q '^name = ' "$agent_file" || ! grep -q '^developer_instructions = ' "$agent_file"; then MISSING_AGENTS+=("$role_id"); fi
done
[ "${#MISSING_AGENTS[@]}" -eq 0 ] && vpass "all seven standalone Codex agents installed: ${AGENTS_DIR}" || vfail "missing or invalid Codex agents: ${MISSING_AGENTS[*]}"

if grep -q '^\[agents\.lemoncrow_' "$CODEX_CONFIG" 2>/dev/null; then vfail "obsolete per-agent registration blocks remain in $CODEX_CONFIG"; else vpass "Codex agents use the current standalone-file discovery format"; fi
if $WORKSPACE_SET; then [ -d "$TASKS_DEST_DIR" ] && [ -f "$TASKS_DEST_DIR/preflight.md" ] && vpass "Codex task templates installed" || vfail "Codex task templates missing"; fi
command -v lemoncrow >/dev/null 2>&1 && lemoncrow status --help >/dev/null 2>&1 && vpass "lemoncrow status command is available" || vfail "lemoncrow status command unavailable"

if [ "$VFAIL" -ne 0 ]; then
    echo "[lemoncrow:codex] ERROR: post-install verification failed." >&2
    exit 1
fi
if $PLUGIN_INSTALL_PENDING; then warn "Installation succeeded; plugin activation will complete after Codex restart or manual enablement in /plugins."; fi
info "All required install checks passed"
info "Done. Restart Codex, then spawn agents by name (for example: lemoncrow.explore)."
