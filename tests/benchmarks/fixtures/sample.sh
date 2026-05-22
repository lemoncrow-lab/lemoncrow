#!/usr/bin/env bash
# Synthetic A/B fixture — Bash deployment helper with retry and rollback.
# NOT for production use. Generated to exercise tree-sitter outline.

set -euo pipefail

# ---------- Constants ----------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_FILE="${LOG_FILE:-/tmp/deploy.log}"
DEPLOY_TIMEOUT="${DEPLOY_TIMEOUT:-120}"
HEALTH_RETRIES="${HEALTH_RETRIES:-5}"
HEALTH_DELAY="${HEALTH_DELAY:-3}"
ROLLBACK_ENABLED="${ROLLBACK_ENABLED:-true}"
ENVIRONMENT="${ENVIRONMENT:-staging}"

# ---------- Logging ----------

log_info()  { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] [INFO]  $*" | tee -a "$LOG_FILE"; }
log_warn()  { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] [WARN]  $*" | tee -a "$LOG_FILE" >&2; }
log_error() { echo "[$(date '+%Y-%m-%dT%H:%M:%S')] [ERROR] $*" | tee -a "$LOG_FILE" >&2; }
log_debug() { [[ "${DEBUG:-0}" == "1" ]] && echo "[$(date '+%Y-%m-%dT%H:%M:%S')] [DEBUG] $*" | tee -a "$LOG_FILE"; true; }

# ---------- Dependency checks ----------

check_deps() {
    local missing=0
    for cmd in docker jq curl git; do
        if ! command -v "$cmd" &>/dev/null; then
            log_error "Required command not found: $cmd"
            missing=1
        fi
    done
    [[ $missing -eq 0 ]] || { log_error "Install missing dependencies and retry"; return 1; }
    log_info "All dependencies found"
}

# ---------- Git helpers ----------

git_short_sha() {
    git -C "${1:-.}" rev-parse --short HEAD 2>/dev/null || echo "unknown"
}

git_branch() {
    git -C "${1:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "detached"
}

git_is_clean() {
    git -C "${1:-.}" diff --quiet HEAD 2>/dev/null
}

# ---------- Docker helpers ----------

docker_build() {
    local image="$1" tag="$2" context="${3:-.}"
    log_info "Building image $image:$tag from $context"
    docker build \
        --tag "$image:$tag" \
        --tag "$image:latest" \
        --label "git.sha=$(git_short_sha)" \
        --label "git.branch=$(git_branch)" \
        --label "build.ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
        --file "$context/Dockerfile" \
        "$context"
}

docker_push() {
    local image="$1" tag="$2"
    log_info "Pushing $image:$tag"
    docker push "$image:$tag"
    docker push "$image:latest"
}

docker_pull_or_build() {
    local image="$1" tag="$2" context="${3:-.}"
    if docker pull "$image:$tag" &>/dev/null; then
        log_info "Pulled $image:$tag from registry"
    else
        log_warn "Pull failed, building locally"
        docker_build "$image" "$tag" "$context"
    fi
}

# ---------- Service control ----------

service_start() {
    local service="$1" compose_file="${2:-docker-compose.yml}"
    log_info "Starting service: $service"
    docker compose -f "$compose_file" up -d --no-deps "$service"
}

service_stop() {
    local service="$1" compose_file="${2:-docker-compose.yml}"
    log_info "Stopping service: $service"
    docker compose -f "$compose_file" stop "$service"
}

service_rollback() {
    local service="$1" prev_tag="$2" compose_file="${3:-docker-compose.yml}"
    if [[ "$ROLLBACK_ENABLED" != "true" ]]; then
        log_warn "Rollback disabled by ROLLBACK_ENABLED=false"
        return 0
    fi
    log_warn "Rolling back $service to $prev_tag"
    IMAGE=$(docker compose -f "$compose_file" config | grep 'image:' | grep "$service" | awk '{print $2}' | head -1)
    IMAGE="${IMAGE%%:*}"
    docker tag "$IMAGE:$prev_tag" "$IMAGE:latest"
    service_stop "$service" "$compose_file"
    service_start "$service" "$compose_file"
}

