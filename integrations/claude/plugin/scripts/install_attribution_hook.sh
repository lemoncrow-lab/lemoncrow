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
# The committing session's own model is folded into the co-author name --
# lemoncrow+<model> -- so a single trailer says both who and which model. The
# session is the one whose id the host exports to this commit (e.g.
# CLAUDE_CODE_SESSION_ID); its model is read from that session's own
# sessions/<date>/<host>/<id>/stats.json, never from state other sessions share.
# The bot email is what GitHub links, so it is also the dedupe key. Without the
# session's own model the name is plain lemoncrow.
BODY
  echo "LEMONCROW_NAME=\"$NAME\""
  echo "LEMONCROW_EMAIL=\"$EMAIL\""
  cat <<'BODY'
lemoncrow_json_value() {
  [ -f "$2" ] || return 0
  sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\([^\"]*\)\".*/\1/p" "$2" 2>/dev/null | head -n 1
}
lemoncrow_own_model() {
  for LEMONCROW_PASS in agent any; do
    for LEMONCROW_PAIR in CLAUDE_CODE_SESSION_ID:claude ANTIGRAVITY_SESSION_ID:antigravity AGY_SESSION_ID:antigravity CODEX_SESSION_ID:codex OPENCODE_SESSION_ID:opencode CURSOR_SESSION_ID:cursor HERMES_SESSION_ID:hermes GITHUB_COPILOT_SESSION_ID:copilot; do
      LEMONCROW_HOST="${LEMONCROW_PAIR#*:}"
      if [ "$LEMONCROW_PASS" = agent ] && [ "$LEMONCROW_HOST" != "${LEMONCROW_AGENT:-}" ]; then continue; fi
      eval "LEMONCROW_SID=\"\${${LEMONCROW_PAIR%%:*}:-}\""
      [ -n "$LEMONCROW_SID" ] || continue
      case "$LEMONCROW_SID" in .*|*[!A-Za-z0-9._-]*) return 0 ;; esac
      for LEMONCROW_STATS in "${LEMONCROW_ROOT:-$HOME/.lemoncrow}"/sessions/*/*/*/"$LEMONCROW_HOST"/"$LEMONCROW_SID"/stats.json; do
        LEMONCROW_M="$(lemoncrow_json_value last_model "$LEMONCROW_STATS")"
        [ -n "$LEMONCROW_M" ] || LEMONCROW_M="$(lemoncrow_json_value model "$LEMONCROW_STATS")"
        if [ -n "$LEMONCROW_M" ]; then printf %s "$LEMONCROW_M"; return 0; fi
      done
      return 0
    done
  done
}
case "$2" in
  merge|squash) ;;
  *)
    if ! grep -qF "<$LEMONCROW_EMAIL>" "$1" 2>/dev/null; then
      LEMONCROW_MODEL="$(printf %s "$(lemoncrow_own_model)" | tr -c 'A-Za-z0-9._:/-' '-' | sed 's/^-*//; s/-*$//')"
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
