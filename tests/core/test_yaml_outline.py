from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.semantic_file_memory import SemanticFileMemoryCapability


def test_yaml_outline_reaches_treesitter(tmp_path: Path) -> None:
    """A .yaml file resolves to `yaml` and yields a tree-sitter outline.

    DLS-OUTLINE-03: YAML structure is buried three wrapper levels deep
    (``stream → document → block_node → block_mapping``). After the 17-01
    ``unwrap`` + ``keep_first_line`` generalization, the wrappers are descended
    and only the first line of each top-level ``block_mapping_pair`` is emitted,
    so top-level document keys appear while deeply nested scalars are dropped
    (~18% of source — a strong win).
    """
    source = """
name: ci
on:
  push:
    branches:
      - main
      - release
  pull_request:
    branches:
      - main
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run the deeply nested build step
        run: make all DEEPLY_NESTED_SCALAR_VALUE=do-not-leak
      - name: Upload artifacts
        run: make upload
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Run tests
        run: make test
""".strip()
    path = tmp_path / "ci.yaml"
    path.write_text(source, encoding="utf-8")

    cap = SemanticFileMemoryCapability(tmp_path)
    payload = cap.smart_read(path, expand=False, outline_threshold=0)

    # Canonical registry resolves the .yaml extension to the "yaml" key.
    assert payload["language"] == "yaml"
    assert payload["mode"] == "outline"

    outline = payload["outline"]
    assert isinstance(outline, dict)
    # The payoff: tree-sitter outline, NOT the generic regex fallback.
    assert outline["kind"] == "treesitter"

    text = outline["text"]
    # Top-level document keys are present.
    assert "name:" in text
    assert "on:" in text
    assert "jobs:" in text
    # Deeply nested scalar values are excluded.
    assert "DEEPLY_NESTED_SCALAR_VALUE" not in text
    assert "do-not-leak" not in text
