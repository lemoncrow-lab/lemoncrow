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

lemoncrow_resolve_version() {
    local repo_root="${1:-${LEMONCROW_REPO:-}}"
    local version=""

    if [[ -n "$repo_root" && -f "$repo_root/pyproject.toml" ]]; then
        version="$(PROJECT_PYPROJECT="$repo_root/pyproject.toml" python3 - <<'PYEOF' 2>/dev/null || true
import os
import re
from pathlib import Path

text = Path(os.environ["PROJECT_PYPROJECT"]).read_text(encoding="utf-8")
match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
print(match.group(1) if match else "")
PYEOF
)"
    fi

    if [[ -z "$version" ]]; then
        version="$(python3 - <<'PYEOF' 2>/dev/null || true
from importlib.metadata import PackageNotFoundError, version

try:
    print(version("lemoncrow"))
except PackageNotFoundError:
    print("")
PYEOF
)"
    fi

    if [[ -z "$version" ]] && command -v lc >/dev/null 2>&1; then
        version="$(lc --version 2>/dev/null | sed -n 's/^lc, version //p' | head -n 1)"
    fi

    if [[ -z "$version" ]]; then
        echo "[lc] ERROR: could not resolve LemonCrow version" >&2
        return 1
    fi

    printf '%s\n' "$version"
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

lemoncrow_apply_reply_register_level() {
    # Rewrite the baked-in ultra reply-register in staged agent files to the
    # active level: $LEMONCROW_TELEGRAPHIC env, else the cli.telegraphic key in
    # <root>/plugin_settings.json (ultra|lite|off). ultra/unset = no-op. Self-contained
    # mirror of lemoncrow.core.reply_register.apply_reply_register_level — keep in
    # sync. $1 = file or directory (recurses over *.md, *.mdc, *.toml).
    local target="$1"
    local dry_run="${2:-false}"
    local repo_root="${LEMONCROW_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
    local shared_dir="${repo_root}/integrations/agents/shared"
    [[ -f "${shared_dir}/reply-register.md" ]] || return 0

    if [[ "$dry_run" == "true" ]]; then
        echo "  [dry-run] apply reply-register level to $target"
        return
    fi

    LEMONCROW_RR_TARGET="$target" LEMONCROW_RR_SHARED="$shared_dir" python3 - <<'PYEOF'
import json
import os
from pathlib import Path

level = (os.environ.get("LEMONCROW_TELEGRAPHIC") or "").strip().lower()
if not level:
    try:
        root = Path(os.environ.get("LEMONCROW_ROOT", "").strip() or (Path.home() / ".lemoncrow"))
        raw = json.loads((root / "plugin_settings.json").read_text(encoding="utf-8"))
        nested = raw.get("lemoncrow")
        if isinstance(nested, dict) and "cli.telegraphic" in nested:
            level = str(nested["cli.telegraphic"]).strip().lower()
        else:
            level = str(raw.get("cli.telegraphic", "")).strip().lower()
    except Exception:
        level = ""
if level not in ("lite", "off"):
    raise SystemExit(0)  # ultra/unset/unknown -> keep files as shipped

shared = Path(os.environ["LEMONCROW_RR_SHARED"])
default_body = (shared / "reply-register.md").read_text(encoding="utf-8").strip()
repl = "" if level == "off" else (shared / "reply-register-lite.md").read_text(encoding="utf-8").strip()
pairs = [(default_body, repl)]
bullet_path = shared / "telegraphic-default.md"
if bullet_path.exists():
    bullet = bullet_path.read_text(encoding="utf-8").strip()
    if bullet:
        pairs += [(bullet + "\n", ""), (bullet, "")]


def toml_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


target = Path(os.environ["LEMONCROW_RR_TARGET"])
files = [target] if target.is_file() else [p for p in target.rglob("*") if p.suffix in (".md", ".mdc", ".toml")]
changed = 0
for p in files:
    text = p.read_text(encoding="utf-8")
    out = text
    for raw_needle, raw_sub in pairs:
        for needle, sub in ((raw_needle, raw_sub), (toml_escape(raw_needle), toml_escape(raw_sub))):
            if needle in out:
                out = out.replace(needle, sub)
    if out == text:
        continue
    while "\n\n\n" in out:
        out = out.replace("\n\n\n", "\n\n")
    p.write_text(out, encoding="utf-8")
    changed += 1
if changed:
    print(f"[lc] reply-register level '{level}' applied to {changed} file(s)")
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

lemoncrow_install_attribution_hook() {
    local repo_dir="${1:-.}"
    local dry_run="${2:-false}"
    local hooks_dir hook trailer marker end_marker

    trailer="Co-Authored-By: lemoncrow <302591943+lemoncrow-agent[bot]@users.noreply.github.com>"
    marker="# >>> lemoncrow attribution >>>"
    end_marker="# <<< lemoncrow attribution <<<"

    if ! git -C "$repo_dir" rev-parse --git-dir >/dev/null 2>&1; then
        warn "LemonCrow attribution skipped: ${repo_dir} is not a git repository"
        return 0
    fi

    hooks_dir="$(git -C "$repo_dir" rev-parse --git-path hooks 2>/dev/null)" || {
        warn "LemonCrow attribution skipped: cannot resolve git hooks path for ${repo_dir}"
        return 0
    }
    case "$hooks_dir" in
        /*) : ;;
        *) hooks_dir="${repo_dir}/${hooks_dir}" ;;
    esac
    hook="${hooks_dir}/prepare-commit-msg"

    if [[ "$dry_run" == "true" ]]; then
        echo "  [dry-run] install LemonCrow co-author hook at ${hook}"
        return 0
    fi

    mkdir -p "$hooks_dir"
    if [ -f "$hook" ] && grep -qF "$marker" "$hook"; then
        info "LemonCrow co-author hook already installed at ${hook}"
        return 0
    fi

    if [ -f "$hook" ]; then
        warn "existing prepare-commit-msg found; appending LemonCrow co-author block (${hook})"
    else
        printf '#!/usr/bin/env bash\n\n' >"$hook"
    fi

    cat >>"$hook" <<EOF
$marker
# Managed by LemonCrow. Appends the co-author trailer unless already present.
# Skips merge/squash commit messages.
LEMONCROW_TRAILER="$trailer"
case "\$2" in
  merge|squash) ;;
  *)
    if ! grep -qF "\$LEMONCROW_TRAILER" "\$1" 2>/dev/null; then
      printf '\n%s\n' "\$LEMONCROW_TRAILER" >> "\$1"
    fi
    ;;
esac
$end_marker
EOF
    chmod +x "$hook"
    info "installed LemonCrow co-author hook at ${hook}"
}
