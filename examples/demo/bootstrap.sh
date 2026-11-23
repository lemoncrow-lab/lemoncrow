#!/usr/bin/env bash
set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SOURCE/../.." && pwd)"
TARGET=""
SCENARIO="standard"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --scenario) SCENARIO="${2:?--scenario needs a name}"; shift 2 ;;
        --scenario=*) SCENARIO="${1#--scenario=}"; shift ;;
        -h|--help) echo "usage: bootstrap.sh [dir] [--scenario NAME]"; python3 "$SOURCE/scripts/apply-demo-change.py" --scenario __list__ 2>&1 | sed -n '2,$p'; exit 0 ;;
        *) TARGET="$1"; shift ;;
    esac
done
TARGET="${TARGET:-/tmp/lemoncrow-review-demo}"

# Fail fast (before touching TARGET or the server) on an unknown scenario.
python3 "$SOURCE/scripts/apply-demo-change.py" --scenario "$SCENARIO" --check

# Install into both the checkout and the local server before any Review capture.
# Provisioned demo runtimes may install the same plugin explicitly before this
# script runs so they do not restart an unrelated developer server.
if [[ "${LEMONCROW_DEMO_SKIP_PLUGIN_SETUP:-0}" != "1" ]]; then
    make -C "$REPO_ROOT" review-demo-plugin
fi
rm -rf "$TARGET"
mkdir -p "$TARGET"
cp -a "$SOURCE/." "$TARGET/"

# Materialize baseline binary media before the baseline commit. The prepared
# change swaps these bytes after the commit, so Review can compare exact old/new
# GIF, video, audio, and PDF artifacts without requiring ffmpeg or other tools.
mkdir -p "$TARGET/media"
cp -a "$TARGET/.fixtures/media/before/." "$TARGET/media/"

npm install --prefix "$TARGET/frontend" --no-audit --no-fund

# Scenario baseline additions (no-op for empty/standard) go into the baseline commit.
if [[ "$SCENARIO" != "standard" && "$SCENARIO" != "empty" ]]; then
    "$TARGET/scripts/apply-demo-change.sh" --scenario "$SCENARIO" --prepare-baseline
fi

git -C "$TARGET" init -b main >/dev/null
git -C "$TARGET" config user.name "LemonCrow Demo"
git -C "$TARGET" config user.email "demo@lemoncrow.local"
git -C "$TARGET" add .
git -C "$TARGET" commit -m "baseline review demo" >/dev/null

if [[ "$SCENARIO" == "empty" ]]; then
    # Nothing to review: clear stale demo ledgers for this workspace and stop.
    uv run --project "$REPO_ROOT" python "$SOURCE/scripts/seed-agent-activity.py" "$TARGET" --prune-only
    echo
    echo "Demo repository ready (scenario: empty): $TARGET"
    echo "The baseline is committed and clean; no Review has been captured."
    exit 0
fi

# The demo exercises this checkout, so it captures with this checkout's `lc`: an
# installed client can lag behind (an older one does not carry `--new`).
if [[ -n "${LEMONCROW_DEMO_LC_BIN:-}" ]]; then
    LC=("$LEMONCROW_DEMO_LC_BIN")
else
    LC=(uv run --project "$REPO_ROOT" lc)
