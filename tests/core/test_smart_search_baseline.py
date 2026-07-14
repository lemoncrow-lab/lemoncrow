from __future__ import annotations

from pathlib import Path

from lemoncrow.core.capabilities.grounded_loop.search_first import _discovery_calls_saved
from lemoncrow.core.capabilities.native_read_baseline import CLAUDE_NATIVE_READ_LINE_LIMIT
from lemoncrow.pro.capabilities.tool_supervision.smart_search import (
    _iter_text_files,
    _naive_bytes_for_matches,
)


def test_smart_search_chunk_baseline_is_claude_grep_paths(tmp_path: Path) -> None:
    path = tmp_path / "large.py"
    path.write_text("\n".join(f"line_{idx} = 'needle'" for idx in range(5000)), encoding="utf-8")

    matches = [{"path": str(path), "snippets": [{"text": "needle"}]}]

    assert _naive_bytes_for_matches(matches, mode="chunks") == len(str(path))
    assert _naive_bytes_for_matches(matches, mode="chunks") < path.stat().st_size


def test_smart_search_full_baseline_caps_claude_read_output(tmp_path: Path) -> None:
    path = tmp_path / "huge.py"
    lines = [f"line_{idx} = 'needle'" for idx in range(CLAUDE_NATIVE_READ_LINE_LIMIT + 500)]
    path.write_text("\n".join(lines), encoding="utf-8")

    baseline = _naive_bytes_for_matches([{"path": str(path)}], mode="full")
    full_size = path.stat().st_size

    assert baseline < full_size
    assert baseline == len("\n".join(lines[:CLAUDE_NATIVE_READ_LINE_LIMIT]))


def test_iter_text_files_skips_local_artifact_directories(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "keep.py").write_text("needle = 1\n", encoding="utf-8")
    (tmp_path / ".bench-work" / "snapshot").mkdir(parents=True)
    (tmp_path / ".bench-work" / "snapshot" / "copy.py").write_text("needle = 2\n", encoding="utf-8")

    files = {path.relative_to(tmp_path).as_posix() for path in _iter_text_files(tmp_path)}

    assert files == {"src/keep.py"}


def test_search_first_batching_baseline_counts_saved_discovery_roundtrips() -> None:
    matches = [{"path": "src/a.py"}, {"path": "src/b.py"}, {"path": "src/c.py"}]

    assert _discovery_calls_saved(matches) == 2
