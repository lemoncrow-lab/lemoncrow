"""Oversized files remain reviewable entries without hiding other diffs."""

from dataclasses import asdict
from pathlib import Path

import pygit2
import pytest

from lemoncrow.pro.capabilities.review.api import synthesize_file_patch
from lemoncrow.pro.capabilities.review.gitdiff import MAX_DIFF_FILE_BYTES, collect_diff, resolve_rev_range
from lemoncrow.pro.capabilities.review.models import oversized_diff_reason
from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable, _materialize_source_tree_snapshot


@pytest.mark.parametrize("mode", ["modified", "added", "deleted"])
@pytest.mark.parametrize("size", [MAX_DIFF_FILE_BYTES, MAX_DIFF_FILE_BYTES + 1])
def test_size_limit_keeps_file_entry_and_other_diffs(tmp_path: Path, mode: str, size: int) -> None:
    repo = pygit2.init_repository(str(tmp_path))
    large = tmp_path / "large.txt"
    if mode != "added":
        large.write_text("a" * size)
    (tmp_path / "small.py").write_text("x = 1\n")
    repo.index.add_all()
    repo.index.write()
    signature = pygit2.Signature("Test", "test@example.com")
    repo.create_commit("HEAD", signature, signature, "base", repo.index.write_tree(), [])
    if mode == "deleted":
        large.unlink()
    else:
        large.write_text("b" * size)
    (tmp_path / "small.py").write_text("x = 2\n")

    diff = collect_diff(
        tmp_path, resolve_rev_range(tmp_path, working_tree=True), with_patch_text=True, patch_text_limit=None
    )
    files = {item.path: item for item in diff.files}
    assert set(files) == {"large.txt", "small.py"}
    assert synthesize_file_patch(asdict(files["small.py"]))[0]
    patch, refusal, detail = synthesize_file_patch(asdict(files["large.txt"]))
    if size > MAX_DIFF_FILE_BYTES:
        assert patch == ""
        assert refusal == "file_too_large"
        assert "1 MiB (1,048,576 bytes)" in detail
        assert not files["large.txt"].hunks
    else:
        assert patch
        assert refusal == ""


@pytest.mark.parametrize(
    "limit, expected", [(512, "512 bytes"), (2048, "2 KiB"), (1048576, "1 MiB"), (1073741824, "1 GiB")]
)
def test_size_notice_uses_readable_units(limit: int, expected: str) -> None:
    assert expected in oversized_diff_reason(limit)


def test_missing_large_source_still_blocks_execution(tmp_path: Path) -> None:
    from types import SimpleNamespace

    class Store:
        def read_source_tree_artifact(self, *_args, **_kwargs):
            return {"large.txt": {"content_digest": "a" * 64, "size": MAX_DIFF_FILE_BYTES + 1, "mode": 0o100644}}

        def read_source_content(self, _digest):
            return None

    with pytest.raises(ReviewSnapshotUnavailable, match=r"large\.txt"):
        _materialize_source_tree_snapshot(
            Store(), SimpleNamespace(review_id="review", id="revision"), tmp_path / "runtime", scope=".", side="new"
        )