fi
capture_log="$(mktemp)"
trap 'rm -f "$capture_log"' EXIT
change_state="the prepared product changes are intentionally uncommitted"
if [[ "$SCENARIO" != "standard" ]]; then
    capture=(--new --track --no-open)
    if [[ "$SCENARIO" == "tenant-cache" ]]; then
        # The agent's pull request: one commit on its own branch, reviewed against main.
        git -C "$TARGET" checkout -q -b agent/cache-invoice-list
        "$TARGET/scripts/apply-demo-change.sh" --scenario "$SCENARIO" --phase change
        git -C "$TARGET" add -A
        git -C "$TARGET" commit -q -m "Cache the invoice list for 60 seconds" -m "Adds a 60-second cache. No behavior change."
        # A commit range, so not --new (it captures only the working tree or index).
        capture=(--base main --head HEAD --title "Cache the invoice list for 60 seconds" --track --no-open)
        change_state="the agent's change is committed on agent/cache-invoice-list"
    else
        "$TARGET/scripts/apply-demo-change.sh" --scenario "$SCENARIO" --phase change
    fi
    uv run --project "$REPO_ROOT" python "$SOURCE/scripts/seed-agent-activity.py" "$TARGET" --prune-only
    case "$SCENARIO" in
        orders|orders-revision-2)
            uv run --project "$REPO_ROOT" python "$SOURCE/scripts/seed-agent-activity.py" "$TARGET" --scenario orders --ledger-only ;;
    esac
    if ! "${LC[@]}" review --repo-root "$TARGET" "${capture[@]}" >"$capture_log" 2>&1; then
        cat "$capture_log" >&2
        exit 1
    fi
    if [[ "$SCENARIO" == "tenant-cache" ]]; then
        # A commit-range capture binds no workspace for --current; the branch's status names its Review.
        status_json="$("${LC[@]}" review status --repo-root "$TARGET" --json || true)"
        review_id="$(printf '%s' "$status_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["review"]["id"])')"
        reader_url="$(printf '%s' "$status_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["reader_url"])')"
        # The reviewer's recipes, each run once on base and once on head: the X-Ray's recorded calls.
        echo "Recording before/after API runs:"
        if ! uv run --project "$REPO_ROOT" python "$SOURCE/scripts/record-demo-runs.py" "$TARGET" "$review_id" tenant-cache; then
            echo "warning: some before/after API runs were not recorded; the X-Ray shows static evidence for them" >&2
        fi
    else
        current_json="$("${LC[@]}" review --repo-root "$TARGET" --current --json)"
        review_id="$(printf '%s' "$current_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["review_id"])')"
        reader_url="$(printf '%s' "$current_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["reader_url"])')"
    fi
    final_revision=1
    if [[ "$SCENARIO" == "orders-revision-2" ]]; then
        final_revision=2
        # Revision 1 is captured; now the agent's follow-up, captured through the
        # same --continue path the standard scenario uses for its final phase.
        "$TARGET/scripts/apply-demo-change.sh" --scenario "$SCENARIO" --phase revision-2
        uv run --project "$REPO_ROOT" python "$SOURCE/scripts/seed-agent-activity.py" "$TARGET" --scenario orders --revision 2 --server-review "$review_id" --ledger-only
        if ! "${LC[@]}" review --repo-root "$TARGET" --continue "$review_id" --track --no-open >"$capture_log" 2>&1; then
            cat "$capture_log" >&2
            exit 1
        fi
    fi
    case "$SCENARIO" in
        orders|orders-revision-2)
            uv run --project "$REPO_ROOT" python "$SOURCE/scripts/seed-agent-activity.py" "$TARGET" --scenario orders --revision "$final_revision" --server-review "$review_id" --verify-only ;;
    esac
else
    "$TARGET/scripts/apply-demo-change.sh" --phase initial

    # Remove only earlier demo-owned hook ledgers for this exact workspace before
    # the server captures the new Change; otherwise Development would correctly
    # import those historical sessions into the fresh Review.
    uv run --project "$REPO_ROOT" python "$SOURCE/scripts/seed-agent-activity.py" "$TARGET" --prune-only

    if ! "${LC[@]}" review --repo-root "$TARGET" --new --track --no-open >"$capture_log" 2>&1; then
        cat "$capture_log" >&2
        exit 1
    fi
    current_json="$("${LC[@]}" review --repo-root "$TARGET" --current --json)"
    review_id="$(printf '%s' "$current_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["review_id"])')"
    reader_url="$(printf '%s' "$current_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["reader_url"])')"

    # Finish the product change only after Snapshot 1 exists. The emitted agent
    # messages are therefore captured against a real server-owned Review, and the
    # next Snapshot can own them exactly rather than backdating them.
    "$TARGET/scripts/apply-demo-change.sh" --phase final
    uv run --project "$REPO_ROOT" python "$SOURCE/scripts/seed-agent-activity.py" "$TARGET" --server-review "$review_id" --ledger-only

    if ! "${LC[@]}" review --repo-root "$TARGET" --continue "$review_id" --track --no-open >"$capture_log" 2>&1; then
        cat "$capture_log" >&2
        exit 1
    fi

    # Snapshot 2 now owns the pre-existing hook output. Import/verify against the
    # exact live Reader Review: sessions, annotations, Brief context, edit
    # provenance, SessionEnd lifecycle, and test evidence all come from real stores.
    uv run --project "$REPO_ROOT" python "$SOURCE/scripts/seed-agent-activity.py" "$TARGET" --server-review "$review_id" --verify-only

    # Record the changed API requests before and after (base and head) on Snapshot 2,
    # so the X-Ray shows recorded calls and the changed response bodies.
    echo "Recording before/after API runs:"
    if ! uv run --project "$REPO_ROOT" python "$SOURCE/scripts/record-demo-runs.py" "$TARGET" "$review_id"; then
        echo "warning: some before/after API runs were not recorded; the X-Ray shows static evidence for them" >&2
    fi
fi

cat <<EOF

Demo repository ready: $TARGET

Inspect setup:
  uv run lc review --repo-root $TARGET --setup

Open seeded Review:
  $reader_url
  ${LC[*]} review --repo-root $TARGET --open-review $review_id

The baseline commit is clean; $change_state.

CSV table previews are enabled by the demo-installed csv-table plugin.
EOF
