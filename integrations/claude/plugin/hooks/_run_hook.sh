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

resolve_atelier_python() {
    if [[ -n "${ATELIER_PYTHON:-}" && -x "${ATELIER_PYTHON}" ]]; then
        if "${ATELIER_PYTHON}" -c "import atelier" 2>/dev/null; then
            echo "${ATELIER_PYTHON}"; return 0
        fi
    fi

    local wrapper py
    wrapper="$(command -v atelier 2>/dev/null || true)"
    if [[ -n "${wrapper}" ]]; then
        # The wrapper exec's atelier.real in the uv venv; the python lives next to it.
        local real venv_bin
        real="$(grep -oE '"[^"]*atelier.real"' "${wrapper}" 2>/dev/null | head -1 | tr -d '"')"
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
    cached_py="$(cat "${_PYTHON_CACHE}")"
    if [[ -x "${cached_py}" ]] && "${cached_py}" -c "import atelier" 2>/dev/null; then
        exec "${cached_py}" "$@"
    fi
fi

PY="$(resolve_atelier_python)"
mkdir -p "$(dirname "${_PYTHON_CACHE}")"
echo "${PY}" > "${_PYTHON_CACHE}"
exec "${PY}" "$@"
