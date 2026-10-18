#!/usr/bin/env bash
# Prove that a real previous LemonCrow release can be upgraded in place to a
# candidate local-distribution archive. Pre-v0.8 Review rows need not migrate,
# by explicit product-owner decision; new Review state must still be durable.
set -euo pipefail

PREVIOUS_TAG="${1:-latest}"
CANDIDATE_ARCHIVE="${2:-}"
[[ -n "$CANDIDATE_ARCHIVE" && -f "$CANDIDATE_ARCHIVE" ]] || {
  echo "usage: $0 PREVIOUS_TAG CANDIDATE_ARCHIVE" >&2
  exit 2
}
CANDIDATE_ARCHIVE="$(cd "$(dirname "$CANDIDATE_ARCHIVE")" && pwd)/$(basename "$CANDIDATE_ARCHIVE")"

# Isolation (scratch WORK, HOME, XDG, no session bus or service manager, private port, cleanup).
# The sourced isolation helper changes cwd to its scratch root; resolve the
# drill's supporting scripts *before* that change, not relative to scratch.
UPGRADE_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=SCRIPTDIR/release_drill_env.sh
source "$UPGRADE_SCRIPT_DIR/release_drill_env.sh"

if [[ "$PREVIOUS_TAG" == "latest" ]]; then
  PREVIOUS_TAG="$(
    curl -fsSL "https://api.github.com/repos/lemoncrow-lab/lemoncrow/releases/latest" |
      python -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])'
  )"
fi
[[ "$PREVIOUS_TAG" == v* ]] || {
  echo "could not resolve previous LemonCrow release tag: $PREVIOUS_TAG" >&2
  exit 1
}
export LEMONCROW_RELEASE_TAG="$PREVIOUS_TAG"

echo "◆ Installing previous release ${PREVIOUS_TAG} into isolated home..."
curl -fsSL "https://github.com/lemoncrow-lab/lemoncrow/releases/download/${PREVIOUS_TAG}/install.sh" -o "$WORK/previous-install.sh"
# LEMONCROW_ENGINE_INSTALL=1 selects the full engine on releases whose
# install.sh is the public product installer (it delegates to
# engine_install.sh); releases before the split ignore it.
LEMONCROW_ENGINE_INSTALL=1 bash "$WORK/previous-install.sh"

OLD_VERSION="$("$LEMONCROW_BIN_DIR/lc" --version)"
echo "  previous: $OLD_VERSION"

REPO="$WORK/repo"
STORE="$WORK/store"
mkdir -p "$REPO" "$STORE"
git -C "$REPO" init -q
git -C "$REPO" config user.email release-upgrade@lemoncrow.invalid
git -C "$REPO" config user.name "LemonCrow release upgrade"
printf 'def answer():\n    return 41\n' > "$REPO/app.py"
git -C "$REPO" add app.py
git -C "$REPO" commit -qm "base"
printf 'def answer():\n    return 42\n' > "$REPO/app.py"

LEMONCROW_URL="$DRILL_URL" "$LEMONCROW_BIN_DIR/lc" --root "$STORE" review \
  --repo-root "$REPO" --track --no-open --no-impact --no-provenance \
  --mark app.py --mark-state reviewed \
  --comment "pre-upgrade durable comment" --on app.py:L2 --json \
  > "$WORK/before.json"

DB="$STORE/lemoncrow_reviews.db"
[[ -s "$DB" ]] || { echo "previous release did not create $DB" >&2; exit 1; }
python - "$DB" "$WORK/before-counts.json" <<'PY'
import json, sqlite3, sys
db, out = sys.argv[1:]
with sqlite3.connect(db) as conn:
    counts = {
        "sessions": conn.execute("select count(*) from review_sessions").fetchone()[0],
        "revisions": conn.execute("select count(*) from review_revisions").fetchone()[0],
        "marks": conn.execute("select count(*) from review_marks").fetchone()[0],
        "annotations": conn.execute("select count(*) from annotations").fetchone()[0],
    }
if (
    counts["sessions"] < 1
    or counts["revisions"] < 1
    or counts["marks"] < 1
    or counts["annotations"] < 1
):
    raise SystemExit(f"previous release created incomplete durable Review state: {counts}")