# ---------- Health checks ----------

wait_healthy() {
    local url="$1" retries="${2:-$HEALTH_RETRIES}" delay="${3:-$HEALTH_DELAY}"
    log_info "Health check: $url (${retries} retries, ${delay}s delay)"
    for i in $(seq 1 "$retries"); do
        local status
        status=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" || echo 000)
        if [[ "$status" == "200" ]]; then
            log_info "Health check passed (attempt $i)"
            return 0
        fi
        log_warn "Health check attempt $i/$retries failed (HTTP $status)"
        sleep "$delay"
    done
    log_error "Service failed health check after $retries attempts"
    return 1
}

# ---------- Notification ----------

notify_slack() {
    local webhook="$1" message="$2" color="${3:-good}"
    [[ -z "$webhook" ]] && return 0
    local payload
    payload=$(jq -cn --arg txt "$message" --arg col "$color" '{
        attachments: [{text: $txt, color: $col}]
    }')
    curl -s -X POST -H 'Content-type: application/json' --data "$payload" "$webhook" || true
}

# ---------- Main deploy flow ----------

deploy() {
    local service="$1" image="$2" tag="$3" health_url="${4:-}"
    local prev_tag
    prev_tag=$(docker inspect --format '{{index .Config.Image}}' "$service" 2>/dev/null | cut -d: -f2 || echo "")

    log_info "Deploying $service -> $image:$tag (prev=$prev_tag)"

    if ! docker_pull_or_build "$image" "$tag"; then
        log_error "Failed to obtain image $image:$tag"
        return 1
    fi

    service_stop "$service"
    service_start "$service"

    if [[ -n "$health_url" ]]; then
        if ! wait_healthy "$health_url"; then
            [[ -n "$prev_tag" ]] && service_rollback "$service" "$prev_tag"
            return 1
        fi
    fi

    log_info "Deploy of $service complete"
    notify_slack "${SLACK_WEBHOOK:-}" "$service deployed ($tag) in $ENVIRONMENT" "good"
    return 0
}

# ---------- Entrypoint ----------

usage() {
    echo "Usage: $0 <service> <image> <tag> [health_url]"
    exit 1
}

main() {
    [[ $# -lt 3 ]] && usage
    check_deps
    deploy "$@"
}

[[ "${BASH_SOURCE[0]}" == "$0" ]] && main "$@"

# ---------- Utility functions ----------

retry() {
    local retries="$1" delay="$2"
    shift 2
    local attempt=0
    until "$@"; do
        attempt=$(( attempt + 1 ))
        if (( attempt >= retries )); then
            log_error "Command failed after $retries attempts: $*"
            return 1
        fi
        log_warn "Retry $attempt/$retries for: $*"
        sleep "$delay"
    done
}

ensure_dir() {
    local dir="$1"
    if [[ ! -d "$dir" ]]; then
        mkdir -p "$dir" && log_info "Created directory: $dir"
    fi
}

wait_for_port() {
    local host="$1" port="$2" timeout="${3:-30}"
    local deadline=$(( SECONDS + timeout ))
    log_info "Waiting for $host:$port (timeout ${timeout}s)"
    while (( SECONDS < deadline )); do
        if bash -c "echo >/dev/tcp/$host/$port" 2>/dev/null; then
            log_info "Port $host:$port is open"
            return 0
        fi
        sleep 1
    done
    log_error "Timeout waiting for $host:$port"
    return 1
}

tempdir_cleanup() {
    local dir
    dir=$(mktemp -d)
    trap "rm -rf '$dir'" EXIT
    echo "$dir"
}

parse_json_field() {
    local json="$1" field="$2"
    echo "$json" | jq -r ".${field} // empty"
}

require_env() {
    local var
    for var in "$@"; do
        if [[ -z "${!var:-}" ]]; then
            log_error "Required environment variable not set: $var"
            return 1
        fi
    done
}
