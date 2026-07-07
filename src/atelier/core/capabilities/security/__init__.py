"""Security scanning capability (G11).

A bounded, honest first-iteration SAST surface built on the ast-grep rule
engine (G12) plus a small intra-procedural Python taint check. This is NOT a
full SAST engine: coverage is a handful of high-signal OWASP/CWE patterns and
one-language (Python) source->sink taint. Every finding is tagged with a rule
id, severity, and confidence, and heuristic findings are marked as such.
"""

from atelier.core.capabilities.security.rules import (
    BUNDLED_RULES,
    SecurityRule,
    bundled_astgrep_rules,
)
from atelier.core.capabilities.security.scanner import (
    Finding,
    SecurityScanner,
    scan_repository,
)
from atelier.core.capabilities.security.taint import (
    TaintFinding,
    analyze_python_source,
)

__all__ = [
    "BUNDLED_RULES",
    "Finding",
    "SecurityRule",
    "SecurityScanner",
    "TaintFinding",
    "analyze_python_source",
    "bundled_astgrep_rules",
    "scan_repository",
]
