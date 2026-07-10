#!/usr/bin/env bash
# _run_hook.sh — runs a plugin hook script with the Python interpreter that
# has the `atelier` package importable. The plugin's hook scripts import from
# atelier.core.capabilities.plugin_runtime, so they need atelier's venv,
# not the system python3.
#
# Usage:  _run_hook.sh /path/to/hook.py [args...]
#
# Tries, in order:
#   1. $ATELIER_PYTHON env override
#   2. resolve atelier on PATH → its sibling venv bin/python
#   3. ~/.local/share/uv/tools/atelier/bin/python (uv tool default)
#   4. path stored in ../atelier-python (written by install_claude.sh)
#   5. system python3 (silent no-op fallback, matches old behavior)

set -u

_PYTHON_CACHE="${XDG_CACHE_HOME:-$HOME/.cache}/atelier/hook_python"

# Cheap fingerprint of "where would atelier resolve right now" -- a PATH
# lookup, no python subprocess. A reinstall (make dev / make prod /
# install.sh) can move atelier to a new venv without removing the old one
# (e.g. ~/.local/...uv/tools vs ~/.atelier/uv-tools); the old interpreter
# still happily satisfies "import atelier" (it's a real, just stale, install),
# so that check alone never notices the switch. Pin the cache to this
# fingerprint too so a changed resolution invalidates it immediately.
_current_fingerprint() {
    local config_file
    config_file="$(dirname "$0")/../atelier-python"
    printf '%s\n%s\n%s\n' "${ATELIER_PYTHON:-}" "$(command -v atelier 2>/dev/null || true)" "$(cat "${config_file}" 2>/dev/null || true)"
}

resolve_atelier_python() {
    if [[ -n "${ATELIER_PYTHON:-}" && -x "${ATELIER_PYTHON}" ]]; then
        if "${ATELIER_PYTHON}" -c "import atelier" 2>/dev/null; then
            echo "${ATELIER_PYTHON}"; return 0
        fi
    fi

    local wrapper py shebang
    wrapper="$(command -v atelier 2>/dev/null || true)"
    if [[ -n "${wrapper}" ]]; then
        # Modern uv tool wrappers are a python script whose shebang IS the venv
        # interpreter (e.g. "#!/Users/x/.atelier/uv-tools/atelier/bin/python").
        # Older uv versions embedded a literal "...atelier.real" path instead --
        # check both so this resolves regardless of uv wrapper generation.
        shebang="$(head -1 "${wrapper}" 2>/dev/null | sed -n 's/^#!//p')"
        if [[ -x "${shebang}" ]] && "${shebang}" -c "import atelier" 2>/dev/null; then
            echo "${shebang}"; return 0
        fi
        # The wrapper exec's atelier.real in the uv venv; the python lives next to it.
        local real venv_bin
        real="$(grep -oE '"[^"]*atelier\.real"' "${wrapper}" 2>/dev/null | head -1 | tr -d '"')"
        if [[ -x "${real}" ]]; then
            venv_bin="$(dirname "${real}")"
            for py in "${venv_bin}/python" "${venv_bin}/python3"; do
                if [[ -x "${py}" ]] && "${py}" -c "import atelier" 2>/dev/null; then
                    echo "${py}"; return 0
                fi
            done
        fi
    fi

    for py in \
        "${HOME}/.local/share/uv/tools/atelier/bin/python" \
        "${HOME}/.local/share/uv/tools/atelier/bin/python3"; do
        if [[ -x "${py}" ]] && "${py}" -c "import atelier" 2>/dev/null; then
            echo "${py}"; return 0
        fi
    done

    # Path written by install_claude.sh at install time (handles binary / dev installs
    # where no uv-tool venv exists next to the atelier wrapper).
    local config_file
    config_file="$(dirname "$0")/../atelier-python"
    if [[ -f "${config_file}" ]]; then
        local stored_py
        stored_py="$(tr -d '[:space:]' < "${config_file}")"
        if [[ -x "${stored_py}" ]] && "${stored_py}" -c "import atelier" 2>/dev/null; then
            echo "${stored_py}"; return 0
        fi
    fi

    echo "python3"
}

if [[ -f "${_PYTHON_CACHE}" ]]; then
    cached_py="$(sed -n '1p' "${_PYTHON_CACHE}")"
    cached_fingerprint="$(tail -n +2 "${_PYTHON_CACHE}")"
    if [[ -n "${cached_py}" && "${cached_fingerprint}" == "$(_current_fingerprint)" ]] \
        && [[ -x "${cached_py}" ]]; then
        # Cache hit: exec directly, skipping the `import atelier` probe (that
        # probe cost a full python spawn on every hook call). Validation is
        # lazy: `execfail` makes a failed exec return here instead of killing
        # the shell, so we drop the stale cache and fall through to a full
        # re-resolve below.
        shopt -s execfail
        exec "${cached_py}" "$@"
        shopt -u execfail
        rm -f "${_PYTHON_CACHE}" 2>/dev/null || true
    fi
fi

PY="$(resolve_atelier_python)"
mkdir -p "$(dirname "${_PYTHON_CACHE}")"
{ echo "${PY}"; _current_fingerprint; } > "${_PYTHON_CACHE}"
exec "${PY}" "$@"
