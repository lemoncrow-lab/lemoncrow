#!/usr/bin/env bash
# Install a repo-local prepare-commit-msg hook that appends LemonCrow's
# Co-Authored-By trailer to commit messages. Idempotent and safe to re-run.
#
# Usage:  bash install_attribution_hook.sh [repo_dir]   (defaults to .)
#
# Pairs with the `attribution` plugin setting: when on, SessionStart sets
# includeCoAuthoredBy=false so Claude Code's own trailer is suppressed and this
# LemonCrow trailer becomes the single co-author line.
#
# Caveat: if the repo already has a prepare-commit-msg hook that exits before
# our appended block runs, the trailer may not be added (the script warns).
set -euo pipefail

REPO_DIR="${1:-.}"
TRAILER="Co-Authored-By: lemoncrow <302591943+lemoncrow-agent[bot]@users.noreply.github.com>"
MARKER="# >>> lemoncrow attribution >>>"
END_MARKER="# <<< lemoncrow attribution <<<"

if ! hooks_dir="$(git -C "$REPO_DIR" rev-parse --git-path hooks 2>/dev/null)"; then
  echo "error: $REPO_DIR is not a git repository" >&2
  exit 1
fi
case "$hooks_dir" in
  /*) : ;;
  *) hooks_dir="$REPO_DIR/$hooks_dir" ;;
esac
mkdir -p "$hooks_dir"
hook="$hooks_dir/prepare-commit-msg"

emit_block() {
  echo "$MARKER"
  cat <<'BODY'
# Managed by LemonCrow (install_attribution_hook.sh). Appends the co-author
# trailer unless already present. Skips merge/squash commit messages.
#
# LemonCrow-Session / LemonCrow-Model make a committed range an exact join with
# the session that authored it, instead of a wall-clock guess. Both are read
# from the session state SessionStart already wrote, and are omitted entirely
# when unknown -- an absent trailer is honest, an invented one is not.
BODY
  echo "LEMONCROW_TRAILER=\"$TRAILER\""
  cat <<'BODY'
LEMONCROW_STATE="$(git rev-parse --show-toplevel 2>/dev/null)/.lemoncrow/workspace/session_state.json"
lemoncrow_state_value() {
  [ -f "$LEMONCROW_STATE" ] || return 0
  sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$LEMONCROW_STATE" 2>/dev/null | head -n 1
}
case "$2" in
  merge|squash) ;;
  *)
    if ! grep -qF "$LEMONCROW_TRAILER" "$1" 2>/dev/null; then
      printf '\n%s\n' "$LEMONCROW_TRAILER" >> "$1"
    fi
    LEMONCROW_SID="$(lemoncrow_state_value session_id)"
    if [ -n "$LEMONCROW_SID" ] && ! grep -q '^LemonCrow-Session:' "$1" 2>/dev/null; then
      printf 'LemonCrow-Session: %s\n' "$LEMONCROW_SID" >> "$1"
    fi
    LEMONCROW_MODEL="$(lemoncrow_state_value model)"
    if [ -n "$LEMONCROW_MODEL" ] && ! grep -q '^LemonCrow-Model:' "$1" 2>/dev/null; then
      printf 'LemonCrow-Model: %s\n' "$LEMONCROW_MODEL" >> "$1"
    fi
    ;;
esac
BODY
  echo "$END_MARKER"
}

if [ -f "$hook" ]; then
  if grep -qF "$MARKER" "$hook"; then
    echo "LemonCrow attribution hook already installed at $hook"
    exit 0
  fi
  echo "warning: existing prepare-commit-msg found; appending LemonCrow block." >&2
  echo "         If that hook exits early, the trailer may not be added." >&2
  { echo ""; emit_block; } >>"$hook"
else
  { echo "#!/usr/bin/env bash"; echo ""; emit_block; } >"$hook"
fi
chmod +x "$hook"
echo "Installed LemonCrow attribution hook at $hook"
