"""Symbol tag extraction used by the PageRank repo map."""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from atelier.infra.code_intel.languages import language_for_path

TagKind = Literal["definition", "reference"]


@dataclass(frozen=True)
class Tag:
    name: str
    kind: TagKind
    file: str
    line: int
    byte_range: tuple[int, int]


def _line_offsets(text: str) -> list[int]:
    offsets = [0]
    total = 0
    for line in text.splitlines(keepends=True):
        total += len(line.encode("utf-8"))
        offsets.append(total)
    return offsets


def _python_tags(path: Path, text: str) -> list[Tag]:
    offsets = _line_offsets(text)
    tags: list[Tag] = []
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            line = int(getattr(node, "lineno", 1))
            tags.append(Tag(node.name, "definition", str(path), line, (offsets[line - 1], offsets[line])))
        elif isinstance(node, ast.Name):
            line = int(getattr(node, "lineno", 1))
            tags.append(Tag(node.id, "reference", str(path), line, (offsets[line - 1], offsets[line])))
    return tags


def _regex_tags(path: Path, text: str, language: str) -> list[Tag]:
    patterns = {
        "javascript": r"(?:function|class|const|let|var)\s+([A-Za-z_$][\w$]*)",
        "typescript": r"(?:function|class|interface|type|const|let|var)\s+([A-Za-z_$][\w$]*)",
        "go": r"(?:func|type|var|const)\s+(?:\([^)]*\)\s*)?([A-Za-z_][\w]*)",
        "rust": r"(?:fn|struct|enum|trait|impl)\s+([A-Za-z_][\w]*)",
    }
    def_re = re.compile(patterns.get(language, patterns["javascript"]))
    ident_re = re.compile(r"[A-Za-z_][$\w]*")
    tags: list[Tag] = []
    byte_offset = 0
    for line_no, line in enumerate(text.splitlines(keepends=True), start=1):
        for match in def_re.finditer(line):
            tags.append(
                Tag(
                    match.group(1),
                    "definition",
                    str(path),
                    line_no,
                    (byte_offset + match.start(1), byte_offset + match.end(1)),
                )
            )
        for match in ident_re.finditer(line):
            tags.append(
                Tag(
                    match.group(0),
                    "reference",
                    str(path),
                    line_no,
                    (byte_offset + match.start(0), byte_offset + match.end(0)),
                )
            )
        byte_offset += len(line.encode("utf-8"))
    return tags


def detect_language(path: Path) -> str | None:
    # Delegate to the canonical registry (DLS-LANG-04). Preserves the
    # str | None contract: extract_tags_from_text short-circuits to [] on None.
    # Widening to more languages is safe — _regex_tags falls back to the
    # javascript pattern for any language without a dedicated regex.
    lang = language_for_path(path)
    return lang.name if lang is not None else None


def extract_tags_from_text(text: str, file_path: str | Path, language: str | None = None) -> list[Tag]:
    """Extract definition/reference tags from source text without reading from disk."""

    path = Path(file_path)
    resolved_language = language or detect_language(path)
    if resolved_language is None:
        return []
    if resolved_language == "python":
        try:
            return _python_tags(path, text)
        except SyntaxError:
            return []
    return _regex_tags(path, text, resolved_language)


def extract_tags(file_path: str | Path, language: str | None = None) -> list[Tag]:
    """Extract definition/reference tags from a supported source file."""

    path = Path(file_path)
    text = path.read_text(encoding="utf-8", errors="replace")
    return extract_tags_from_text(text, path, language=language)


__all__ = ["Tag", "detect_language", "extract_tags", "extract_tags_from_text"]
