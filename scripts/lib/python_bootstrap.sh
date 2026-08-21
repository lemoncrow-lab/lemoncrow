#!/usr/bin/env bash
# python_bootstrap.sh — portable `python3` fallback for Linux and macOS.
#
# Several installer/verify/uninstall scripts shell out to a bare `python3`
# for small JSON-munging tasks. On a machine with no system Python (and no
# pip — irrelevant here since nothing below uses pip) those calls die with
# "python3: command not found" instead of degrading gracefully.
#
# Sourcing this file defines a `python3` shell FUNCTION that shadows the
# real command: bash resolves function names before searching $PATH, so
# every existing `python3 -c "..."` / `python3 - <<EOF` call site below
# keeps working completely unmodified.
#   * If a system `python3` exists, it runs unchanged (`command python3`
#     bypasses this function to reach the real binary — no recursion).
#   * Otherwise, if `uv` is on PATH, fall back to a uv-managed interpreter.
#     uv ships a fully self-contained CPython build (python-build-standalone)
#     that it downloads itself, so this works even with zero system Python
#     and zero pip. This is the same mechanism scripts/bundle.sh uses to
#     recover from uv's "invalid environment: missing python executable".
#   * Otherwise, fail with a clear message instead of a bare "not found".
#
# Works identically on Linux and macOS: `command -v`, bash functions, and
# `export -f` are portable POSIX/bash features, and uv's python-build-
# standalone downloads cover both platforms (incl. Apple Silicon).
#
# Guarded against double-sourcing (both lib/common.sh and
# lib/managed_context.sh source this file).
[[ -n "${_LEMONCROW_PYTHON_BOOTSTRAP_LOADED:-}" ]] && return 0
_LEMONCROW_PYTHON_BOOTSTRAP_LOADED=1

python3() {
    # type -P searches $PATH only, unlike `command -v`, which would match
    # this very function once it's defined and always report "found".
    local real
    real="$(type -P python3 2>/dev/null || true)"
    if [[ -n "$real" ]]; then
        "$real" "$@"
        return $?
    fi

    if ! command -v uv >/dev/null 2>&1; then
        echo "python3: command not found, and uv is not on PATH to install one" >&2
        return 127
    fi

    local ver="${LEMONCROW_PYTHON_VERSION:-3.13}"
    local bin
    bin="$(uv python find "$ver" 2>/dev/null || true)"
    if [[ -z "$bin" || ! -x "$bin" ]]; then
        uv python install "$ver" >/dev/null 2>&1 || true
        bin="$(uv python find "$ver" 2>/dev/null || true)"
    fi
    if [[ -z "$bin" || ! -x "$bin" ]]; then
        echo "python3: not found, and uv could not provide Python ${ver}" >&2
        return 127
    fi
    "$bin" "$@"
}
export -f python3
