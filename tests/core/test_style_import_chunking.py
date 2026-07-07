from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.style_import.importer import split_markdown_text


def test_chunker_handles_headings_code_fences_and_long_sections(tmp_path: Path) -> None:
    text = "\n".join(
        [
            "# Guide",
            "Intro paragraph.",
            "## Procedures",
            "Do the safe thing.",
            "```python",
            "## not a real heading",
            "print('still code')",
            "```",
            "### Verification",
            "Confirm it worked.",
            "## Long Section",
            *("Repeat the explicit procedure sentence." for _ in range(80)),
        ]
    )

    chunks = split_markdown_text(text, file_path=tmp_path / "STYLE.md", max_tokens=80)

    assert len(chunks) >= 3
    assert any("## not a real heading" in chunk.text for chunk in chunks)
    assert any(chunk.text.startswith("### Verification") for chunk in chunks)
    assert all(chunk.start_line <= chunk.end_line for chunk in chunks)
