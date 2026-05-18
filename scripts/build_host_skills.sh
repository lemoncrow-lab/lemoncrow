#!/usr/bin/env bash
# build_host_skills.sh — Generate host skill bundles from integrations/skills
#
# Usage:
#   bash scripts/build_host_skills.sh --host codex
#   bash scripts/build_host_skills.sh --host gemini --include-dev
#   bash scripts/build_host_skills.sh --host codex --dest /tmp/codex-skills
#   bash scripts/build_host_skills.sh --host all --include-dev

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ATELIER_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILLS_SRC="${ATELIER_REPO}/integrations/skills"
ALWAYS_EXCLUDED_SKILLS=("trace")

HOST=""
DEST=""
INCLUDE_DEV=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --host)
            if [[ $# -lt 2 ]]; then
                echo "Missing value for --host" >&2
                exit 1
            fi
            HOST="$2"
            shift
            ;;
        --dest)
            if [[ $# -lt 2 ]]; then
                echo "Missing value for --dest" >&2
                exit 1
            fi
            DEST="$2"
            shift
            ;;
        --include-dev)
            INCLUDE_DEV=1
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
    shift
done

if [[ -z "$HOST" ]]; then
    echo "--host is required (codex | gemini | all)" >&2
    exit 1
fi

if [[ ! -d "$SKILLS_SRC" ]]; then
    echo "Shared skills directory not found: $SKILLS_SRC" >&2
    exit 1
fi

mapfile -t DEV_ONLY_SKILLS < <(
    PYTHONPATH="${ATELIER_REPO}/src:${PYTHONPATH:-}" python3 - <<'PY'
from atelier.core.environment import DEV_ONLY_SKILLS

for name in sorted(DEV_ONLY_SKILLS):
    print(name)
PY
)

is_dev_only_skill() {
    local name="$1"
    local skill
    for skill in "${DEV_ONLY_SKILLS[@]}"; do
        if [[ "$skill" == "$name" ]]; then
            return 0
        fi
    done
    return 1
}

is_always_excluded_skill() {
    local name="$1"
    local skill
    for skill in "${ALWAYS_EXCLUDED_SKILLS[@]}"; do
        if [[ "$skill" == "$name" ]]; then
            return 0
        fi
    done
    return 1
}

default_dest_for_host() {
    case "$1" in
        codex) printf "%s" "${ATELIER_REPO}/integrations/codex/plugin/skills" ;;
        gemini) printf "%s" "${ATELIER_REPO}/integrations/gemini/extension/skills" ;;
        *)
            echo "Unknown host: $1" >&2
            exit 1
            ;;
    esac
}

render_host_bundle() {
    local host="$1"
    local dest_dir="$2"

    mkdir -p "$dest_dir"
    find "$dest_dir" -mindepth 1 -maxdepth 1 \
        ! -name ".gitignore" \
        ! -name "README.md" \
        -exec rm -rf {} +

    local skill_dir
    for skill_dir in "$SKILLS_SRC"/*; do
        if [[ ! -d "$skill_dir" || ! -f "$skill_dir/SKILL.md" ]]; then
            continue
        fi

        local skill_name
        skill_name="$(basename "$skill_dir")"
        if is_always_excluded_skill "$skill_name"; then
            continue
        fi
        if [[ "$INCLUDE_DEV" != "1" ]] && is_dev_only_skill "$skill_name"; then
            continue
        fi

        mkdir -p "$dest_dir/$skill_name"
        cp "$skill_dir/SKILL.md" "$dest_dir/$skill_name/SKILL.md"
    done

    echo "[atelier:skills] generated ${host} bundle -> ${dest_dir}"
}

if [[ "$HOST" == "all" ]]; then
    if [[ -n "$DEST" ]]; then
        echo "--dest cannot be used with --host all" >&2
        exit 1
    fi
    render_host_bundle "codex" "$(default_dest_for_host codex)"
    render_host_bundle "gemini" "$(default_dest_for_host gemini)"
    exit 0
fi

if [[ -z "$DEST" ]]; then
    DEST="$(default_dest_for_host "$HOST")"
fi

render_host_bundle "$HOST" "$DEST"
