from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from lemoncrow.pro.capabilities.tool_supervision.native_search import (
    MAX_STRUCTURED_OUTPUT_CHARS,
    _match_line_numbers,
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
        file_glob_patterns=["*.py"],
        lines_before=1,
        lines_after=1,
        repo_root=tmp_path / "src",
    )

    text = "\n".join(_texts(result))
    assert result["_meta"]["fileMatchCount"] == 1
    assert "a.py" in text
    assert "alpha" in text
    assert "omega" in text
    assert "b.md" not in text


def test_native_search_type_alias_line_suffix_and_modified_since(tmp_path: Path) -> None:
    path = tmp_path / "example.py"
    path.write_text("one\ntwo\nthree\n", encoding="utf-8")

    result = search_workspace(path=".", file_glob_patterns=["example.py:L2-L3"], type="python", repo_root=tmp_path)
    text = "\n".join(_texts(result))
    assert "example.py" in text
    assert "two" in text
    assert "three" in text

    skipped = search_workspace(
        path=".",
        file_glob_patterns=["example.py"],
        if_modified_since="2999-01-01",
        repo_root=tmp_path,
    )
    assert "unchanged" in "\n".join(_texts(skipped))


def test_native_search_path_range_suffix_scopes_matches(tmp_path: Path) -> None:
    path = tmp_path / "store.py"
    path.write_text("needle\nplain\nneedle\nplain\nplain\nneedle\n", encoding="utf-8")

    # A bare "path:Lstart-Lend" with no pattern is accepted (guard) as a slice read.
    sliced = search_workspace(path="store.py:L1-L2", output_mode="file_paths_with_content", repo_root=tmp_path)
    assert "store.py:L1-L2" in "\n".join(_texts(sliced))

    # With a regex, the range scopes which matches report: lines 1 and 3 match
    # inside the window; line 6 is outside it and excluded.
    counted = search_workspace(
        path="store.py:L1-L3",
        content_regex="needle",
        output_mode="file_paths_with_match_count",
        repo_root=tmp_path,
    )
    assert "store.py\t2" in "\n".join(_texts(counted))

    # ranked_file_map honors the window too.
    ranked = search_workspace(
        path="store.py:L6", content_regex="needle", output_mode="ranked_file_map", repo_root=tmp_path
    )
    assert ranked["matches"]
    assert ranked["matches"][0]["match_count"] == 1


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


def test_native_search_file_content_mode_honors_context_budget_tokens(tmp_path: Path) -> None:
    path = tmp_path / "big.py"
    path.write_text("\n".join(f"line_{idx} needle value" for idx in range(300)), encoding="utf-8")

    result = search_workspace(
        path=".",
        content_regex="needle",
        file_glob_patterns=["*.py"],
        output_mode="file_paths_with_content",
        lines_before=3,
        lines_after=3,
        lines_per_file=300,
        cap_chars=80_000,
        context_budget_tokens=60,
        repo_root=tmp_path,
    )
    text_chars = sum(len(text) for text in _texts(result))
    assert result["_meta"]["capChars"] == 1_000 * 4
    assert text_chars <= result["_meta"]["capChars"]


def test_native_search_ranked_file_map_baseline_is_path_list(tmp_path: Path) -> None:
    path = tmp_path / "big.py"
    path.write_text("\n".join(f"line_{idx} = 'needle'" for idx in range(1000)), encoding="utf-8")

    result = search_workspace(
        path=".",
        content_regex="needle",
        file_glob_patterns=["*.py"],
        output_mode="ranked_file_map",
        repo_root=tmp_path,
    )

    assert result["_meta"]["fileMatchCount"] == 1
    assert result["tokens_saved"] == 0


def test_match_line_numbers_bails_on_expired_deadline_every_iteration() -> None:
    # Regression: the deadline must be honored on every iteration, not only every
    # 256th, so a ReDoS-prone regex cannot keep scanning lines past the budget.
    import re
    import time

    regex = re.compile("needle")
    lines = ["needle"] * 1000
    expired = time.monotonic() - 1.0

    out = _match_line_numbers(
        lines,
        regex,
        None,
        include_all_when_no_regex=False,
        deadline=expired,
    )

    assert out == []


def test_match_line_numbers_hard_bounds_catastrophic_single_line() -> None:
    # Regression for #13: a catastrophic-backtracking pattern against one very
    # long line must be bounded by a hard wall-clock timeout *inside* the search
    # call, not just between iterations. Use the `regex` engine (which the search
    # path prefers) and a tiny deadline so the test stays fast.
    import time

    from lemoncrow.pro.capabilities.tool_supervision.native_search import _regex_module

    assert _regex_module is not None, "the `regex` engine is a declared dependency"

    pattern = _regex_module.compile(r"(a|a)*$")
    # A run of "a"s ending in a non-"a": `(a|a)*` consumes every "a", `$` then
    # fails, forcing the engine to backtrack catastrophically over every split.
    long_line = "a" * 40 + "b"
    budget = 0.5
    deadline = time.monotonic() + budget

    start = time.monotonic()
    out = _match_line_numbers(
        [long_line],
        pattern,
        None,
        include_all_when_no_regex=False,
        deadline=deadline,
    )
    elapsed = time.monotonic() - start

    # Must bail out (no match recorded) within ~2x the budget rather than hang.
    assert out == []
    assert elapsed < budget * 2


def test_native_search_file_content_mode_spills_large_payload(tmp_path: Path, monkeypatch: Any) -> None:
    spill_dir = tmp_path / "spill"
    monkeypatch.setenv("LEMONCROW_MCP_SPILL_DIR", str(spill_dir))
    path = tmp_path / "large.py"
    path.write_text("\n".join(f"line_{idx} needle payload text" for idx in range(2000)), encoding="utf-8")

    result = search_workspace(
        path=".",
        content_regex="needle",
        file_glob_patterns=["*.py"],
        output_mode="file_paths_with_content",
        lines_before=3,
        lines_after=3,
        lines_per_file=2000,
        context_budget_tokens=1000,
        cap_chars=80_000,
        repo_root=tmp_path,
    )

    assert result["_meta"]["spilled"] is True
    artifact = result["artifact"]
    artifact_path = Path(artifact["path"])
    assert artifact_path.exists()
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    assert payload["_meta"]["fileMatchCount"] >= 1
    assert payload["content"]
    assert "needle" in payload["content"][0]["text"]


def test_native_search_ranked_file_map_includes_root_dotfiles_but_skips_internal_dirs(tmp_path: Path) -> None:
    token = "root_dotfile_unique_token"
    (tmp_path / ".env.example").write_text(f"{token}\n", encoding="utf-8")
    internal = tmp_path / ".lemoncrow"
    internal.mkdir()
    (internal / "state.txt").write_text(f"{token}\n", encoding="utf-8")

    result = search_workspace(
        path=".",
        content_regex=token,
        output_mode="ranked_file_map",
        repo_root=tmp_path,
    )

    files = [match["file"] for match in result["matches"]]
    assert files == [".env.example"]
