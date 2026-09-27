"""Deterministic query planning for code-search retrieval channels.

The planner is intentionally lexical and side-effect free. It classifies the
shape of a request, not its likely answer, so the same query always selects the
same channels regardless of repository contents. Retrieval remains responsible
for tenant/possession checks and exact fallbacks when a bounded candidate source
overflows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = ["QueryIntent", "QueryPlan", "plan_code_query"]

_PRECISE_SYMBOL: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_DEFINITION: Final[re.Pattern[str]] = re.compile(
    r"\b(?:def|class|function|fn|type|struct|interface|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE,
)
_IMPACT: Final[re.Pattern[str]] = re.compile(
    r"\b(?:callers?|callees?|references?|usages?|used\s+by|depends?\s+on|dependencies|dependents|impact|blast\s+radius)\b",
    re.IGNORECASE,
)
_PATH_SUFFIX: Final[re.Pattern[str]] = re.compile(
    r"\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|java|kt|kts|c|cc|cpp|cxx|h|hpp|cs|rb|php|scala|swift|lua|sh|sql|md|json|ya?ml|toml)$",
    re.IGNORECASE,
)
_TOKEN: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_IMPACT_STOP: Final[frozenset[str]] = frozenset(
    {
        "caller",
        "callers",
        "callee",
        "callees",
        "reference",
        "references",
        "usage",
        "usages",
        "used",
        "by",
        "depends",
        "depend",
        "on",
        "dependencies",
        "dependents",
        "impact",
        "blast",
        "radius",
        "of",
        "for",
        "the",
        "a",
        "an",
        "what",
        "show",
        "find",
    }
)


class QueryIntent(StrEnum):
    PIPE = "pipe_alternatives"
    REGEX = "regex"
    EXACT_SYMBOL = "exact_symbol"
    PATH = "path"
    IMPACT = "impact"
    DEFINITION = "definition"
    NATURAL = "natural_language"
    HYBRID = "hybrid"


@dataclass(frozen=True, slots=True)
class QueryPlan:
    intent: QueryIntent
    channels: tuple[str, ...]
    symbol: str = ""
    path: str = ""
    reason: str = ""

    def to_wire(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "intent": self.intent.value,
            "channels": list(self.channels),
        }
        if self.symbol:
            payload["symbol"] = self.symbol
        if self.path:
            payload["path"] = self.path
        if self.reason:
            payload["reason"] = self.reason
        return payload


def _looks_regex(query: str) -> bool:
    stripped = query.strip()
    if not stripped:
        return False
    if stripped.startswith("^") or stripped.endswith("$") or "(?" in stripped:
        return True
    if any(token in stripped for token in (".*", ".+", ".?")):
        return True
    if re.search(r"\\[()\[\]{}.^$*+?|bBdDsSwWAZz]", stripped):
        return True
    return len(stripped) <= 256 and re.search(r"\[[^\]\n]{1,32}\]", stripped) is not None


def _pipe(query: str) -> bool:
    if "|" not in query:
        return False
    meaningful = [part.strip().lstrip("^").rstrip("$") for part in query.split("|")]
    return sum(bool(_TOKEN.search(part)) and len(part) >= 2 for part in meaningful) >= 2


def _path_query(query: str) -> str:
    stripped = query.strip().replace("\\", "/")
    if not stripped or any(character.isspace() for character in stripped):
        return ""
    if "/" in stripped or _PATH_SUFFIX.search(stripped):
        return stripped.removeprefix("./")
    return ""


def _impact_symbol(query: str) -> str:
    if _IMPACT.search(query) is None:
        return ""
    candidates = [token for token in _TOKEN.findall(query) if token.casefold() not in _IMPACT_STOP]
    return candidates[-1] if candidates else ""


def plan_code_query(query: str) -> QueryPlan:
    stripped = query.strip()
    if _pipe(stripped):
        return QueryPlan(QueryIntent.PIPE, ("lexical", "symbol"), reason="explicit_alternatives")
    if _looks_regex(stripped):
        return QueryPlan(QueryIntent.REGEX, ("regex",), reason="explicit_regex_syntax")
    path = _path_query(stripped)
    if path:
        return QueryPlan(QueryIntent.PATH, ("path",), path=path, reason="path_shaped_query")
    if _PRECISE_SYMBOL.fullmatch(stripped) and (
        "." in stripped or "_" in stripped or any(character.isupper() for character in stripped)
    ):
        return QueryPlan(
            QueryIntent.EXACT_SYMBOL,
            ("symbol",),
            symbol=stripped.rsplit(".", 1)[-1],
            reason="unambiguous_code_identifier",
        )
    impact_symbol = _impact_symbol(stripped)
    if impact_symbol:
        return QueryPlan(
            QueryIntent.IMPACT,
            ("symbol", "relations"),
            symbol=impact_symbol,
            reason="impact_relation_language",
        )
    definition = _DEFINITION.search(stripped)
    if definition is not None:
        return QueryPlan(
            QueryIntent.DEFINITION,
            ("symbol", "lexical"),
            symbol=definition.group(1),
            reason="definition_shaped_query",
        )
    tokens = _TOKEN.findall(stripped)
    code_shaped = any(
        "_" in token or token.isupper() or any(character.isupper() for character in token[1:]) for token in tokens
    )
    if len(tokens) >= 4 and not code_shaped:
        return QueryPlan(
            QueryIntent.NATURAL,
            ("lexical", "semantic"),
            reason="multiword_natural_language",
        )
    return QueryPlan(QueryIntent.HYBRID, ("symbol", "lexical", "relations"), reason="mixed_code_query")
