"""ast-grep for ``codemod`` and ``scan``: find the binary, run it, apply its rewrites.

One engine for the thin client and the main package. Standard library only:
the binary comes from the ``LEMONCROW_AST_GREP_BIN`` override or ``PATH`` and
is never downloaded here -- the main package plugs its managed bootstrap in
through ``bootstrap`` / ``resolve_binary``. ast-grep walks files in parallel,
so its output order changes from run to run; every result here comes back in
path order, and a ``limit`` keeps the same matches on every run.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from itertools import groupby
from pathlib import Path
from typing import Any

from .fsio import atomic_write

ASTGREP_ENV_VAR = "LEMONCROW_AST_GREP_BIN"
ASTGREP_BINARY = "ast-grep"


@dataclass(frozen=True)
class AstGrepBinaryResolution:
    """Structured ast-grep availability status."""

    available: bool
    path: Path | None = None
    source: str | None = None
    checked: tuple[str, ...] = ()
    reason: str | None = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "error": "tool_unavailable",
            "tool": "ast-grep",
            "expected_binary": ASTGREP_BINARY,
            "message": self.reason or "ast-grep is unavailable",
            "checked": list(self.checked),
            "hint": f"Set {ASTGREP_ENV_VAR} to an executable ast-grep binary or allow the managed bootstrap path.",
        }


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _reject_reason(path: str) -> str | None:
    name = Path(path).name
    if name == "sg":
        return "resolved binary is the Linux `sg` group-switch utility, not ast-grep"
    return None


def _resolve_candidate(candidate: str) -> Path | None:
    expanded = Path(candidate).expanduser()
    if expanded.name == candidate:
        resolved = shutil.which(candidate)
        if not resolved:
            return None
        expanded = Path(resolved)
    try:
        return expanded.resolve()
    except OSError:
        return None


def discover_astgrep(
    repo_root: str | Path,
    *,
    env: Mapping[str, str] | None = None,
    bootstrap: Callable[[Path], AstGrepBinaryResolution] | None = None,
) -> AstGrepBinaryResolution:
    """Resolve ast-grep via env override, exact binary discovery, then optional bootstrap."""

    root = Path(repo_root).resolve()
    checked: list[str] = []

    env_candidate = (os.environ if env is None else env).get(ASTGREP_ENV_VAR)
    if env_candidate:
        checked.append(env_candidate)
        reason = _reject_reason(env_candidate)
        resolved = _resolve_candidate(env_candidate)
        if reason:
            return AstGrepBinaryResolution(available=False, checked=tuple(checked), reason=reason)
        if resolved is not None and _is_executable(resolved):
            return AstGrepBinaryResolution(available=True, path=resolved, source="env", checked=tuple(checked))

    exact_candidate = shutil.which(ASTGREP_BINARY)
    if exact_candidate:
        checked.append(exact_candidate)
        reason = _reject_reason(exact_candidate)
        resolved = _resolve_candidate(exact_candidate)
        if reason:
            return AstGrepBinaryResolution(available=False, checked=tuple(checked), reason=reason)
        if resolved is not None and _is_executable(resolved):
            return AstGrepBinaryResolution(
                available=True,
                path=resolved,
                source="system",
                checked=tuple(checked),
            )

    if bootstrap is not None:
        managed = bootstrap(root)
        if managed.available:
            return AstGrepBinaryResolution(
                available=True,
                path=managed.path,
                source=managed.source,
                checked=tuple([*checked, *managed.checked]),
            )
        checked.extend(managed.checked)
        return AstGrepBinaryResolution(available=False, checked=tuple(checked), reason=managed.reason)

    return AstGrepBinaryResolution(
        available=False,
        checked=tuple(checked),
        reason="ast-grep could not be resolved from env override or exact binary discovery",
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


def one_based(result: PatternSearchResult) -> PatternSearchResult:
    """ast-grep's 0-based lines and columns as the 1-based ones editors and ``read`` use."""
    return PatternSearchResult(
        matches=[
            replace(
                match,
                line=match.line + 1,
                column=match.column + 1,
                end_line=match.end_line + 1,
                end_column=match.end_column + 1,
            )
            for match in result.matches
        ],
        truncated=result.truncated,
        total_matches=result.total_matches,
    )


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


def _in_path_order(raw_matches: list[Any]) -> list[Any]:
    """ast-grep's matches ordered by file and position, not by which thread finished first."""

    def key(raw: Any) -> tuple[int, str, tuple[int, int, int, int], str, str]:
        if not isinstance(raw, dict):
            return (1, "", (0, 0, 0, 0), "", "")
        file_path = str(raw.get("file") or raw.get("file_path") or "")
        rule_id = str(raw.get("ruleId") or raw.get("rule_id") or "")
        return (0, file_path, _parse_range(raw), rule_id, str(raw.get("text") or ""))

    return sorted(raw_matches, key=key)


def render_rules(rules: Iterable[Mapping[str, Any]]) -> str:
    """Rule dicts as ast-grep's ``---``-separated inline rules; JSON is valid YAML."""
    return "\n---\n".join(json.dumps(rule, ensure_ascii=False) for rule in rules)


