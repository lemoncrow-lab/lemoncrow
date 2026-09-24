#!/usr/bin/env bash
# Build the architecture-neutral hosted LemonCrow client artifact.
#
# Hosted mode deliberately ships *only* lemoncrow-client. It has no runtime
# dependencies and no native extension, so one py3-none-any wheel is sufficient
# for every supported Linux/macOS architecture. The full platform distribution
# remains the local-mode artifact built by scripts/build.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
cd "$ROOT"

OUT_DIR="${LEMONCROW_HOSTED_DIST_DIR:-$ROOT/dist}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/lemoncrow-hosted-build.XXXXXX")"
trap 'rm -rf "${WORK:-}"' EXIT
mkdir -p "$OUT_DIR" "$WORK/vendor"

command -v uv >/dev/null 2>&1 || { echo "build_hosted.sh: uv is required" >&2; exit 1; }

echo "◆ Building zero-dependency hosted client wheel..."
uv build --wheel --package lemoncrow-client --out-dir "$WORK/vendor" >/dev/null
# uv may add staging metadata such as .gitignore. The published hosted payload
# is intentionally one wheel and nothing else.
find "$WORK/vendor" -maxdepth 1 -type f ! -name '*.whl' -delete
wheel="$(find "$WORK/vendor" -maxdepth 1 -type f -name 'lemoncrow_client-*-py3-none-any.whl' -print -quit)"
[[ -n "$wheel" ]] || { echo "build_hosted.sh: expected a py3-none-any lemoncrow-client wheel" >&2; exit 1; }
wheel_count="$(find "$WORK/vendor" -maxdepth 1 -type f -name '*.whl' -print | wc -l | tr -d ' ')"
[[ "$wheel_count" == "1" ]] \
    || { echo "build_hosted.sh: hosted artifact must contain exactly one wheel" >&2; exit 1; }

archive="$OUT_DIR/lemoncrow-hosted-client.tar.gz"
rm -f "$archive" "$archive.sha256"
tar -czf "$archive" -C "$WORK" vendor

if command -v sha256sum >/dev/null 2>&1; then
    digest="$(sha256sum "$archive" | awk '{print $1}')"
elif command -v shasum >/dev/null 2>&1; then
    digest="$(shasum -a 256 "$archive" | awk '{print $1}')"
else
    echo "build_hosted.sh: sha256sum or shasum is required" >&2
    exit 1
fi
printf '%s\n' "$digest" >"$archive.sha256"

echo "✓ Hosted client artifact: $archive"
echo "  $(du -sh "$archive" | awk '{print $1}')  (one py3-none-any wheel, no runtime dependencies)"
