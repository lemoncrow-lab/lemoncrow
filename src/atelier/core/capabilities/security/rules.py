"""Bundled high-signal OWASP/CWE rule pack for the G11 security scanner.

Each :class:`SecurityRule` couples a small ast-grep rule body (executed via the
G12 rule engine) with security metadata: a stable ``rule_id``, ``cwe`` tag,
``severity``, a human ``message``, and a ``confidence`` band. The set is
deliberately small and conservative -- it favours precision (few false
positives) over recall. This is a first iteration, NOT exhaustive coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Severity = Literal["error", "warning", "info"]
Confidence = Literal["high", "medium", "low"]


@dataclass(frozen=True)
class SecurityRule:
    """A bundled security rule plus the ast-grep matcher that detects it."""

    rule_id: str
    cwe: str
    severity: Severity
    confidence: Confidence
    message: str
    language: str
    matcher: dict[str, Any]
    heuristic: bool = False
    references: tuple[str, ...] = field(default_factory=tuple)

    def to_astgrep_rule(self) -> dict[str, Any]:
        """Render the full ast-grep rule dict consumed by ``AstGrepAdapter.scan``."""
        return {
            "id": self.rule_id,
            "language": self.language,
            "severity": self.severity,
            "message": self.message,
            "rule": self.matcher,
        }

    def metadata(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "cwe": self.cwe,
            "severity": self.severity,
            "confidence": self.confidence,
            "message": self.message,
            "heuristic": self.heuristic,
            "references": list(self.references),
        }


# CWE-95 / OWASP A03 Injection: eval/exec of a (potentially dynamic) value.
_EVAL_EXEC = SecurityRule(
    rule_id="py-eval-exec",
    cwe="CWE-95",
    severity="error",
    confidence="medium",
    message=(
        "Dynamic code execution via eval()/exec(). If the argument is derived "
        "from untrusted input this is a code-injection sink."
    ),
    language="python",
    matcher={
        "any": [
            {"pattern": "eval($X)"},
            {"pattern": "exec($X)"},
        ]
    },
    references=("https://cwe.mitre.org/data/definitions/95.html",),
)

# CWE-78 / OWASP A03 Injection: subprocess with shell=True AND string building
# (concatenation or f-string), which is the classic OS-command-injection shape.
# Requiring interpolation keeps the rule precise: a static literal command with
# shell=True is not flagged.
_SUBPROCESS_SHELL = SecurityRule(
    rule_id="py-subprocess-shell-true",
    cwe="CWE-78",
    severity="error",
    confidence="high",
    message=(
        "subprocess called with shell=True and an interpolated/concatenated "
        "command string -- OS command injection risk."
    ),
    language="python",
    matcher={
        "any": [
            {"pattern": "subprocess.run($CMD, shell=True)"},
            {"pattern": "subprocess.call($CMD, shell=True)"},
            {"pattern": "subprocess.Popen($CMD, shell=True)"},
            {"pattern": "subprocess.check_output($CMD, shell=True)"},
            {"pattern": "subprocess.check_call($CMD, shell=True)"},
        ],
        "has": {
            "any": [
                {"pattern": "$A + $B"},
                {"kind": "string", "regex": r"\{"},
            ],
            "stopBy": "end",
        },
    },
    references=("https://cwe.mitre.org/data/definitions/78.html",),
)

# CWE-89 / OWASP A03 Injection: SQL query assembled by string concatenation or
# %-formatting passed straight into .execute(). Parameterized queries
# (.execute(sql, params)) are NOT matched.
_SQL_CONCAT = SecurityRule(
    rule_id="py-sql-string-concat",
    cwe="CWE-89",
    severity="warning",
    confidence="medium",
    message=(
        "SQL string built with concatenation/%-formatting and passed to "
        "execute() -- use parameterized queries instead (SQL injection risk)."
    ),
    language="python",
    matcher={
        "any": [
            {"pattern": "$OBJ.execute($Q + $R)"},
            {"pattern": "$OBJ.execute($Q % $R)"},
            {"pattern": "$OBJ.executemany($Q + $R)"},
            {"pattern": "$OBJ.executemany($Q % $R)"},
        ]
    },
    references=("https://cwe.mitre.org/data/definitions/89.html",),
)

# CWE-798 Hardcoded credentials: a string literal of meaningful length assigned
# to a credential-named identifier. Heuristic -- name + length based -- so it is
# tagged low confidence and heuristic=True.
_HARDCODED_SECRET = SecurityRule(
    rule_id="py-hardcoded-secret",
    cwe="CWE-798",
    severity="warning",
    confidence="low",
    message=(
        "Possible hardcoded secret: a string literal assigned to a "
        "credential-named variable. Move secrets to env/secret storage."
    ),
    language="python",
    heuristic=True,
    matcher={
        "pattern": "$NAME = $VAL",
        "all": [
            {
                "has": {
                    "field": "left",
                    "regex": r"(?i)(password|passwd|secret|api_?key|token|access_?key|private_?key)",
                }
            },
            {
                "has": {
                    "field": "right",
                    "kind": "string",
                    "regex": r".{8,}",
                }
            },
        ],
    },
    references=("https://cwe.mitre.org/data/definitions/798.html",),
)


BUNDLED_RULES: tuple[SecurityRule, ...] = (
    _EVAL_EXEC,
    _SUBPROCESS_SHELL,
    _SQL_CONCAT,
    _HARDCODED_SECRET,
)

_RULES_BY_ID: dict[str, SecurityRule] = {rule.rule_id: rule for rule in BUNDLED_RULES}


def rule_by_id(rule_id: str) -> SecurityRule | None:
    """Look up bundled rule metadata by its stable id."""
    return _RULES_BY_ID.get(rule_id)


def bundled_astgrep_rules(languages: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Return the bundled rules as ast-grep rule dicts, optionally filtered by language."""
    selected = BUNDLED_RULES
    if languages is not None:
        wanted = {lang.lower() for lang in languages}
        selected = tuple(rule for rule in BUNDLED_RULES if rule.language.lower() in wanted)
    return [rule.to_astgrep_rule() for rule in selected]


__all__ = [
    "BUNDLED_RULES",
    "Confidence",
    "SecurityRule",
    "Severity",
    "bundled_astgrep_rules",
    "rule_by_id",
]
