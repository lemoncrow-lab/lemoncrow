from __future__ import annotations

from pathlib import Path

from lemoncrow.pro.capabilities.semantic_file_memory import SemanticFileMemoryCapability


def test_toml_outline_reaches_treesitter(tmp_path: Path) -> None:
    """A .toml file resolves to `toml` and yields a tree-sitter outline.

    DLS-OUTLINE-04: TOML declarations sit at the top level — top-level
    key/value pairs are kept verbatim and ``[table]`` / ``[[array]]`` headers
    are emitted as a first line, while values nested inside tables are dropped.
    """
    source = """
title = "LemonCrow Demo Project"
version = "2.1.0"
authors = ["Pankaj", "The Team"]

[package]
name = "demo-package"
description = "A fairly long description that should never appear in the outline"
keywords = ["alpha", "beta", "gamma"]
license = "MIT"

[[bin]]
name = "demo-cli"
path = "src/main.rs"

[dependencies]
serde = { version = "1.0", features = ["derive"] }
tokio = { version = "1.35", features = ["full"] }
nested_secret_token = "do-not-leak-this-value"

[dev-dependencies]
pytest = "8.0"
ruff = "0.6"
""".strip()
    path = tmp_path / "sample.toml"
    path.write_text(source, encoding="utf-8")

    cap = SemanticFileMemoryCapability(tmp_path)
    payload = cap.smart_read(path, expand=False, outline_threshold=0)

    # Canonical registry resolves the .toml extension to the "toml" key.
    assert payload["language"] == "toml"
    assert payload["mode"] == "outline"

    outline = payload["outline"]
    assert isinstance(outline, dict)
    # The payoff: tree-sitter outline, NOT the generic regex fallback.
    assert outline["kind"] == "treesitter"

    text = outline["text"]
    # Table headers kept (first line only).
    assert "[package]" in text
    assert "[[bin]]" in text
    # Top-level key/value pairs kept verbatim.
    assert "title" in text
    assert "version" in text
    # Values nested inside a table must NOT appear in the outline.
    assert "nested_secret_token" not in text
    assert "do-not-leak-this-value" not in text