class AstGrepAdapter:
    """Execute ast-grep with explicit binary handling and typed output parsing."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        binary_path: Path | None = None,
        timeout: float = 120.0,
        resolve_binary: Callable[[Path], Path] | None = None,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.binary_path = binary_path
        # Bounded wall-clock per ast-grep invocation. A stalled child is not an
        # exception, so without this a full-repo scan can hang forever and the
        # caller's `except Exception` never fires.
        self.timeout = timeout
        #: Finds the binary when ``binary_path`` is not given; raises
        #: :class:`AstGrepToolUnavailable`. Defaults to :func:`discover_astgrep`.
        self.resolve_binary = resolve_binary

    def _resolve_binary(self) -> Path:
        if self.binary_path is not None:
            return self.binary_path
        if self.resolve_binary is not None:
            self.binary_path = self.resolve_binary(self.repo_root)
            return self.binary_path
        resolution = discover_astgrep(self.repo_root)
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
        raw_matches = _in_path_order(matches_payload if isinstance(matches_payload, list) else [])
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
            total_matches=int(total_matches) if isinstance(total_matches, int) else len(raw_matches),
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
        for file_path, edits in sorted(edits_by_file.items()):
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
        inline = rules if isinstance(rules, str) else render_rules(rules)
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
        raw_matches = _in_path_order(matches_payload if isinstance(matches_payload, list) else [])
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


@dataclass(frozen=True)
class RewriteCandidate:
    """A single file rewrite proposed by ast-grep."""

    file_path: str
    before: str
    after: str


@dataclass(frozen=True)
class RewriteOutcome:
    """Rendered rewrite result for preview or apply flows."""

    diff: str
    files_changed: list[str]


def execute_rewrite(
    repo_root: Path,
    candidates: list[RewriteCandidate],
    *,
    dry_run: bool,
) -> RewriteOutcome:
    """Render diffs and optionally apply rewrites to disk."""

    diff_parts: list[str] = []
    files_changed: list[str] = []
    for candidate in candidates:
        if candidate.before == candidate.after:
            continue
        diff_parts.extend(
            difflib.unified_diff(
                candidate.before.splitlines(keepends=True),
                candidate.after.splitlines(keepends=True),
                fromfile=f"a/{candidate.file_path}",
                tofile=f"b/{candidate.file_path}",
            )
        )
        if not dry_run:
            atomic_write((repo_root / candidate.file_path).resolve(), candidate.after)
        files_changed.append(candidate.file_path)
    return RewriteOutcome(diff="".join(diff_parts), files_changed=files_changed)


_DIFF_HEAD_LINES = 170
_DIFF_TAIL_LINES = 30


def bound_rewrite_diff(diff: str) -> str:
    """Head and tail of a long rewrite diff; a short one passes through untouched.

    A repo-wide rewrite would otherwise put an unbounded verbatim diff in the
    model's context. ``files_changed`` still lists every affected file.
    """
    diff_lines = (diff or "").splitlines(keepends=True)
    if len(diff_lines) <= _DIFF_HEAD_LINES + _DIFF_TAIL_LINES:
        return diff
    elided = len(diff_lines) - _DIFF_HEAD_LINES - _DIFF_TAIL_LINES
    return (
        "".join(diff_lines[:_DIFF_HEAD_LINES])
        + f"... ({elided} more diff lines elided; see files_changed)\n"
        + "".join(diff_lines[-_DIFF_TAIL_LINES:])
    )


def group_rows_by_file(rows: Iterable[tuple[str, int, str]]) -> list[str]:
    """Render ``(path, line, label)`` rows with each file path emitted once.

    Emitting the path once per file (a header line, then indented per-hit
    lines) instead of repeating the full path on every hit is the dominant
    token win for clustered usages/callers/callees/search/pattern results.
    """
    ordered = sorted(rows, key=lambda row: (row[0], row[1], row[2]))
    out: list[str] = []
    for file_path, group in groupby(ordered, key=lambda row: row[0]):
        out.append(f"- {file_path}")
        for _path, line, label in group:
            loc = str(line) if line > 0 else ""
            if loc and label:
                out.append(f"  - {loc} — {label}")
            elif loc:
                out.append(f"  - {loc}")
            elif label:
                out.append(f"  - {label}")
            else:
                out.append("  - ?")
    return out


def render_pattern_text(payload: Mapping[str, Any]) -> str | None:
    """A pattern search's matches, one line per hit under its file."""
    matches = payload.get("matches")
    # Rewrite responses carry a diff/files_changed, not matches -- leave those as
    # JSON so the agent still receives the structured diff. Only search responses
    # (matches is a list, possibly empty) get the compact markdown treatment.
    if not isinstance(matches, list):
        return None
    rows: list[tuple[str, int, str]] = []
    for match in matches:
        if not isinstance(match, Mapping):
            continue
        file_path = str(match.get("path") or match.get("file_path") or "?")
        line = int(match.get("line") or match.get("start_line") or 0)
        snippet = " ".join(str(match.get("snippet") or "").split())[:120]
        rows.append((file_path, line, snippet))
    if not rows:
        return "- no matches"
    lines: list[str] = []
    lines.extend(group_rows_by_file(rows))
    if payload.get("truncated"):
        total = payload.get("total_matches")
        lines.append(f"- truncated (total_matches={total})" if total is not None else "- truncated")
    return "\n".join(lines)


__all__ = [
    "ASTGREP_BINARY",
    "ASTGREP_ENV_VAR",
    "AstGrepAdapter",
    "AstGrepBinaryResolution",
    "AstGrepToolUnavailable",
    "PatternMatch",
    "PatternRewriteResult",
    "PatternSearchResult",
    "RewriteCandidate",
    "RewriteOutcome",
    "RuleMatch",
    "RuleScanResult",
    "bound_rewrite_diff",
    "discover_astgrep",
    "execute_rewrite",
    "group_rows_by_file",
    "one_based",
    "render_pattern_text",
    "render_rules",
]
