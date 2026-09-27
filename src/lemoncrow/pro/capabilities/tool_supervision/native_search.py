"""The main package's grep: the kit's search engine plus what only it has.

:mod:`lemoncrow_client.kit.search` is the engine. This module adds the ripgrep
(or system grep) fast path, the third-party ``regex`` engine's per-call
timeout, PDF text through ``pypdf``, the read-cost baseline behind
``tokens_saved`` and the JSON spill store.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from lemoncrow_client.kit.search import SKIP_DIRS, SearchHooks
from lemoncrow_client.kit.search import search_workspace as _kit_search_workspace

from lemoncrow.core.capabilities.native_read_baseline import claude_read_baseline_text

try:  # Third-party engine supporting a per-call wall-clock `timeout=` on search.
    import regex as _regex_module
except ImportError:  # pragma: no cover - fallback path when `regex` is absent.
    _regex_module = None

# Fast grep backends — ripgrep is strongly preferred; system grep is the
# fallback. The Python directory-walk path is not used for content_regex
# searches on any Mac/Linux system where one of these is always available.
_RG_BIN: str | None = shutil.which("rg")
_GREP_BIN: str | None = shutil.which("grep")


def _append_rg_skip_globs(cmd: list[str]) -> None:
    """Keep ripgrep's fast path aligned with the Python walker exclusions."""
    for directory in sorted(SKIP_DIRS):
        cmd.extend(["--glob", f"!**/{directory}/**"])


def _spill_dir() -> Path:
    configured = os.environ.get("LEMONCROW_MCP_SPILL_DIR")
    if configured:
        path = Path(configured).expanduser().resolve()
    else:
        path = Path(tempfile.gettempdir()) / "lemoncrow-spill"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _spill_response_payload(payload: dict[str, Any]) -> Path:
    spill_path = _spill_dir() / f"search-{int(time.time() * 1000)}.json"
    spill_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return spill_path


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception:
        logging.exception("Recovered from broad exception handler")
        return f"[PDF text extraction unavailable: install pypdf to read {path.name}]"
    try:
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        logging.exception("Recovered from broad exception handler")
        return f"[PDF text extraction failed: {exc}]"


def _rg_candidate_files(
    *,
    pattern: str,
    base: Path,
    glob_patterns: list[str],
    ignore_case: bool,
    timeout: float,
) -> list[Path] | None:
    """Return files matching *pattern* via ``rg -l``. None = error / timeout."""
    assert _RG_BIN is not None
    cmd: list[str] = [_RG_BIN, "--files-with-matches", "--no-messages", "--hidden"]
    if ignore_case:
        cmd.append("-i")
    for g in glob_patterns:
        cmd.extend(["--glob", g])
    _append_rg_skip_globs(cmd)
    cmd.extend(["--", pattern, str(base)])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode not in (0, 1):  # 0=found, 1=no match, other=error
        return None
    return [Path(line) for line in proc.stdout.splitlines() if line]


def _grep_candidate_files(
    *,
    pattern: str,
    base: Path,
    glob_patterns: list[str],
    ignore_case: bool,
    timeout: float,
) -> list[Path] | None:
    """Return files matching *pattern* via ``grep -rl``. None = error / timeout."""
    assert _GREP_BIN is not None
    cmd: list[str] = [_GREP_BIN, "-rl", "-H"]
    if ignore_case:
        cmd.append("-i")
    for g in glob_patterns:
        # grep --include matches basenames only; a path-qualified glob like
        # "src/**/*.py" would otherwise never match anything.
        cmd.extend(["--include", Path(g).name])
    cmd.extend(["--", pattern, str(base)])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode not in (0, 1):
        return None
    return [Path(line) for line in proc.stdout.splitlines() if line]


def _rg_line_numbers(
    *,
    pattern: str,
    base: Path,
    glob_patterns: list[str],
    ignore_case: bool,
    timeout: float,
) -> dict[str, list[int]] | None:
    """Return {abs_path: [1-based line nos]} via ``rg -n``. None = error / timeout."""
    assert _RG_BIN is not None
    cmd: list[str] = [
        _RG_BIN,
        "--line-number",
        "--no-heading",
        "--with-filename",
        "--no-messages",
        "--hidden",
        # NUL after the filename so a path containing ':' is parsed unambiguously
        # (plain "path:lineno:content" splitting drops such files silently).
        "--null",
    ]
    if ignore_case:
        cmd.append("-i")
    for g in glob_patterns:
        cmd.extend(["--glob", g])
    _append_rg_skip_globs(cmd)
    cmd.extend(["--", pattern, str(base)])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode not in (0, 1):
        return None
    result: dict[str, list[int]] = {}
    for line in proc.stdout.splitlines():
        # format: /abs/path\0lineno:content  (NUL isolates the path, which may
        # itself contain ':'); the remainder is "lineno:content".
        path, sep, rest = line.partition("\0")
        if not sep:
            continue
        lineno_str = rest.partition(":")[0]
        try:
            lineno = int(lineno_str)
        except ValueError:
            continue
        result.setdefault(path, []).append(lineno)
    return result


def _grep_line_numbers(
    *,
    pattern: str,
    base: Path,
    glob_patterns: list[str],
    ignore_case: bool,
    timeout: float,
) -> dict[str, list[int]] | None:
    """Return {abs_path: [1-based line nos]} via ``grep -rn``. None = error / timeout."""
    assert _GREP_BIN is not None
    # -Z: NUL after the filename so a path containing ':' is parsed unambiguously.
    cmd: list[str] = [_GREP_BIN, "-rn", "-H", "-Z"]
    if ignore_case:
        cmd.append("-i")
    for g in glob_patterns:
        # grep --include matches basenames only; a path-qualified glob like
        # "src/**/*.py" would otherwise never match anything.
        cmd.extend(["--include", Path(g).name])
    cmd.extend(["--", pattern, str(base)])
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode not in (0, 1):
        return None
    result: dict[str, list[int]] = {}
    for line in proc.stdout.splitlines():
        # format: path\0lineno:content  (NUL isolates a path that may contain ':')
        path, sep, rest = line.partition("\0")
        if not sep:
            continue
        lineno_str = rest.partition(":")[0]
        try:
            lineno = int(lineno_str)
        except ValueError:
            continue
        result.setdefault(path, []).append(lineno)
    return result


def _fast_path() -> tuple[Callable[..., list[Path] | None] | None, Callable[..., dict[str, list[int]] | None] | None]:
    if _RG_BIN is not None:
        return _rg_candidate_files, _rg_line_numbers
    if _GREP_BIN is not None:
        return _grep_candidate_files, _grep_line_numbers
    return None, None


def search_workspace(**kwargs: Any) -> dict[str, Any]:
    """:func:`lemoncrow_client.kit.search.search_workspace` with this package's hooks."""
    fast_files, fast_lines = _fast_path()
    hooks = SearchHooks(
        fast_files=fast_files,
        fast_lines=fast_lines,
        regex_module=_regex_module,
        pdf_text=_extract_pdf,
        read_baseline=claude_read_baseline_text,
        spill=_spill_response_payload,
    )
    return _kit_search_workspace(hooks=hooks, **kwargs)


__all__ = ["search_workspace"]
