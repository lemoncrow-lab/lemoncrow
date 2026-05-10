from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from atelier.core.capabilities.tool_supervision.native_search import (
    MAX_STRUCTURED_OUTPUT_CHARS,
    search_workspace,
)


def _texts(result: dict[str, Any]) -> list[str]:
    return [
        str(item.get("text", "")) for item in result["content"] if isinstance(item, dict) and item.get("type") == "text"
    ]


def test_native_search_glob_regex_context_and_counts(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("alpha\nneedle\nomega\n", encoding="utf-8")
    (tmp_path / "src" / "b.md").write_text("needle\n", encoding="utf-8")

    result = search_workspace(
        path=".",
        content_regex="needle",
        file_glob_patterns=["src/**/*.py"],
        lines_before=1,
        lines_after=1,
        repo_root=tmp_path,
    )

    text = "\n".join(_texts(result))
    assert result["_meta"]["fileMatchCount"] == 1
    assert "src/a.py" in text
    assert "alpha" in text
    assert "omega" in text
    assert "src/b.md" not in text


def test_native_search_type_alias_line_suffix_and_modified_since(tmp_path: Path) -> None:
    path = tmp_path / "example.py"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = search_workspace(path=".", file_glob_patterns=["example.py#2-3"], type="python", repo_root=tmp_path)
    text = "\n".join(_texts(result))
    assert "example.py#2-3" in text
    assert "two" in text
    assert "three" in text

    skipped = search_workspace(
        path=".",
        file_glob_patterns=["example.py"],
        if_modified_since="2999-01-01",
        repo_root=tmp_path,
    )
    assert "unchanged" in "\n".join(_texts(skipped))


def test_native_search_notebook_and_image_blocks(tmp_path: Path) -> None:
    notebook = tmp_path / "work.ipynb"
    notebook.write_text(
        '{"cells":[{"cell_type":"code","source":["print(1)"],"outputs":[{"name":"stdout"}]}],"metadata":{},"nbformat":4,"nbformat_minor":5}',
        encoding="utf-8",
    )
    image = tmp_path / "pixel.png"
    image.write_bytes(
        base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII=")
    )

    notebook_result = search_workspace(path=".", file_glob_patterns=["*.ipynb"], repo_root=tmp_path)
    assert "cell 0" in "\n".join(_texts(notebook_result))
    assert "outputs" in "\n".join(_texts(notebook_result))

    image_result = search_workspace(path=".", file_glob_patterns=["*.png"], repo_root=tmp_path)
    image_blocks = [item for item in image_result["content"] if isinstance(item, dict) and item.get("type") == "image"]
    assert image_blocks and image_blocks[0]["mimeType"] == "image/png"


def test_native_search_summary_imports_and_output_cap(tmp_path: Path) -> None:
    path = tmp_path / "module.py"
    path.write_text("import os\nclass Thing:\n    pass\ndef work():\n    pass\n", encoding="utf-8")

    summary = search_workspace(path=".", file_glob_patterns=["module.py"], summary=True, repo_root=tmp_path)
    text = "\n".join(_texts(summary))
    assert "ClassDef: Thing" in text
    assert "FunctionDef: work" in text

    imports = search_workspace(path=".", file_glob_patterns=["module.py#imports"], repo_root=tmp_path)
    assert "- os" in "\n".join(_texts(imports))

    capped = search_workspace(path=".", file_glob_patterns=["module.py"], cap_chars=8, repo_root=tmp_path)
    assert capped["_meta"]["capChars"] == 8
    assert MAX_STRUCTURED_OUTPUT_CHARS == 80_000
