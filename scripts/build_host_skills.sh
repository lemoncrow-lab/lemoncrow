#!/usr/bin/env bash
# build_host_skills.sh — Generate host skill bundles from the shared packaged skill bundle
#
# Usage:
#   bash scripts/build_host_skills.sh --host codex
#   bash scripts/build_host_skills.sh --host codex --dest /tmp/codex-skills
#   bash scripts/build_host_skills.sh --host all

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LEMONCROW_REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
SKILLS_SRC="${LEMONCROW_REPO}/integrations/skills"
RENDER_SCRIPT="${SCRIPT_DIR}/sync_agent_context.py"

HOST=""
DEST=""
INCLUDE_DEV=0
# Public skills (benchmark/orchestrate/perf-review/recall/swarm/ux-review) to
# copy from $SKILLS_SRC. Empty (the default) ships none of them -- a future
# on-demand install feature passes --include-skills=<comma-separated names> to
# opt specific ones in. HIDDEN_SKILLS below stays absolute regardless.
INCLUDE_SKILLS=""
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
        --include-dev) INCLUDE_DEV=1 ;;
        --include-skills=*) INCLUDE_SKILLS="${1#*=}" ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
    shift
done

if [[ -z "$HOST" ]]; then
    echo "--host is required (claude | codex | antigravity | all)" >&2
    exit 1
fi

# Regenerate host context files if uv is available; skip silently in binary-only envs
# (build.sh pre-generates these before packaging).
if command -v uv >/dev/null 2>&1; then
    uv run python "$RENDER_SCRIPT" >/dev/null 2>&1 || true
fi

if [[ ! -d "$SKILLS_SRC" ]]; then
    echo "Shared packaged skills directory not found: $SKILLS_SRC" >&2
    exit 1
fi

# These skill names are dev/internal and should not be exposed in host bundles.
# Kept as a static list to avoid importing the Python package at install time.
HIDDEN_SKILLS=(
    analyze-failures
    context
    evals
    rescue
    savings
    status
    record
)

# Skills that ship by default regardless of --include-skills -- currently just
# the on-demand install/remove/list discovery skill. Mirrors DEFAULT_SKILLS in
# src/lemoncrow/core/environment.py; keep both in sync.
DEFAULT_SKILLS=(
    lemoncrow
)

is_default_skill() {
    local name="$1"
    local skill
    for skill in "${DEFAULT_SKILLS[@]}"; do
        if [[ "$skill" == "$name" ]]; then
            return 0
        fi
    done
    return 1
}

ROLE_SKILLS=()
while IFS= read -r mode_path; do
    [[ -n "$mode_path" ]] && ROLE_SKILLS+=("$(basename "$mode_path" .md)")
done < <(find "${LEMONCROW_REPO}/integrations/agents" -mindepth 1 -maxdepth 1 -type f -name '*.md' | sort)

is_hidden_skill() {
    local name="$1"
    local skill
    for skill in "${HIDDEN_SKILLS[@]}"; do
        if [[ "$skill" == "$name" ]]; then
            return 0
        fi
    done
    return 1
}

# Ships none of the 6 optional public skills by default; --include-skills=<names>
# opts specific ones in (still subject to is_hidden_skill above, which is
# absolute). DEFAULT_SKILLS above always ships regardless of this flag.
is_included_skill() {
    local name="$1"
    is_default_skill "$name" && return 0
    [[ -z "$INCLUDE_SKILLS" ]] && return 1
    local skill
    IFS=',' read -ra _included_skills <<< "$INCLUDE_SKILLS"
    for skill in "${_included_skills[@]}"; do
        if [[ "$skill" == "$name" ]]; then
            return 0
        fi
    done
    return 1
}

default_dest_for_host() {
    case "$1" in
        claude) printf "%s" "${LEMONCROW_REPO}/integrations/claude/plugin/skills" ;;
        codex) printf "%s" "${LEMONCROW_REPO}/integrations/codex/plugin/skills" ;;
        antigravity) printf "%s" "${LEMONCROW_REPO}/integrations/antigravity/skills" ;;
        *)
            echo "Unknown host: $1" >&2
            exit 1
            ;;
    esac
}

render_host_bundle() {
    local host="$1"
    local dest_dir="$2"
    local generated_dir
    local skill_dir
    local skill_name
    local source_path
    local dest_path

    mkdir -p "$dest_dir"

    generated_dir="$(default_dest_for_host "$host")"
    for skill_name in "${ROLE_SKILLS[@]}"; do
        source_path="$generated_dir/$skill_name/SKILL.md"
        dest_path="$dest_dir/$skill_name/SKILL.md"
        [[ -f "$source_path" ]] || continue
        mkdir -p "$dest_dir/$skill_name"
        if [[ "$source_path" != "$dest_path" ]]; then
            cp "$source_path" "$dest_path"
        fi
    done

    while IFS= read -r skill_dir; do
        [[ -n "$skill_dir" ]] || continue
        skill_name="$(basename "$skill_dir")"
        if [[ ! -f "$skill_dir/SKILL.md" ]]; then
            continue
        fi
        if is_hidden_skill "$skill_name"; then
            continue
        fi
        if ! is_included_skill "$skill_name"; then
            continue
        fi
        mkdir -p "$dest_dir/$skill_name"
        cp "$skill_dir/SKILL.md" "$dest_dir/$skill_name/SKILL.md"
    done < <(find "$SKILLS_SRC" -mindepth 1 -maxdepth 1 -type d | sort)

    echo "[lemoncrow:skills] generated ${host} bundle -> ${dest_dir}"
}

if [[ "$HOST" == "all" ]]; then
    if [[ -n "$DEST" ]]; then
        echo "--dest cannot be used with --host all" >&2
        exit 1
    fi
    render_host_bundle "claude" "$(default_dest_for_host claude)"
    render_host_bundle "codex" "$(default_dest_for_host codex)"
    render_host_bundle "antigravity" "$(default_dest_for_host antigravity)"
    exit 0
fi

if [[ -z "$DEST" ]]; then
    DEST="$(default_dest_for_host "$HOST")"
fi

render_host_bundle "$HOST" "$DEST"
