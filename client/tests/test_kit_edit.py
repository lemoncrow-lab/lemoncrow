"""The shared edit engine, as the client runs it: no main-package hooks."""

from __future__ import annotations

from pathlib import Path

import pytest
from lemoncrow_client.kit.edit import (
    EditExtensions,
    SymbolTarget,
    TargetSpec,
    apply_edits,
    normalize_edit_aliases,
    parse_target,
    require_content,
)


def _write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_batched_ranges_use_pre_batch_line_numbers(tmp_path: Path) -> None:
    target = _write(tmp_path, "t.txt", "a\nb\nc\nd\ne\n")

    result = apply_edits(
        [
            {"path": "t.txt:L1-L1", "new_string": "A1\nA2\nA3\n"},
            {"path": "t.txt:L4-L4", "new_string": "D\n"},
        ],
        root=tmp_path,
    )

    assert result["failed"] == []
    assert target.read_text(encoding="utf-8") == "A1\nA2\nA3\nb\nc\nD\ne\n"


def test_a_failed_edit_rolls_back_the_whole_batch(tmp_path: Path) -> None:
    first = _write(tmp_path, "one.txt", "keep\n")
    second = _write(tmp_path, "two.txt", "x\n")

    result = apply_edits(
        [
            {"path": "one.txt", "old_string": "keep", "new_string": "changed"},
            {"path": "two.txt", "old_string": "missing", "new_string": "y"},
        ],
        root=tmp_path,
    )

    assert result["rolled_back"] is True
    assert result["failed"][0]["edit_index"] == 1
    assert first.read_text(encoding="utf-8") == "keep\n"
    assert second.read_text(encoding="utf-8") == "x\n"


def test_a_miss_ships_the_disk_text_to_retry_with(tmp_path: Path) -> None:
    _write(tmp_path, "m.py", "def f():\n    return 1\n")

    result = apply_edits(
        [{"path": "m.py", "old_string": "def f():\n    return 2\n", "new_string": "def f():\n    return 3\n"}],
        root=tmp_path,
    )

    failure = result["failed"][0]
    assert failure["error"] == "old_string not found in file"
    assert failure["retry_with"]["path"] == "m.py:L1-L2"
    assert failure["retry_with"]["old_string"] == "def f():\n    return 1\n"


def test_a_stale_retry_is_recognized_as_already_applied(tmp_path: Path) -> None:
    target = _write(tmp_path, "mod.py", "x = 2\n")

    result = apply_edits([{"path": "mod.py", "old_string": "x = 1", "new_string": "x = 2"}], root=tmp_path)

    assert result["applied"][0]["match_mode"] == "noop"
    assert target.read_text(encoding="utf-8") == "x = 2\n"


@pytest.mark.parametrize("new_string", ["\n", "beta\n"])
def test_a_blank_or_repeated_replacement_is_not_proof_the_edit_was_applied(tmp_path: Path, new_string: str) -> None:
    target = _write(tmp_path, "f.txt", "alpha\nbeta\n\nbeta\n")

    result = apply_edits([{"path": "f.txt", "old_string": "gamma\n", "new_string": new_string}], root=tmp_path)

    assert result["failed"][0]["error"] == "old_string not found in file"
    assert target.read_text(encoding="utf-8") == "alpha\nbeta\n\nbeta\n"


def test_a_range_past_the_end_of_the_file_is_refused(tmp_path: Path) -> None:
    target = _write(tmp_path, "f.txt", "a\nb\n")

    result = apply_edits([{"path": "f.txt:L3-L3", "new_string": "c\n"}], root=tmp_path)

    assert result["failed"][0]["error"].startswith("range L3 starts past the end of f.txt (2 lines)")
    assert target.read_text(encoding="utf-8") == "a\nb\n"


def test_the_parse_gate_refuses_broken_python(tmp_path: Path) -> None:
    target = _write(tmp_path, "m.py", "x = 1\n")

    result = apply_edits([{"path": "m.py", "old_string": "x = 1", "new_string": "x = ("}], root=tmp_path)

    assert result["rolled_back"] is True
    assert "post-edit parse error in m.py" in result["failed"][0]["error"]
    assert target.read_text(encoding="utf-8") == "x = 1\n"


