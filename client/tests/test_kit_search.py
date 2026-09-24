"""The shared search engine's own guarantees, independent of either surface."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lemoncrow_client.kit.search import SearchHooks, search_workspace


def _repo(tmp_path: Path) -> Path:
    for name in ("b.py", "a.py", "c.py"):
        (tmp_path / name).write_text("needle\n", encoding="utf-8")
    return tmp_path


def test_a_fast_backend_answers_in_path_order_whatever_order_it_prints(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    shuffled = [root / "c.py", root / "a.py", root / "b.py"]

    def fast_files(**_kwargs: Any) -> list[Path]:
        return list(shuffled)

    def fast_lines(**_kwargs: Any) -> dict[str, list[int]]:
        return {str(path): [1] for path in shuffled}

    hooks = SearchHooks(fast_files=fast_files, fast_lines=fast_lines)
    paths = search_workspace(
        path=".", content_regex="needle", output_mode="file_paths_only", repo_root=root, hooks=hooks
    )
    assert paths["content"][0]["text"] == "# grep (3 files)\n\na.py\nb.py\nc.py"
    first = search_workspace(path=".", content_regex="needle", file_limit=1, repo_root=root, hooks=hooks)
    assert first["content"][0]["text"].startswith("a.py\n")


def test_a_universe_limits_directory_walks_but_not_a_named_file(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    hooks = SearchHooks(universe=lambda _root: frozenset({root.resolve() / "a.py"}))
    walked = search_workspace(
        path=".", content_regex="needle", output_mode="file_paths_only", repo_root=root, hooks=hooks
    )
    assert walked["content"][0]["text"] == "# grep (1 files)\n\na.py"
    named = search_workspace(
        path="c.py", content_regex="needle", output_mode="file_paths_only", repo_root=root, hooks=hooks
    )
    assert named["content"][0]["text"] == "# grep (1 files)\n\nc.py"


def test_without_a_spill_store_a_large_result_stays_inline(tmp_path: Path) -> None:
    (tmp_path / "hay.py").write_text("".join(f"needle {i} " + "x" * 80 + "\n" for i in range(200)), encoding="utf-8")
    result = search_workspace(path=".", content_regex="needle", repo_root=tmp_path)
    assert "artifact" not in result
    assert result["content"][0]["text"].startswith("hay.py\n@@ 1-1\nneedle 0 ")
