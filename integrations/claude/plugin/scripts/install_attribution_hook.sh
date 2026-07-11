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
TRAILER="Co-Authored-By: LemonCrow <293447754+lemoncrow@users.noreply.github.com>"
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
  cat <<EOF
$MARKER
# Managed by LemonCrow (install_attribution_hook.sh). Appends the co-author
# trailer unless already present. Skips merge/squash commit messages.
LEMONCROW_TRAILER="$TRAILER"
case "\$2" in
  merge|squash) ;;
  *)
    if ! grep -qF "\$LEMONCROW_TRAILER" "\$1" 2>/dev/null; then
      printf '\n%s\n' "\$LEMONCROW_TRAILER" >> "\$1"
    fi
    ;;
esac
$END_MARKER
EOF
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
