from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.code_context import CodeContextEngine
from atelier.core.capabilities.code_context.output_policy import CONTEXT_COMPACT, TRUNCATION_MARKER


def test_tool_explore_caps_merged_source_sections(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    body_lines = [f"    value_{idx} = {idx}" for idx in range(120)]
    source = "\n".join(
        [
            "def big_token_helper_a() -> int:",
            *body_lines,
            "    return value_119",
            "",
            "def big_token_helper_b() -> int:",
            *body_lines,
            "    return value_119",
            "",
        ]
    )
    (src / "helpers.py").write_text(source, encoding="utf-8")

    engine = CodeContextEngine(tmp_path, db_path=tmp_path / "code.sqlite")
    payload = engine.tool_explore(
        "big_token_helper",
        max_files=2,
        max_symbols=10,
        include_source=True,
        include_relationships=False,
        line_numbers=True,
        budget_tokens=12_000,
    )

    files = payload["files"]
    assert files
    sections = files[0]["source_sections"]
    assert len(sections) == 1
    content = sections[0]["content"]
    assert content.startswith("1\tdef big_token_helper_a() -> int:")
    assert TRUNCATION_MARKER in content
    assert len(content) <= CONTEXT_COMPACT.max_code_block_chars + 1
    assert "def big_token_helper_b() -> int:" not in content
