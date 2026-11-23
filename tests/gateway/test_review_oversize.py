"""Oversized files remain reviewable entries without hiding other diffs."""

import hashlib
import itertools
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pygit2
import pytest

from lemoncrow.pro.capabilities.review.api import synthesize_file_patch
from lemoncrow.pro.capabilities.review.gitdiff import MAX_DIFF_FILE_BYTES, collect_diff, resolve_rev_range
from lemoncrow.pro.capabilities.review.models import oversized_diff_reason
from lemoncrow.pro.capabilities.review.snapshot import ReviewSnapshotUnavailable, _materialize_source_tree_snapshot


def test_size_limit_keeps_file_entry_and_other_diffs(tmp_path: Path) -> None:
    for case_index, (mode, size) in enumerate(
        itertools.product(["modified", "added", "deleted"], [MAX_DIFF_FILE_BYTES, MAX_DIFF_FILE_BYTES + 1])
    ):
        case_tmp_path = tmp_path / f"case-{case_index}"
        case_tmp_path.mkdir()
        repo = pygit2.init_repository(str(case_tmp_path))
        large = case_tmp_path / "large.txt"
        if mode != "added":
            large.write_text("a" * size)
        (case_tmp_path / "small.py").write_text("x = 1\n")
        repo.index.add_all()
        repo.index.write()
        signature = pygit2.Signature("Test", "test@example.com")
        repo.create_commit("HEAD", signature, signature, "base", repo.index.write_tree(), [])
        if mode == "deleted":
            large.unlink()
        else:
            large.write_text("b" * size)
        (case_tmp_path / "small.py").write_text("x = 2\n")
        diff = collect_diff(
            case_tmp_path,
            resolve_rev_range(case_tmp_path, working_tree=True),
            with_patch_text=True,
            patch_text_limit=None,
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


def test_size_notice_uses_readable_units() -> None:
    for limit, expected in [(512, "512 bytes"), (2048, "2 KiB"), (1048576, "1 MiB"), (1073741824, "1 GiB")]:
        assert expected in oversized_diff_reason(limit)


def test_missing_large_source_still_blocks_execution(tmp_path: Path) -> None:
    class Store:
        def read_source_tree_artifact(self, *_args, **_kwargs):
            return {"large.txt": {"content_digest": "a" * 64, "size": MAX_DIFF_FILE_BYTES + 1, "mode": 0o100644}}

        def read_source_content(self, _digest):
            return None

    with pytest.raises(ReviewSnapshotUnavailable, match=r"large\.txt"):
        _materialize_source_tree_snapshot(
            Store(), SimpleNamespace(review_id="review", id="revision"), tmp_path / "runtime", scope=".", side="new"
        )


def _tree_store(files: dict[str, tuple[bytes, bytes | None]]):
    """A stored source tree: path -> (the bytes it names, what the content store returns)."""

    class Store:
        def read_source_tree_artifact(self, *_args, **_kwargs):
            return {
                path: {"content_digest": hashlib.sha256(named).hexdigest(), "size": len(named), "mode": 0o100644}
                for path, (named, _held) in files.items()
            }

        def read_source_content(self, digest):
            for named, held in files.values():
                if hashlib.sha256(named).hexdigest() == digest:
                    return held
            return None

    return Store()


_REVISION = SimpleNamespace(review_id="review", id="revision")


def test_a_reader_that_tolerates_absence_gets_the_rest_of_the_tree(tmp_path: Path) -> None:
    store = _tree_store({"kept.py": (b"x = 1\n", b"x = 1\n"), "docs/demo.gif": (b"GIF", None)})
    unavailable: list[str] = []
    target = tmp_path / "ask"

    assert _materialize_source_tree_snapshot(store, _REVISION, target, scope=".", side="new", unavailable=unavailable)

    assert unavailable == ["docs/demo.gif"]
    assert (target / "kept.py").read_bytes() == b"x = 1\n"
    assert not (target / "docs" / "demo.gif").exists()


@pytest.mark.parametrize(
    ("named", "held", "message"),
    [(b"expected", b"tampered", "does not match its digest"), (b"expected", b"expected!", "does not match its digest")],
)
def test_wrong_bytes_refuse_even_a_reader_that_tolerates_absence(
    tmp_path: Path, named: bytes, held: bytes, message: str
) -> None:
    store = _tree_store({"kept.py": (b"x = 1\n", b"x = 1\n"), "bad.bin": (named, held)})

    with pytest.raises(ReviewSnapshotUnavailable, match=message):
        _materialize_source_tree_snapshot(store, _REVISION, tmp_path / "ask", scope=".", side="new", unavailable=[])


def test_a_recorded_size_that_disagrees_refuses_a_tolerant_reader(tmp_path: Path) -> None:
    class Store:
        def read_source_tree_artifact(self, *_args, **_kwargs):
            return {"f.bin": {"content_digest": hashlib.sha256(b"abc").hexdigest(), "size": 4, "mode": 0o100644}}

        def read_source_content(self, _digest):
            return b"abc"

    with pytest.raises(ReviewSnapshotUnavailable, match="size changed"):
        _materialize_source_tree_snapshot(Store(), _REVISION, tmp_path / "ask", scope=".", side="new", unavailable=[])
