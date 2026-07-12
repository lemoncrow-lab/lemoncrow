#!/usr/bin/env bash
# _lemoncrow_python.sh — prints the path to a Python interpreter that has
# `lc` importable. Used by hooks and the statusline so that bundled
# scripts can `from lemoncrow.core.capabilities... import ...` regardless of
# the user's system `python3`.
#
# Resolution order:
#   1. $LEMONCROW_PYTHON env override
#   2. lc wrapper → its sibling venv python
#   3. ~/.local/share/uv/tools/lemoncrow/bin/python (uv tool default)
#   4. python3 (silent fallback)

resolve_lemoncrow_python() {
    if [[ -n "${LEMONCROW_PYTHON:-}" && -x "${LEMONCROW_PYTHON}" ]]; then
        if "${LEMONCROW_PYTHON}" -c "import lemoncrow" 2>/dev/null; then
            echo "${LEMONCROW_PYTHON}"; return 0
        fi
    fi

    local wrapper real venv_bin py shebang
    wrapper="$(command -v lc 2>/dev/null || true)"
    if [[ -n "${wrapper}" ]]; then
        # Modern uv tool wrappers are a python script whose shebang IS the venv
        # interpreter (e.g. "#!/Users/x/.lemoncrow/uv-tools/lemoncrow/bin/python").
        # Older uv versions embedded a literal "...lemoncrow.real" path instead --
        # check both so this resolves regardless of uv wrapper generation.
        shebang="$(head -1 "${wrapper}" 2>/dev/null | sed -n 's/^#!//p')"
        if [[ -x "${shebang}" ]] && "${shebang}" -c "import lemoncrow" 2>/dev/null; then
            echo "${shebang}"; return 0
        fi
        real="$(grep -oE '"[^"]*lemoncrow\.real"' "${wrapper}" 2>/dev/null | head -1 | tr -d '"')"
        if [[ -x "${real}" ]]; then
            venv_bin="$(dirname "${real}")"
            for py in "${venv_bin}/python" "${venv_bin}/python3"; do
                if [[ -x "${py}" ]] && "${py}" -c "import lemoncrow" 2>/dev/null; then
                    echo "${py}"; return 0
                fi
            done
        fi
    fi

    for py in \
        "${HOME}/.local/share/uv/tools/lemoncrow/bin/python" \
        "${HOME}/.local/share/uv/tools/lemoncrow/bin/python3"; do
        if [[ -x "${py}" ]] && "${py}" -c "import lemoncrow" 2>/dev/null; then
            echo "${py}"; return 0
        fi
    done

    echo "python3"
}

resolve_lemoncrow_python
