"""Thin subprocess wrapper for ast-grep structural search and rewrite flows."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atelier.infra.code_intel.astgrep.binaries import discover_astgrep_binary
from atelier.infra.code_intel.astgrep.rewrite import (
    RewriteCandidate,
    RewriteOutcome,
    execute_rewrite,
)


class AstGrepToolUnavailable(RuntimeError):
    """Raised when ast-grep cannot be resolved safely."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(str(payload.get("message") or "ast-grep is unavailable"))
        self.payload = payload


@dataclass(frozen=True)
class PatternMatch:
    """Typed ast-grep structural match."""

    file_path: str
    line: int
    column: int
    end_line: int
    end_column: int
    snippet: str
    captures: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "snippet": self.snippet,
            "captures": self.captures,
        }


@dataclass(frozen=True)
class PatternSearchResult:
    """Typed ast-grep search result payload."""

    matches: list[PatternMatch]
    truncated: bool = False
    total_matches: int | None = None


@dataclass(frozen=True)
class PatternRewriteResult:
    """Typed ast-grep rewrite payload."""

    diff: str
    files_changed: list[str]


def _capture_text(raw_capture: Any) -> str | None:
    if isinstance(raw_capture, dict):
        text = raw_capture.get("text")
        return str(text) if text is not None else None
    return str(raw_capture) if raw_capture is not None else None


def _parse_captures(raw: dict[str, Any]) -> dict[str, str]:
    meta = raw.get("metaVariables")
    if not isinstance(meta, dict):
        return {}
    single = meta.get("single")
    if not isinstance(single, dict):
        return {}
    captures: dict[str, str] = {}
    for key, value in single.items():
        text = _capture_text(value)
        if text is not None:
            captures[str(key)] = text
    return captures


def _parse_range(raw: dict[str, Any]) -> tuple[int, int, int, int]:
    payload = raw.get("range")
    if not isinstance(payload, dict):
        return (0, 0, 0, 0)
    start = payload.get("start")
    end = payload.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return (0, 0, 0, 0)
    return (
        int(start.get("line", 0)),
        int(start.get("column", 0)),
        int(end.get("line", 0)),
        int(end.get("column", 0)),
    )


def _parse_json_output(stdout: str) -> dict[str, Any]:
    text = stdout.strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"matches": [json.loads(line) for line in text.splitlines() if line.strip()]}
    if isinstance(parsed, list):
        return {"matches": parsed}
    if isinstance(parsed, dict):
        return parsed
    return {}


class AstGrepAdapter:
    """Execute ast-grep with explicit binary handling and typed output parsing."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        binary_path: Path | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.binary_path = binary_path

    def _resolve_binary(self) -> Path:
        if self.binary_path is not None:
            return self.binary_path
        resolution = discover_astgrep_binary(self.repo_root, allow_bootstrap=True)
        if not resolution.available or resolution.path is None:
            raise AstGrepToolUnavailable(resolution.to_payload())
        self.binary_path = resolution.path
        return resolution.path

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        command = [str(self._resolve_binary()), *args]
        result = subprocess.run(
            command,
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "ast-grep command failed")
        return result

    def search(
        self,
        *,
        pattern: str,
        language: str | None = None,
        file_glob: str | None = None,
        limit: int = 20,
    ) -> PatternSearchResult:
        args = ["run", "--pattern", pattern, "--json"]
        if language:
            args.extend(["--lang", language])
        if file_glob:
            args.extend(["--globs", file_glob])
        result = self._run(args)
        payload = _parse_json_output(result.stdout)
        matches_payload = payload.get("matches", [])
        raw_matches = matches_payload if isinstance(matches_payload, list) else []
        matches: list[PatternMatch] = []
        for raw in raw_matches[:limit]:
            if not isinstance(raw, dict):
                continue
            line, column, end_line, end_column = _parse_range(raw)
            matches.append(
                PatternMatch(
                    file_path=str(raw.get("file") or raw.get("file_path") or ""),
                    line=line,
                    column=column,
                    end_line=end_line,
                    end_column=end_column,
                    snippet=str(raw.get("text") or raw.get("snippet") or ""),
                    captures=_parse_captures(raw),
                )
            )
        total_matches = payload.get("total_matches")
        return PatternSearchResult(
            matches=matches,
            truncated=bool(payload.get("truncated")) or len(raw_matches) > limit,
            total_matches=int(total_matches) if isinstance(total_matches, int) else None,
        )

    def rewrite(
        self,
        *,
        pattern: str,
        rewrite: str,
        language: str | None = None,
        file_glob: str | None = None,
        dry_run: bool = True,
    ) -> PatternRewriteResult:
        args = ["run", "--pattern", pattern, "--rewrite", rewrite, "--json"]
        if language:
            args.extend(["--lang", language])
        if file_glob:
            args.extend(["--globs", file_glob])
        result = self._run(args)
        payload = _parse_json_output(result.stdout)
        raw_rewrites = payload.get("rewrites", [])
        rewrites = raw_rewrites if isinstance(raw_rewrites, list) else []
        candidates = [
            RewriteCandidate(
                file_path=str(item.get("file") or item.get("file_path") or ""),
                before=str(item.get("before") or ""),
                after=str(item.get("after") or ""),
            )
            for item in rewrites
            if isinstance(item, dict)
        ]
        outcome: RewriteOutcome = execute_rewrite(self.repo_root, candidates, dry_run=dry_run)
        return PatternRewriteResult(diff=outcome.diff, files_changed=outcome.files_changed)


__all__ = [
    "AstGrepAdapter",
    "AstGrepToolUnavailable",
    "PatternMatch",
    "PatternRewriteResult",
    "PatternSearchResult",
]
