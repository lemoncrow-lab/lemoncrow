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
NAME="lemoncrow"
EMAIL="302591943+lemoncrow-agent[bot]@users.noreply.github.com"
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
# The model (from the session state SessionStart already wrote) is folded into
# the co-author name -- lemoncrow+<model> -- so a single trailer says both who
# and which model. The bot email is what GitHub links, so it is also the dedupe
# key. With no model in the session state the name is plain lemoncrow.
BODY
  echo "LEMONCROW_NAME=\"$NAME\""
  echo "LEMONCROW_EMAIL=\"$EMAIL\""
  cat <<'BODY'
LEMONCROW_STATE="$(git rev-parse --show-toplevel 2>/dev/null)/.lemoncrow/workspace/session_state.json"
lemoncrow_state_value() {
  [ -f "$LEMONCROW_STATE" ] || return 0
  sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$LEMONCROW_STATE" 2>/dev/null | head -n 1
}
case "$2" in
  merge|squash) ;;
  *)
    if ! grep -qF "<$LEMONCROW_EMAIL>" "$1" 2>/dev/null; then
      LEMONCROW_MODEL="$(printf %s "$(lemoncrow_state_value model)" | tr -c 'A-Za-z0-9._:/-' '-' | sed 's/^-*//; s/-*$//')"
      LEMONCROW_WHO="$LEMONCROW_NAME"
      if [ -n "$LEMONCROW_MODEL" ]; then LEMONCROW_WHO="$LEMONCROW_NAME+$LEMONCROW_MODEL"; fi
      printf '\nCo-Authored-By: %s <%s>\n' "$LEMONCROW_WHO" "$LEMONCROW_EMAIL" >> "$1"
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
