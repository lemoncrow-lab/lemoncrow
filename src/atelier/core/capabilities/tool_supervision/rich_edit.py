"""Atelier-native rich batch edit capability."""

from __future__ import annotations

import contextlib
import json
import logging
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .fuzzy_match import apply_fuzzy_replace, normalize_for_fuzzy

logger = logging.getLogger(__name__)

_PROTECTED_PARTS = {".git", ".atelier", "node_modules", ".venv"}
_SMART_QUOTES = str.maketrans(
    {"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'", "\u2013": "-", "\u2014": "-"}
)


@dataclass(frozen=True)
class TargetSpec:
    path: str
    start_line: int | None = None
    end_line: int | None = None
    cell: str | int | None = None


def _repo_root(repo_root: str | Path | None = None) -> Path:
    return Path(repo_root or Path.cwd()).resolve()


def _parse_target(raw_path: str) -> TargetSpec:
    if "#cell=" in raw_path:
        path, cell = raw_path.split("#cell=", 1)
        return TargetSpec(path=path, cell=cell)
    match = re.search(r"#(\d+)(?:-(\d+))?$", raw_path)
    if match:
        return TargetSpec(
            path=raw_path[: match.start()],
            start_line=int(match.group(1)),
            end_line=int(match.group(2) or match.group(1)),
        )
    return TargetSpec(path=raw_path)


def _resolve(root: Path, raw_path: str) -> Path:
    spec = _parse_target(raw_path)
    path = Path(spec.path)
    resolved = path if path.is_absolute() else root / path
    resolved = resolved.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escape denied: {raw_path}") from exc
    if any(part in _PROTECTED_PARTS for part in resolved.parts):
        raise ValueError(f"protected path denied: {raw_path}")
    return resolved


def _normalize_typography(text: str) -> str:
    return text.translate(_SMART_QUOTES)


def _placeholder_pattern(old_string: str) -> re.Pattern[str] | None:
    if "..." not in old_string and "<...>" not in old_string:
        return None
    escaped = re.escape(old_string)
    escaped = escaped.replace(re.escape("<...>"), r"[\s\S]{0,4000}").replace(re.escape("..."), r"[\s\S]{0,2000}")
    return re.compile(escaped)


def _leading_whitespace(line: str) -> str:
    match = re.match(r"\s*", line)
    return match.group(0) if match else ""


def _adapt_indentation(old: str, new: str, matched: str) -> str:
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    matched_lines = matched.splitlines()
    if len(new_lines) <= 1 or not old_lines or not matched_lines:
        return new
    base_indent_text = _leading_whitespace(matched_lines[0]) if matched_lines else ""
    if not base_indent_text and len(old_lines) > 1:
        base_indent_text = _leading_whitespace(old_lines[1])
    if base_indent_text:
        result: list[str] = [new_lines[0]]
        consecutive_blanks = 0
        for line in new_lines[1:]:
            if not line.strip():
                consecutive_blanks += 1
                result.append(line)
            elif not line.startswith((" ", "\t")) and consecutive_blanks < 2:
                result.append(base_indent_text + line)
                consecutive_blanks = 0
            else:
                result.append(line)
                consecutive_blanks = 0
        new_lines = result
    old_indent = len(old_lines[0]) - len(old_lines[0].lstrip())
    matched_indent = len(matched_lines[0]) - len(matched_lines[0].lstrip())
    delta = matched_indent - old_indent
    if delta <= 0:
        return "\n".join(new_lines)
    prefix = " " * delta
    return "\n".join((prefix + line if line.strip() else line) for line in new_lines)


def _replace_in_scope(content: str, spec: TargetSpec, old_string: str, new_string: str) -> tuple[str, int, int]:
    lines = content.splitlines(keepends=True)
    start_offset = 0
    end_offset = len(content)
    if spec.start_line is not None:
        start = max(1, spec.start_line)
        end = min(len(lines), spec.end_line or start)
        start_offset = sum(len(line) for line in lines[: start - 1])
        end_offset = sum(len(line) for line in lines[:end])
    scoped = content[start_offset:end_offset]

    index = scoped.find(old_string)
    matched = old_string
    if index == -1:
        normalized_content = _normalize_typography(scoped)
        normalized_old = _normalize_typography(old_string)
        normalized_index = normalized_content.find(normalized_old)
        if normalized_index != -1:
            index = normalized_index
            matched = scoped[index : index + len(old_string)]
    if index == -1:
        placeholder = _placeholder_pattern(old_string)
        if placeholder:
            match = placeholder.search(scoped)
            if match:
                index = match.start()
                matched = match.group(0)
    if index != -1:
        replacement = _adapt_indentation(old_string, new_string, matched)
        absolute = start_offset + index
        line_start = content[:absolute].count("\n") + 1
        line_end = line_start + matched.count("\n")
        return (
            content[:absolute] + replacement + content[absolute + len(matched) :],
            line_start,
            line_end,
        )

    if normalize_for_fuzzy(old_string):
        fuzzed, line_start, line_end = apply_fuzzy_replace(scoped, old_string, new_string)
        return (
            content[:start_offset] + fuzzed + content[end_offset:],
            line_start + content[:start_offset].count("\n"),
            line_end + content[:start_offset].count("\n"),
        )
    raise ValueError("old_string not found in file")


