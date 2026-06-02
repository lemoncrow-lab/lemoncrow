from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.code_context import CodeContextEngine
from atelier.core.capabilities.code_context.output_policy import (
    TRUNCATION_MARKER,
    hard_cap_chars,
    resolve_output_policy,
)


def _write_fixture_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "orders.py").write_text(
        "class OrderService:\n"
        "    def calculate_total(self, items: list[int]) -> int:\n"
        "        return sum(items)\n"
        "\n"
        "def helper() -> OrderService:\n"
        "    return OrderService()\n",
        encoding="utf-8",
    )


def test_hard_cap_chars_truncates_and_marks_suffix() -> None:
    text = "line1\nline2\nline3\nline4"
    capped = hard_cap_chars(text, 12)

    assert capped.endswith(TRUNCATION_MARKER)
    assert len(capped) > 12


def test_resolve_output_policy_has_locked_phase1_caps() -> None:
    assert resolve_output_policy("search").max_total_tokens == 1400
    assert resolve_output_policy("relation").max_total_tokens == 1700
    assert resolve_output_policy("context").max_total_tokens == 5000
    assert resolve_output_policy("outline").max_total_tokens == 2400
    assert resolve_output_policy("node").max_total_tokens == 1800


def test_tool_specific_hard_caps_are_enforced(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    assert engine.tool_index(budget_tokens=99_999)["total_tokens"] <= 80
    assert engine.tool_cache_status(budget_tokens=99_999)["total_tokens"] <= 50
    assert engine.tool_search("OrderService", limit=20, budget_tokens=99_999)["total_tokens"] <= 300
    assert (
        engine.tool_symbol(qualified_name="OrderService", file_path="src/orders.py", budget_tokens=99_999)[
            "total_tokens"
        ]
        <= 300
    )
    outline_payload = engine.tool_outline(file_path="src/orders.py", budget_tokens=99_999)
    assert outline_payload.get("error") is None
    assert outline_payload["total_tokens"] <= 150


def test_tool_search_budget_tokens_cannot_exceed_policy_safety_cap(tmp_path: Path) -> None:
    _write_fixture_repo(tmp_path)
    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")

    payload = engine.tool_search("OrderService", limit=20, budget_tokens=99_999)

    assert payload["total_tokens"] <= 300
