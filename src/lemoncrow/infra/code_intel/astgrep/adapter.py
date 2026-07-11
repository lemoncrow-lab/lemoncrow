"""Thin subprocess wrapper for ast-grep structural search and rewrite flows."""

from __future__ import annotations

import contextlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from lemoncrow.infra.code_intel.astgrep.binaries import discover_astgrep_binary
from lemoncrow.infra.code_intel.astgrep.rewrite import (
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


@dataclass(frozen=True)
class RuleMatch:
    """Typed ast-grep rule-mode match (scan / --rule output).

    Unlike :class:`PatternMatch`, a rule match carries the originating
    ``rule_id`` and ``severity`` because a single scan can evaluate many rules
    at once (relational/composite matchers such as ``inside``/``has``/``all``).
    """

    rule_id: str
    severity: str
    file_path: str
    line: int
    column: int
    end_line: int
    end_column: int
    snippet: str
    message: str
    captures: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "file_path": self.file_path,
            "line": self.line,
            "column": self.column,
            "end_line": self.end_line,
            "end_column": self.end_column,
            "snippet": self.snippet,
            "message": self.message,
            "captures": self.captures,
        }


@dataclass(frozen=True)
class RuleScanResult:
    """Typed ast-grep rule-scan payload."""

    matches: list[RuleMatch]
    truncated: bool = False
    total_matches: int | None = None


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
        # Tolerant fallback for newline-delimited (`--json=stream`) output. Guard
        # each line's parse: a single malformed line must not crash the parser,
        # so unparseable lines are skipped rather than propagating the error.
        matches: list[Any] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            with contextlib.suppress(json.JSONDecodeError):
                matches.append(json.loads(line))
        return {"matches": matches}
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
        timeout: float = 120.0,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.binary_path = binary_path
        # Bounded wall-clock per ast-grep invocation. A stalled child is not an
        # exception, so without this a full-repo scan can hang forever and the
        # caller's `except Exception` never fires.
        self.timeout = timeout

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
        try:
            result = subprocess.run(
                command,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # A hang is not an exception subprocess raises on its own; surface it
            # as a domain error so callers (whose guard is `except Exception`) see
            # a failure instead of blocking indefinitely.
            raise RuntimeError(f"ast-grep command timed out after {self.timeout:g}s") from exc
        stderr = result.stderr.strip()
        # ast-grep follows grep's exit-code convention: 0 = matches found,
        # 1 = no matches (NOT an error), >=2 = a real failure (bad lang/args).
        if result.returncode not in (0, 1):
            raise RuntimeError(stderr or "ast-grep command failed")
        # A malformed pattern parses to an ERROR node: ast-grep exits 0, emits no
        # matches, and only warns on stderr. Surface that instead of a silent empty
        # result so the caller knows the pattern was wrong, not that nothing matched.
        if "ERROR node" in stderr:
            raise RuntimeError(stderr)
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
        raw_matches = payload.get("matches", [])
        matches = raw_matches if isinstance(raw_matches, list) else []
        # ast-grep --json emits one object per match carrying `replacement` plus
        # byte `replacementOffsets`; reconstruct each file's post-rewrite content by
        # splicing replacements back-to-front so earlier edits don't shift offsets.
        edits_by_file: dict[str, list[tuple[int, int, str]]] = {}
        for raw in matches:
            if not isinstance(raw, dict):
                continue
            replacement = raw.get("replacement")
            if replacement is None:
                continue
            file_path = str(raw.get("file") or raw.get("file_path") or "")
            offsets = raw.get("replacementOffsets")
            if not isinstance(offsets, dict):
                byte_range = raw.get("range")
                offsets = byte_range.get("byteOffset") if isinstance(byte_range, dict) else None
            if not file_path or not isinstance(offsets, dict):
                continue
            start, end = offsets.get("start"), offsets.get("end")
            if not isinstance(start, int) or not isinstance(end, int):
                continue
            edits_by_file.setdefault(file_path, []).append((start, end, str(replacement)))
        candidates: list[RewriteCandidate] = []
        for file_path, edits in edits_by_file.items():
            target = (self.repo_root / file_path).resolve()
            try:
                original = target.read_bytes()
            except OSError:
                continue
            updated = original
            prev_start: int | None = None
            for start, end, replacement in sorted(edits, key=lambda edit: edit[0], reverse=True):
                if prev_start is not None and end > prev_start:
                    # Sorted back-to-front: an end past the previous edit's start means
                    # overlapping/nested matches that would corrupt the splice. Skip it.
                    continue
                updated = updated[:start] + replacement.encode("utf-8") + updated[end:]
                prev_start = start
            candidates.append(
                RewriteCandidate(
                    file_path=file_path,
                    before=original.decode("utf-8", errors="replace"),
                    after=updated.decode("utf-8", errors="replace"),
                )
            )
        outcome: RewriteOutcome = execute_rewrite(self.repo_root, candidates, dry_run=dry_run)
        return PatternRewriteResult(diff=outcome.diff, files_changed=outcome.files_changed)

    def scan(
        self,
        *,
        rules: list[dict[str, Any]] | str,
        paths: list[str] | None = None,
        no_ignore: bool = True,
        limit: int = 200,
    ) -> RuleScanResult:
        """Run ast-grep in rule mode (``scan --inline-rules``).

        ``rules`` is either a list of rule dicts (each a full ast-grep rule with
        at least ``id``/``language``/``rule`` keys, where ``rule`` may contain
        relational/composite matchers: ``inside``, ``has``, ``precedes``,
        ``follows``, ``all``, ``any``, ``not``, ``pattern``, ``kind``) or a
        pre-rendered YAML string (multiple rules separated by ``---``).

        This is additive: the legacy ``--pattern``/``--rewrite`` paths in
        :meth:`search`/:meth:`rewrite` are untouched.
        """
        inline = rules if isinstance(rules, str) else _render_rules_yaml(rules)
        # --json=compact emits a single JSON array; --json=stream would emit one
        # bare object per line, which _parse_json_output collapses to a single
        # dict (no `matches` key) when only one finding exists.
        args = ["scan", "--inline-rules", inline, "--json=compact"]
        if no_ignore:
            # Temp dirs, dotfiles, and worktrees are frequently gitignored;
            # without this a scan silently returns nothing on hidden/VCS paths.
            args.extend(["--no-ignore", "hidden"])
            # Only override VCS ignores when explicit paths are supplied (e.g. a
            # caller targeting a worktree or VCS-hidden file). A default
            # whole-repo scan must NOT walk .git: it is huge, irrelevant to
            # source rules, and a frequent source of stalls.
            if paths:
                args.extend(["--no-ignore", "vcs"])
        scan_paths = paths if paths else [str(self.repo_root)]
        args.extend(scan_paths)
        result = self._run(args)
        payload = _parse_json_output(result.stdout)
        matches_payload = payload.get("matches", [])
        raw_matches = matches_payload if isinstance(matches_payload, list) else []
        matches: list[RuleMatch] = []
        for raw in raw_matches[:limit]:
            if not isinstance(raw, dict):
                continue
            line, column, end_line, end_column = _parse_range(raw)
            matches.append(
                RuleMatch(
                    rule_id=str(raw.get("ruleId") or raw.get("rule_id") or ""),
                    severity=str(raw.get("severity") or "info"),
                    file_path=str(raw.get("file") or raw.get("file_path") or ""),
                    line=line,
                    column=column,
                    end_line=end_line,
                    end_column=end_column,
                    snippet=str(raw.get("text") or raw.get("snippet") or ""),
                    message=str(raw.get("message") or ""),
                    captures=_parse_captures(raw),
                )
            )
        return RuleScanResult(
            matches=matches,
            truncated=len(raw_matches) > limit,
            total_matches=len(raw_matches),
        )


def _render_rules_yaml(rules: list[dict[str, Any]]) -> str:
    """Serialize rule dicts into ast-grep's ``---``-separated inline YAML."""
    return "\n---\n".join(yaml.safe_dump(rule, sort_keys=False, default_flow_style=False) for rule in rules)


__all__ = [
    "AstGrepAdapter",
    "AstGrepToolUnavailable",
    "PatternMatch",
    "PatternRewriteResult",
    "PatternSearchResult",
    "RuleMatch",
    "RuleScanResult",
]
