from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability


def test_shell_outline_reaches_treesitter_bash(tmp_path: Path) -> None:
    """A .sh file resolves to `bash` and yields a tree-sitter outline.

    Regression for DLS-LANG-03: previously `_language_for` returned "shell"
    while the tree-sitter grammar is keyed "bash", so shell files dead-ended at
    the generic regex outline. Delegating to the canonical registry returns
    "bash", reaching the live grammar.
    """
    source = """
#!/usr/bin/env bash

TOP_LEVEL_CONST=7

run_build() {
    local sentinel_body=42
    echo "$sentinel_body"
}
""".strip()
    path = tmp_path / "sample.sh"
    path.write_text(source, encoding="utf-8")

    cap = SemanticFileMemoryCapability(tmp_path)
    payload = cap.smart_read(path, expand=False, outline_threshold=0)

    # DLS-LANG-03: shell extension resolves to the canonical bash key.
    assert payload["language"] == "bash"
    assert payload["mode"] == "outline"

    outline = payload["outline"]
    assert isinstance(outline, dict)
    # The payoff: tree-sitter outline, NOT the generic regex fallback.
    assert outline["kind"] == "treesitter"

    text = outline["text"]
    # Function signature kept, body stripped (signature-only).
    assert "run_build" in text
    assert "sentinel_body" not in text
