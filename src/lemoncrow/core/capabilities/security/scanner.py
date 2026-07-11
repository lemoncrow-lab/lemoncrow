"""Security scan orchestrator (G11).

Merges two evidence sources into one unified finding list:

1. ast-grep rule-pack matches (G12 rule engine) -- the bundled OWASP/CWE rules.
2. A bounded intra-procedural Python taint check (source->sink).

The scanner is additive and fail-open: an unavailable ast-grep binary or an
unreadable file degrades the scan rather than raising. Findings carry a stable
rule id, severity, confidence, and a ``heuristic`` flag. This is a first
iteration with bounded coverage -- it never claims exhaustiveness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.security.rules import (
    BUNDLED_RULES,
    SecurityRule,
    rule_by_id,
)
from lemoncrow.core.capabilities.security.taint import TaintFinding, analyze_python_source
from lemoncrow.infra.code_intel.astgrep.adapter import (
    AstGrepAdapter,
    AstGrepToolUnavailable,
    RuleMatch,
)

logger = logging.getLogger(__name__)

_PYTHON_SUFFIXES: frozenset[str] = frozenset({".py", ".pyi"})

# Process-level taint cache: (abs_path_str, mtime_ns, size) -> findings.
# A stat() call (~1µs) replaces a full ast.parse + taint walk (~5-12ms) per
# unchanged file. Invalidated automatically when mtime_ns or size changes.
_taint_cache: dict[tuple[str, int, int], list[TaintFinding]] = {}

_SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".lemoncrow"}
)


@dataclass(frozen=True)
class Finding:
    """A unified security finding from either the rule pack or the taint check."""

    rule_id: str
    cwe: str
    severity: str
    confidence: str
    file_path: str
    line: int
    message: str
    source: str  # "rule" | "taint"
    heuristic: bool
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "cwe": self.cwe,
            "severity": self.severity,
            "confidence": self.confidence,
            "path": self.file_path,
            "line": self.line,
            "message": self.message,
            "source": self.source,
            "heuristic": self.heuristic,
            "snippet": self.snippet,
        }


_SEVERITY_ORDER: dict[str, int] = {"error": 0, "warning": 1, "info": 2}


class SecurityScanner:
    """Scan a repository for the bundled rule pack plus bounded Python taint."""

    def __init__(
        self,
        repo_root: str | Path,
        *,
        rules: tuple[SecurityRule, ...] = BUNDLED_RULES,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.rules = rules

    def _relpath(self, raw: str) -> str:
        if not raw:
            return raw
        candidate = Path(raw)
        if candidate.is_absolute():
            try:
                return str(candidate.resolve().relative_to(self.repo_root))
            except ValueError:
                return str(candidate)
        return raw

    def _scan_rules(self, paths: list[str] | None) -> list[Finding]:
        adapter = AstGrepAdapter(self.repo_root)
        astgrep_rules = [rule.to_astgrep_rule() for rule in self.rules]
        try:
            result = adapter.scan(rules=astgrep_rules, paths=paths)
        except AstGrepToolUnavailable:
            # Fail open: the taint check still runs even without ast-grep.
            logger.warning("ast-grep unavailable; skipping rule-pack scan")
            return []
        except Exception:
            logging.exception("Recovered from broad exception handler")
            return []
        findings: list[Finding] = []
        for match in result.matches:
            findings.append(self._finding_from_rule_match(match))
        return findings

    def _finding_from_rule_match(self, match: RuleMatch) -> Finding:
        meta = rule_by_id(match.rule_id)
        confidence = meta.confidence if meta is not None else "medium"
        cwe = meta.cwe if meta is not None else ""
        heuristic = meta.heuristic if meta is not None else False
        return Finding(
            rule_id=match.rule_id,
            cwe=cwe,
            severity=match.severity or (meta.severity if meta is not None else "info"),
            confidence=confidence,
            file_path=self._relpath(match.file_path),
            # ast-grep reports 0-based lines; surface 1-based to match editors.
            line=match.line + 1,
            message=match.message or (meta.message if meta is not None else ""),
            source="rule",
            heuristic=heuristic,
            snippet=match.snippet,
        )

    def _iter_python_files(self, paths: list[str] | None) -> list[Path]:
        roots: list[Path]
        if paths:
            roots = []
            for raw in paths:
                candidate = Path(raw)
                roots.append(candidate if candidate.is_absolute() else self.repo_root / candidate)
        else:
            roots = [self.repo_root]
        files: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            if root.is_file():
                if root.suffix in _PYTHON_SUFFIXES and root not in seen:
                    seen.add(root)
                    files.append(root)
                continue
            if not root.is_dir():
                continue
            for path in root.rglob("*"):
                if path.suffix not in _PYTHON_SUFFIXES:
                    continue
                if any(part in _SKIP_DIR_NAMES for part in path.parts):
                    continue
                if path in seen:
                    continue
                seen.add(path)
                files.append(path)
        return files

    def _scan_taint(self, paths: list[str] | None) -> list[Finding]:
        findings: list[Finding] = []
        for path in self._iter_python_files(paths):
            # Cache check: stat() is ~1µs; skip re-parse + re-walk if unchanged.
            try:
                st = path.stat()
                cache_key: tuple[str, int, int] | None = (str(path.resolve()), st.st_mtime_ns, st.st_size)
            except OSError:
                cache_key = None
            if cache_key is not None and cache_key in _taint_cache:
                taint_results = _taint_cache[cache_key]
            else:
                try:
                    source = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                except Exception:
                    logging.exception("Recovered from broad exception handler")
                    continue
                rel_for_analyze = self._relpath(str(path))
                taint_results = analyze_python_source(source, file_path=rel_for_analyze)
                if cache_key is not None:
                    _taint_cache[cache_key] = taint_results
            rel = self._relpath(str(path))
            for taint in taint_results:
                findings.append(
                    Finding(
                        rule_id=taint.rule_id,
                        cwe=taint.cwe,
                        severity=taint.severity,
                        confidence=taint.confidence,
                        file_path=rel,
                        line=taint.line,
                        message=taint.message,
                        source="taint",
                        heuristic=taint.heuristic,
                    )
                )
        return findings

    def scan(
        self,
        *,
        paths: list[str] | None = None,
        include_taint: bool = True,
        include_rules: bool = True,
    ) -> list[Finding]:
        """Return the merged, de-duplicated, severity-sorted finding list."""
        findings: list[Finding] = []
        if include_rules:
            findings.extend(self._scan_rules(paths))
        if include_taint:
            findings.extend(self._scan_taint(paths))
        return _dedupe_and_sort(findings)


def _dedupe_and_sort(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str, int]] = set()
    unique: list[Finding] = []
    for finding in findings:
        key = (finding.rule_id, finding.file_path, finding.line)
        if key in seen:
            continue
        seen.add(key)
        unique.append(finding)
    unique.sort(
        key=lambda f: (
            _SEVERITY_ORDER.get(f.severity, 99),
            f.file_path,
            f.line,
            f.rule_id,
        )
    )
    return unique


def scan_repository(
    repo_root: str | Path,
    *,
    paths: list[str] | None = None,
    include_taint: bool = True,
    include_rules: bool = True,
) -> list[dict[str, Any]]:
    """Convenience wrapper returning serialized findings for the MCP surface."""
    scanner = SecurityScanner(repo_root)
    return [
        finding.to_dict()
        for finding in scanner.scan(
            paths=paths,
            include_taint=include_taint,
            include_rules=include_rules,
        )
    ]


__all__ = [
    "Finding",
    "SecurityScanner",
    "scan_repository",
]