open(out, "w", encoding="utf-8").write(json.dumps(counts))
PY

echo "◆ Installing candidate over the same installation..."
mkdir -p "$WORK/candidate"
tar -xzf "$CANDIDATE_ARCHIVE" -C "$WORK/candidate"
unset LEMONCROW_RELEASE_TAG
LEMONCROW_LOCAL_SRC="$WORK/candidate" bash "$WORK/candidate/scripts/engine_install.sh" --local

NEW_VERSION="$("$LEMONCROW_BIN_DIR/lc" --version)"
echo "  candidate: $NEW_VERSION"
[[ "$NEW_VERSION" != "$OLD_VERSION" ]] || {
  echo "candidate reports the same version as ${PREVIOUS_TAG}: $NEW_VERSION" >&2
  exit 1
}
# The installer hands the server restart to a background worker: review once it reports ready.
restart=""
for ((i = 0; i < 300; i++)); do
  restart="$(bash "$LEMONCROW_INSTALL_DIR/scripts/local_server.sh" restart-status 2>/dev/null || true)"
  [[ "$restart" == ready* || "$restart" == failed* ]] && break
  sleep 0.5
done
[[ "$restart" == ready* ]] || {
  echo "candidate local server did not become ready: ${restart:-no status}" >&2
  exit 1
}

# A mypyc miscompile surfaces only when its module is imported (a segfault, a dataclass that
# cannot be built), so import every compiled module, not just the ones one review touches.
CANDIDATE_PYTHON="$HOME/.lemoncrow/uv-tools/lemoncrow/bin/python"
"$CANDIDATE_PYTHON" -X faulthandler - <<'PY'
import importlib, importlib.machinery, inspect, pathlib
import lemoncrow

site = pathlib.Path(lemoncrow.__file__).parent.parent
names = sorted(
    ".".join((*path.relative_to(site).parent.parts, inspect.getmodulename(path.name) or "")).removesuffix(".__init__")
    for path in (site / "lemoncrow").rglob("*")
    if path.name.endswith(tuple(importlib.machinery.EXTENSION_SUFFIXES))
)
failed = []
for name in names:
    try:
        importlib.import_module(name)
    except Exception as exc:
        failed.append(f"{name}: {type(exc).__name__}: {exc}")
if not names or failed:
    raise SystemExit("compiled candidate modules failed to import:\n" + "\n".join(failed or ["none compiled"]))
print(f"✓ all {len(names)} compiled candidate modules import")
PY

# The new loopback Review server is intentionally a new durable storage
# boundary. Pre-v0.8 marks, comments and Snapshots are not imported (as
# documented in CHANGELOG.md); do not pretend to apply migrations on the old DB.
# Instead verify that new marks/comments survive a separate read via the server.
LEMONCROW_URL="$DRILL_URL" "$LEMONCROW_BIN_DIR/lc" --root "$STORE" review \
  --repo-root "$REPO" --track --no-open --no-impact --no-provenance \
  --mark app.py --mark-state reviewed \
  --comment "new-release durable comment" --on app.py:L2 --json \
  > "$WORK/after.json" 2> "$WORK/after-state.txt"

CANDIDATE_REVIEW_REF="$(python - "$WORK/after-state.txt" <<'PY'
import json, pathlib, sys
for line in reversed(pathlib.Path(sys.argv[1]).read_text().splitlines()):
    try:
        row = json.loads(line)
    except json.JSONDecodeError:
        continue
    if isinstance(row, dict) and str(row.get("review_id", "")).startswith("r/"):
        print(row["review_id"])
        break
else:
    raise SystemExit("candidate did not record a valid Review reference")
PY
)"
LEMONCROW_URL="$DRILL_URL" "$LEMONCROW_BIN_DIR/lc" --root "$STORE" review \
  --show-review "$CANDIDATE_REVIEW_REF" --json > "$WORK/after-show.json"

python "$UPGRADE_SCRIPT_DIR/verify_release_review_bootstrap.py" \
  "$DB" "$WORK/before-counts.json" "$WORK/after-state.txt" "$WORK/after-show.json"

echo "✓ ${PREVIOUS_TAG} -> candidate upgrade passed; new Review persisted (pre-v0.8 Reviews intentionally not migrated)"