def _cell_index(cells: list[dict[str, Any]], target: str | int | None) -> int:
    if target is None:
        raise ValueError("notebook cell target is required")
    if target == "last":
        return len(cells) - 1
    index = int(target)
    if index < 0 or index >= len(cells):
        raise ValueError("notebook cell target out of range")
    return index


def _cell_source(cell: dict[str, Any]) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


def _set_cell_source(cell: dict[str, Any], source: str) -> None:
    cell["source"] = source
    if cell.get("cell_type") == "code":
        cell["outputs"] = []
        cell["execution_count"] = None


def _apply_notebook_edit(notebook: dict[str, Any], spec: TargetSpec, edit: dict[str, Any]) -> None:
    cells = notebook.setdefault("cells", [])
    if not isinstance(cells, list):
        raise ValueError("notebook cells must be a list")
    action = edit.get("cell_action")
    if action in {"insert_after", "insert_before"}:
        index = _cell_index(cells, spec.cell)
        new_cell = {
            "cell_type": edit.get("cell_type", "code"),
            "metadata": {},
            "source": edit.get("new_string", ""),
        }
        if new_cell["cell_type"] == "code":
            new_cell.update({"outputs": [], "execution_count": None})
        cells.insert(index + (1 if action == "insert_after" else 0), new_cell)
        return
    if action == "delete":
        del cells[_cell_index(cells, spec.cell)]
        return
    if action in {"move_after", "move_before"}:
        index = _cell_index(cells, spec.cell)
        target = _cell_index(cells, edit.get("cell_move_target"))
        cell = cells.pop(index)
        if index < target:
            target -= 1
        cells.insert(target + (1 if action == "move_after" else 0), cell)
        return
    if edit.get("overwrite") and spec.cell is not None:
        index = _cell_index(cells, spec.cell)
        try:
            replacement = json.loads(str(edit.get("new_string", "")))
            if isinstance(replacement, dict) and "cell_type" in replacement:
                cells[index] = replacement
                return
        except Exception:
            logger.warning(
                "Suppressed exception at rich_edit.py:215",
                exc_info=True,
            )
        _set_cell_source(cells[index], str(edit.get("new_string", "")))
        return
    matches = [cell for cell in cells if str(edit.get("old_string", "")) in _cell_source(cell)]
    if len(matches) != 1:
        raise ValueError("old_string must match exactly one notebook cell")
    cell = matches[0]
    _set_cell_source(
        cell,
        _cell_source(cell).replace(str(edit.get("old_string", "")), str(edit.get("new_string", "")), 1),
    )


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        handle.write(text)
        tmp = Path(handle.name)
    tmp.replace(path)


def apply_rich_edits(
    edits: list[dict[str, Any]], *, repo_root: str | Path | None = None, atomic: bool = True
) -> dict[str, Any]:
    """Apply rich Atelier edits in memory, writing each touched file once."""
    root = _repo_root(repo_root)
    backups: dict[Path, bytes | None] = {}
    file_state: dict[Path, str] = {}
    applied: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    try:
        for edit in edits:
            raw_path = str(edit.get("file_path") or edit.get("path") or "")
            if not raw_path:
                raise ValueError("file_path is required")
            spec = _parse_target(raw_path)
            path = _resolve(root, raw_path)
            if path not in backups:
                backups[path] = path.read_bytes() if path.exists() else None
            content = file_state.get(path)
            if content is None:
                content = path.read_text(encoding="utf-8") if path.exists() else ""

            if path.suffix.lower() == ".ipynb":
                notebook = json.loads(content or '{"cells": [], "metadata": {}, "nbformat": 4, "nbformat_minor": 5}')
                _apply_notebook_edit(notebook, spec, edit)
                file_state[path] = json.dumps(notebook, indent=2)
                applied.append({"path": raw_path, "kind": "notebook"})
                continue

            if edit.get("overwrite") or (not path.exists() and not edit.get("old_string")):
                file_state[path] = str(edit.get("new_string", ""))
                applied.append({"path": raw_path, "kind": "overwrite"})
                continue

            old_string = str(edit.get("old_string", ""))
            if not old_string:
                raise ValueError("old_string is required unless overwrite=true or creating a new file")
            new_content, line_start, line_end = _replace_in_scope(
                content, spec, old_string, str(edit.get("new_string", ""))
            )
            file_state[path] = new_content
            applied.append({"path": raw_path, "hunks": [{"line_start": line_start, "line_end": line_end}]})

        for path, content in file_state.items():
            _atomic_write(path, content)
        return {"applied": applied, "failed": [], "rolled_back": False, "writes": len(file_state)}
    except Exception as exc:
        failed.append({"error": str(exc)})
        if atomic:
            for path, payload in backups.items():
                if payload is None:
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(payload)
            return {"applied": [], "failed": failed, "rolled_back": True}
        for path, content in file_state.items():
            with contextlib.suppress(Exception):
                _atomic_write(path, content)
        return {
            "applied": applied,
            "failed": failed,
            "rolled_back": False,
            "writes": len(file_state),
        }


__all__ = ["apply_rich_edits"]
