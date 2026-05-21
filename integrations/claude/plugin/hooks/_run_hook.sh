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
#   2. resolve atelier-mcp on PATH → its sibling venv bin/python
#   3. ~/.local/share/uv/tools/atelier/bin/python (uv tool default)
#   4. system python3 (silent no-op fallback, matches old behavior)

set -u

resolve_atelier_python() {
    if [[ -n "${ATELIER_PYTHON:-}" && -x "${ATELIER_PYTHON}" ]]; then
        if "${ATELIER_PYTHON}" -c "import atelier" 2>/dev/null; then
            echo "${ATELIER_PYTHON}"; return 0
        fi
    fi

    local mcp_wrapper py
    mcp_wrapper="$(command -v atelier-mcp 2>/dev/null || true)"
    if [[ -n "${mcp_wrapper}" ]]; then
        # The wrapper exec's atelier-mcp.real in the uv venv; the python lives next to it.
        local real venv_bin
        real="$(grep -oE '"[^"]*atelier-mcp.real"' "${mcp_wrapper}" 2>/dev/null | head -1 | tr -d '"')"
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

    echo "python3"
}

PY="$(resolve_atelier_python)"
exec "${PY}" "$@"