def test_protected_directories_are_refused(tmp_path: Path) -> None:
    result = apply_edits([{"path": ".git/config", "new_string": "x", "replace": True}], root=tmp_path)

    assert "protected path denied" in result["failed"][0]["error"]
    assert not (tmp_path / ".git" / "config").exists()


def test_paths_outside_the_root_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"

    result = apply_edits([{"path": str(outside), "new_string": "x", "replace": True}], root=root)

    assert "path escape denied" in result["failed"][0]["error"]
    assert not outside.exists()


def test_symbol_and_projection_edits_need_their_hooks(tmp_path: Path) -> None:
    _write(tmp_path, "m.py", "x = 1\n")

    symbol = apply_edits([{"kind": "symbol", "symbol": "m.x", "new_string": "x = 2"}], root=tmp_path)
    projection = apply_edits(
        [{"kind": "projection", "path": "m.py", "projected_start": 0, "projected_end": 1, "new_string": "y"}],
        root=tmp_path,
    )

    assert symbol["failed"][0]["error"].startswith("symbol edits need the LemonCrow code index")
    assert projection["failed"][0]["error"].startswith("projection edits need the LemonCrow server's projection")


def test_a_whitespace_divergent_anchor_misses_without_the_fuzzy_hook(tmp_path: Path) -> None:
    target = _write(tmp_path, "m.py", "def compute(a, b):\n    return a + b\n")

    result = apply_edits(
        [
            {
                "path": "m.py",
                "old_string": "def compute(a,b):\n    return a+b\n",
                "new_string": "def compute(a, b):\n    return a * b\n",
            }
        ],
        root=tmp_path,
    )

    assert result["failed"][0]["error"] == "old_string not found in file"
    assert target.read_text(encoding="utf-8") == "def compute(a, b):\n    return a + b\n"


def test_hooks_extend_matching_and_run_after_writes(tmp_path: Path) -> None:
    target = _write(tmp_path, "m.py", "value = 1\n")
    written: list[list[str]] = []
    extensions = EditExtensions(
        fuzzy_replace=lambda scoped, old, new: (scoped.replace("value = 1", new), 1, 1),
        after_write=lambda root, paths: written.append(sorted(path.name for path in paths)),
    )

    result = apply_edits(
        [{"path": "m.py", "old_string": "value=1", "new_string": "value = 2"}],
        root=tmp_path,
        extensions=extensions,
    )

    assert result["applied"][0]["match_mode"] == "fuzzy"
    assert target.read_text(encoding="utf-8") == "value = 2\n"
    assert written == [["m.py"]]


def test_the_symbol_hook_targets_the_resolved_span(tmp_path: Path) -> None:
    target = _write(tmp_path, "m.py", "def f():\n    return 1\n")
    recorded: list[str] = []
    extensions = EditExtensions(
        resolve_symbol=lambda edit, root: SymbolTarget(
            scoped_file_path="m.py",
            old_string="return 1",
            new_string="return 2",
            symbol_id="m.f",
            handle=None,
        ),
        after_symbol_edits=lambda targets: recorded.extend(target.symbol_id for target in targets),
    )

    result = apply_edits([{"kind": "symbol", "symbol": "m.f"}], root=tmp_path, extensions=extensions)

    assert result["applied"][0]["kind"] == "symbol"
    assert result["applied"][0]["symbol_id"] == "m.f"
    assert target.read_text(encoding="utf-8") == "def f():\n    return 2\n"
    assert recorded == ["m.f"]


def test_aliases_are_promoted_and_content_is_required() -> None:
    assert normalize_edit_aliases({"path": "a", "old": "x", "new": "y"})["old_string"] == "x"
    whole = normalize_edit_aliases({"path": "a", "replace": True, "content": "body"})
    assert whole["new_string"] == "body"
    assert whole["replace"] is True

    with pytest.raises(ValueError, match="at least one"):
        require_content([])
    with pytest.raises(ValueError, match=r"edits\[0\] has no replacement content: provide new$"):
        require_content([{"path": "a", "old_string": "x"}])
    require_content([{"path": "a", "new_string": ""}])
    require_content([{"kind": "symbol"}])


def test_parse_target_reads_every_suffix() -> None:
    assert parse_target("a.py:L3-L5") == TargetSpec(path="a.py", start_line=3, end_line=5)
    assert parse_target("a.py:minified:L3").minified is True
    assert parse_target("nb.ipynb#cell=2").cell == "2"
    assert parse_target("a.py:full").whole_file is True
