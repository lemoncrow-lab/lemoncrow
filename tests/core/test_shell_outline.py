from __future__ import annotations

from pathlib import Path

from lemoncrow.pro.capabilities.semantic_file_memory import SemanticFileMemoryCapability


def test_shell_outline_reaches_treesitter_bash(tmp_path: Path) -> None:
    """A .sh file resolves to `bash` and yields a tree-sitter outline.

    Regression for DLS-LANG-03: previously `_language_for` returned "shell"
    while the tree-sitter grammar is keyed "bash", so shell files dead-ended at
    the generic regex outline. Delegating to the canonical registry returns
    "bash", reaching the live grammar.
    """
    source = """
#!/usr/bin/env bash
set -euo pipefail

TOP_LEVEL_CONST=7
export DEPLOY_ENV=production
declare -r MAX_RETRIES=5

run_build() {
    local sentinel_body=42
    echo "$sentinel_body"
    make all
}

deploy() {
    rsync -a ./dist/ server:/var/www/
    echo deployed
}

echo "top level command noise that should be dropped"
ls -la /tmp
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
    # Function signatures kept, bodies stripped (signature-only).
    assert "run_build" in text
    assert "deploy" in text
    assert "sentinel_body" not in text
    # Variable assignments kept verbatim.
    assert "TOP_LEVEL_CONST" in text
    # declaration_command surfaces export/declare lines (retuned bash config).
    assert "export DEPLOY_ENV=production" in text
    assert "declare -r MAX_RETRIES=5" in text
    # Bare top-level command invocations are noise and are NOT kept.
    assert "top level command noise" not in text
    assert "ls -la /tmp" not in text
