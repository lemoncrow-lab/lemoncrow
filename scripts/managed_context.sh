#!/usr/bin/env bash

LEMONCROW_CODE_BLOCK_START="<!-- LEMONCROW START -->"
LEMONCROW_CODE_BLOCK_END="<!-- LEMONCROW END -->"

lemoncrow_resolve_install_profile() {
    local host_tag="${1:-lemoncrow}"
    local repo_root="${2:-${LEMONCROW_REPO:-}}"
    local output
    local -a profile_lines=()

    if [[ -z "$repo_root" ]]; then
        repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    fi

    if ! output="$({
        PYTHONPATH="${repo_root}/src:${PYTHONPATH:-}" python3 - "$host_tag" <<'PY'
import sys

from lemoncrow.core.environment import install_profile_warning, resolve_install_profile

host_tag = sys.argv[1]

try:
    profile = resolve_install_profile()
except ValueError as exc:
    print(f"[{host_tag}] ERROR: {exc}", file=sys.stderr)
    raise SystemExit(1)

print(profile)
print(install_profile_warning(profile) or "")
PY
    } 2>&1)"; then
        printf '%s\n' "$output" >&2
        echo "[${host_tag}] ERROR: failed to resolve install profile" >&2
        return 1
    fi

    while IFS= read -r line; do
        profile_lines+=("$line")
    done <<<"$output"
    INSTALL_PROFILE="${profile_lines[0]:-}"
    LEMONCROW_INSTALL_PROFILE_WARNING="${profile_lines[1]:-}"

    if [[ -z "$INSTALL_PROFILE" ]]; then
        echo "[${host_tag}] ERROR: failed to resolve install profile" >&2
        return 1
    fi
}

lemoncrow_write_managed_copy() {
    local source_file="$1"
    local dest_file="$2"
    local dry_run="${3:-false}"

    if [[ "$dry_run" == "true" ]]; then
        echo "  [dry-run] write managed LemonCrow context to $dest_file"
        return
    fi

    mkdir -p "$(dirname "$dest_file")"
    python3 - <<PYEOF
from pathlib import Path
import re

source = Path("$source_file").read_text(encoding="utf-8").strip()
pattern = re.compile(
    rf"^{re.escape('$LEMONCROW_CODE_BLOCK_START')}\n(.*)\n{re.escape('$LEMONCROW_CODE_BLOCK_END')}$",
    re.DOTALL,
)
match = pattern.fullmatch(source)
if match:
    source = match.group(1).strip()

if source.startswith("---"):
    # Frontmatter must be at the very top for Claude to parse colors/tools
    Path("$dest_file").write_text(source + "\n", encoding="utf-8")
else:
    managed = "$LEMONCROW_CODE_BLOCK_START\n" + source + "\n$LEMONCROW_CODE_BLOCK_END\n"
    Path("$dest_file").write_text(managed, encoding="utf-8")
PYEOF
}

lemoncrow_upsert_managed_block() {
    local source_file="$1"
    local dest_file="$2"
    local dry_run="${3:-false}"

    if [[ "$dry_run" == "true" ]]; then
        echo "  [dry-run] replace or append managed LemonCrow context in $dest_file"
        return
    fi

    mkdir -p "$(dirname "$dest_file")"
    python3 - <<PYEOF
from pathlib import Path
import re

source = Path("$source_file").read_text(encoding="utf-8").strip()
source_pattern = re.compile(
    rf"^{re.escape('$LEMONCROW_CODE_BLOCK_START')}\n(.*)\n{re.escape('$LEMONCROW_CODE_BLOCK_END')}$",
    re.DOTALL,
)
source_match = source_pattern.fullmatch(source)
if source_match:
    source = source_match.group(1).strip()
managed = "$LEMONCROW_CODE_BLOCK_START\n" + source + "\n$LEMONCROW_CODE_BLOCK_END"
dest_path = Path("$dest_file")
existing = dest_path.read_text(encoding="utf-8").rstrip() if dest_path.exists() else ""
pattern = re.compile(
    rf"{re.escape('$LEMONCROW_CODE_BLOCK_START')}.*?{re.escape('$LEMONCROW_CODE_BLOCK_END')}\n?",
    re.DOTALL,
)

if existing.strip() == source:
    updated = managed
elif pattern.search(existing):
    updated = pattern.sub(managed, existing, count=1).rstrip()
elif existing:
    updated = f"{existing}\n\n---\n\n{managed}".rstrip()
else:
    updated = managed

dest_path.write_text(updated + "\n", encoding="utf-8")
PYEOF
}

lemoncrow_remove_managed_block() {
    local dest_file="$1"
    local dry_run="${2:-false}"

    if [[ ! -f "$dest_file" ]]; then
        echo "unchanged"
        return
    fi

    if [[ "$dry_run" == "true" ]]; then
        if grep -q "$LEMONCROW_CODE_BLOCK_START" "$dest_file" 2>/dev/null; then
            echo "dry-run-remove"
        else
            echo "unchanged"
        fi
        return
    fi

    python3 - <<PYEOF
from pathlib import Path
import re

path = Path("$dest_file")
text = path.read_text(encoding="utf-8")
pattern = re.compile(
    rf"\n*{re.escape('$LEMONCROW_CODE_BLOCK_START')}\n.*?{re.escape('$LEMONCROW_CODE_BLOCK_END')}\n*",
    re.DOTALL,
)

if pattern.search(text):
    updated = pattern.sub("\n\n", text, count=1)
    updated = re.sub(r"\n{3,}", "\n\n", updated).strip()
    if updated:
        path.write_text(updated + "\n", encoding="utf-8")
        print("updated")
    else:
        path.unlink()
        print("removed")
else:
    print("unchanged")
PYEOF
}
