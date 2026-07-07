#!/usr/bin/env bash
# Install a repo-local prepare-commit-msg hook that appends Atelier's
# Co-Authored-By trailer to commit messages. Idempotent and safe to re-run.
#
# Usage:  bash install_attribution_hook.sh [repo_dir]   (defaults to .)
#
# Pairs with the `attribution` plugin setting: when on, SessionStart sets
# includeCoAuthoredBy=false so Claude Code's own trailer is suppressed and this
# Atelier trailer becomes the single co-author line.
#
# Caveat: if the repo already has a prepare-commit-msg hook that exits before
# our appended block runs, the trailer may not be added (the script warns).
set -euo pipefail

REPO_DIR="${1:-.}"
TRAILER="Co-Authored-By: atelier <293447754+atelier@users.noreply.github.com>"
MARKER="# >>> atelier attribution >>>"
END_MARKER="# <<< atelier attribution <<<"

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
  cat <<EOF
$MARKER
# Managed by Atelier (install_attribution_hook.sh). Appends the co-author
# trailer unless already present. Skips merge/squash commit messages.
ATELIER_TRAILER="$TRAILER"
case "\$2" in
  merge|squash) ;;
  *)
    if ! grep -qF "\$ATELIER_TRAILER" "\$1" 2>/dev/null; then
      printf '\n%s\n' "\$ATELIER_TRAILER" >> "\$1"
    fi
    ;;
esac
$END_MARKER
EOF
}

if [ -f "$hook" ]; then
  if grep -qF "$MARKER" "$hook"; then
    echo "Atelier attribution hook already installed at $hook"
    exit 0
  fi
  echo "warning: existing prepare-commit-msg found; appending Atelier block." >&2
  echo "         If that hook exits early, the trailer may not be added." >&2
  { echo ""; emit_block; } >>"$hook"
else
  { echo "#!/usr/bin/env bash"; echo ""; emit_block; } >"$hook"
fi
chmod +x "$hook"
echo "Installed Atelier attribution hook at $hook"
