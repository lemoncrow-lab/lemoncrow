#!/usr/bin/env bash
# Thin entry for the empty Review state. The app itself lives in ../demo.
# usage: bootstrap.sh <dir> [extra ../demo/bootstrap.sh args]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${1:?usage: bootstrap.sh <dir> [args]}"
shift
exec "$HERE/../demo/bootstrap.sh" "$TARGET" --scenario empty "$@"
