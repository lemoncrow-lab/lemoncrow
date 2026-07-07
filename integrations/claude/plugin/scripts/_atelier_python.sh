#!/usr/bin/env bash
# _atelier_python.sh — prints the path to a Python interpreter that has
# `atelier` importable. Used by hooks and the statusline so that bundled
# scripts can `from atelier.core.capabilities... import ...` regardless of
# the user's system `python3`.
#
# Resolution order:
#   1. $ATELIER_PYTHON env override
#   2. atelier wrapper → its sibling venv python
#   3. ~/.local/share/uv/tools/atelier/bin/python (uv tool default)
#   4. python3 (silent fallback)

resolve_atelier_python() {
    if [[ -n "${ATELIER_PYTHON:-}" && -x "${ATELIER_PYTHON}" ]]; then
        if "${ATELIER_PYTHON}" -c "import atelier" 2>/dev/null; then
            echo "${ATELIER_PYTHON}"; return 0
        fi
    fi

    local wrapper real venv_bin py
    wrapper="$(command -v atelier 2>/dev/null || true)"
    if [[ -n "${wrapper}" ]]; then
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

    echo "python3"
}

resolve_atelier_python
