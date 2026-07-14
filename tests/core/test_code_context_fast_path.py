"""Unit tests for the symbol-exact fast-path gate (_hef_exact_is_decisive).

The gate decides when the exact-symbol channel alone resolves a
definition/symbol-intent explore query, letting _submit_hef_channels skip the
anchor-Zoekt and line-FTS recall channels (the dominant explore cost).
"""

from __future__ import annotations

from typing import Any

from lemoncrow.pro.capabilities.code_context.engine import (
    _hef_exact_is_decisive,
    _HefQueryPlan,
)


def _plan(intent: str) -> _HefQueryPlan:
    return _HefQueryPlan(
        intent=intent,
        definitions=(("def", "target_helper"),),
        identifiers=("target_helper",),
        anchors=("target_helper",),
        terms=("target_helper",),
        literals=(),
        wants_tests=False,
        wants_auxiliary=False,
    )


def _detail(
    confidence: float,
    *,
    definition_tokens: tuple[str, ...] = ("target_helper",),
    best_df: int = 1,
) -> dict[str, Any]:
    return {
        "tokens": ["target_helper"],
        "definition_tokens": list(definition_tokens),
        "kind_matches": ["target_helper"],
        "idf": 1.0,
        "best_df": best_df,
        "confidence": confidence,
    }


def test_decisive_high_confidence_rare_definition() -> None:
    assert _hef_exact_is_decisive("def target_helper", _plan("definition"), ["a.py"], {"a.py": _detail(0.95)})


def test_not_decisive_for_regex_shaped_query() -> None:
    # Escapes/groups/classes (anything beyond `a|b` alternation) ask for CONTENT
    # matches -- exactly what the line-FTS channel serves, so never decisive.
    assert not _hef_exact_is_decisive(
        r"def target_helper\(self", _plan("definition"), ["a.py"], {"a.py": _detail(0.95)}
    )


def test_not_decisive_without_definition_kind() -> None:
    # Top hit merely binds a variable of the queried name -- not a definition.
    assert not _hef_exact_is_decisive(
        "def target_helper", _plan("definition"), ["a.py"], {"a.py": _detail(0.95, definition_tokens=())}
    )


def test_not_decisive_on_common_name() -> None:
    # Collision-heavy names ('save', 'get') need the full pipeline's ranking.
    assert not _hef_exact_is_decisive(
        "def target_helper", _plan("definition"), ["a.py"], {"a.py": _detail(0.95, best_df=40)}
    )


def test_not_decisive_on_confidence_tie() -> None:
    # Same-name definitions in sibling files tie at equal confidence; the exact
    # channel cannot order them (e.g. a `client` fixture in three conftests).
    details = {"a.py": _detail(0.9), "b.py": _detail(0.9)}
    assert not _hef_exact_is_decisive("def target_helper", _plan("definition"), ["a.py", "b.py"], details)


def test_decisive_with_separated_runner_up() -> None:
    details = {"a.py": _detail(0.9), "b.py": _detail(0.5)}
    assert _hef_exact_is_decisive("def target_helper", _plan("definition"), ["a.py", "b.py"], details)


def test_not_decisive_for_prose_intent() -> None:
    assert not _hef_exact_is_decisive("target_helper", _plan("prose"), ["a.py"], {"a.py": _detail(0.95)})


def test_intent_aware_confidence_floor() -> None:
    # Bare-identifier (symbol-intent) confidence caps at 0.70 in the exact
    # channel's formula, so the symbol floor sits below the definition floor.
    assert not _hef_exact_is_decisive("target_helper", _plan("symbol"), ["a.py"], {"a.py": _detail(0.60)})
    assert _hef_exact_is_decisive("target_helper", _plan("symbol"), ["a.py"], {"a.py": _detail(0.68)})
