#!/usr/bin/env bash
set -euo pipefail

SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:-/tmp/lemoncrow-review-demo}"

rm -rf "$TARGET"
mkdir -p "$TARGET"
cp -a "$SOURCE/." "$TARGET/"

# Materialize baseline binary media before the baseline commit. The prepared
# change swaps these bytes after the commit, so Review can compare exact old/new
# GIF, video, audio, and PDF artifacts without requiring ffmpeg or other tools.
mkdir -p "$TARGET/media"
cp -a "$TARGET/.fixtures/media/before/." "$TARGET/media/"

npm install --prefix "$TARGET/frontend" --no-audit --no-fund

git -C "$TARGET" init -b main >/dev/null
git -C "$TARGET" config user.name "LemonCrow Demo"
git -C "$TARGET" config user.email "demo@lemoncrow.local"
git -C "$TARGET" add .
git -C "$TARGET" commit -m "baseline review demo" >/dev/null

"$TARGET/scripts/apply-demo-change.sh"

cat <<EOF

Demo repository ready: $TARGET

Inspect setup:
  uv run lc review --repo-root $TARGET --setup

Open Review:
  uv run lc review --repo-root $TARGET --open

The baseline commit is clean; the prepared product changes are intentionally uncommitted.

Optional third-party preview plugin for data/plans.csv (from the LemonCrow checkout):
  uv pip install -e ./demo/review-preview-plugin
EOF
