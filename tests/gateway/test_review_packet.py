"""Raw-diff layer of the review packet: rev resolution, hunks, classification.

Fixture repos are built with pygit2 directly (no ``git`` binary, no subprocess)
so these tests exercise the same code path the product uses and stay fast.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pygit2
import pytest

from lemoncrow.pro.capabilities.review.gitdiff import (
    RevRange,
    classify_path,
    collect_diff,
    load_blobs,
    resolve_rev_range,
)
from lemoncrow.pro.capabilities.review.packet import build_review_packet, build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.session_models import ReviewSession
from lemoncrow.pro.capabilities.review.sources.local import range_for_session, source_ref

_HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@pytest.fixture(autouse=True)
def _no_astgrep_download(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Reject ast-grep discovery before it can reach the managed download path.

    ``build_review_packet`` now runs the impact detectors, and ast-grep's managed
    bootstrap would try to *download* a binary into each throwaway repo. ``sg`` is
    the one candidate name ``_reject_reason`` refuses outright, so discovery
    returns "unavailable" without attempting a bootstrap.
    """

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))


def _signature(offset: int) -> pygit2.Signature:
    return pygit2.Signature("Fixture Tester", "fixture@example.com", 1700000000 + offset, 0)


def _init_repo(root: Path) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.com"
    return repo


def _commit(repo: Any, message: str, offset: int) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = _signature(offset)
    oid = repo.create_commit("HEAD", signature, signature, message, tree, parents)
    return str(oid)


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _linear_repo(tmp_path: Path) -> tuple[Path, Any, list[str]]:
    root = tmp_path / "linear"
    repo = _init_repo(root)
    shas: list[str] = []
    _write(root, "src/app.py", "def one():\n    return 1\n")
    shas.append(_commit(repo, "one", 0))
    _write(root, "src/app.py", "def one():\n    return 2\n")
    shas.append(_commit(repo, "two", 60))
    _write(root, "src/app.py", "def one():\n    return 3\n")
    shas.append(_commit(repo, "three", 120))
    return root, repo, shas


def _branched_repo(tmp_path: Path) -> tuple[Path, Any, dict[str, str]]:
    """main: A -> B -> D ; feature branches off B and adds C. merge_base == B."""

    root = tmp_path / "branched"
    repo = _init_repo(root)
    _write(root, "src/app.py", "def one():\n    return 1\n")
    sha_a = _commit(repo, "A", 0)
    _write(root, "src/app.py", "def one():\n    return 2\n")
    sha_b = _commit(repo, "B", 60)

    repo.branches.local.create("feature", repo.get(repo.head.target))
    repo.checkout("refs/heads/feature")
    _write(root, "src/feature.py", "def feature():\n    return 'c'\n")
    sha_c = _commit(repo, "C", 120)

    repo.checkout("refs/heads/main")
    _write(root, "src/main_only.py", "def main_only():\n    return 'd'\n")
    sha_d = _commit(repo, "D", 180)

    repo.checkout("refs/heads/feature")
    return root, repo, {"A": sha_a, "B": sha_b, "C": sha_c, "D": sha_d}


def test_resolve_rev_range_single_rev_uses_merge_base(tmp_path: Path) -> None:
    root, _repo, shas = _linear_repo(tmp_path)
    rng = resolve_rev_range(root, "HEAD~1")
    assert rng.mode == "commit_range"
    assert rng.base_sha == shas[1]
    assert rng.head_sha == shas[2]


def test_resolve_rev_range_three_dot_uses_merge_base(tmp_path: Path) -> None:
    root, _repo, shas = _branched_repo(tmp_path)
    rng = resolve_rev_range(root, "main...HEAD")
    assert rng.base_sha == shas["B"]
    assert rng.base_sha != shas["D"]
    assert rng.merge_base_sha == shas["B"]
    assert rng.head_sha == shas["C"]


def test_resolve_rev_range_two_dot_is_literal(tmp_path: Path) -> None:
    root, _repo, shas = _branched_repo(tmp_path)
    rng = resolve_rev_range(root, "main..HEAD")
    assert rng.base_sha == shas["D"]
    assert rng.merge_base_sha == ""
    assert rng.head_sha == shas["C"]


def test_resolve_rev_range_defaults_to_working_tree_when_dirty(tmp_path: Path) -> None:
    root, _repo, _shas = _linear_repo(tmp_path)
    _write(root, "src/app.py", "def one():\n    return 4\n")
    rng = resolve_rev_range(root)
    assert rng.mode == "working_tree"
    assert rng.dirty is True
    assert rng.head_rev == "WORKDIR"
    assert rng.head_sha == ""


def test_resolve_rev_range_defaults_to_head_parent_when_clean(tmp_path: Path) -> None:
    root, _repo, shas = _linear_repo(tmp_path)
    rng = resolve_rev_range(root)
    assert rng.mode == "commit_range"
    assert rng.base_rev == "HEAD~1"
    assert rng.base_sha == shas[1]


def test_resolve_rev_range_single_commit_repo_uses_empty_tree(tmp_path: Path) -> None:
    root = tmp_path / "single"
    repo = _init_repo(root)
    _write(root, "src/app.py", "def one():\n    return 1\n")
    _commit(repo, "only", 0)
    rng = resolve_rev_range(root)
    assert rng.mode == "commit_range"
    assert rng.base_sha == ""
    assert rng.base_rev == "(empty tree)"
    files = collect_diff(root, rng).files
    assert [item.path for item in files] == ["src/app.py"]
    assert files[0].status == "added"


def test_resolve_rev_range_unknown_spec_raises_value_error(tmp_path: Path) -> None:
    root, _repo, _shas = _linear_repo(tmp_path)
    with pytest.raises(ValueError, match="cannot resolve revision"):
        resolve_rev_range(root, "no-such-ref")


def test_changed_files_detects_rename(tmp_path: Path) -> None:
    root = tmp_path / "rename"
    repo = _init_repo(root)
    body = "".join(f"line_{index} = {index}\n" for index in range(40))
    _write(root, "a.py", body)
    _commit(repo, "add a", 0)

    (root / "a.py").unlink()
    _write(root, "b.py", body.replace("line_7 = 7", "line_7 = 700"))
    _commit(repo, "rename a to b", 60)

    rng = resolve_rev_range(root, "HEAD~1..HEAD")
    files = collect_diff(root, rng).files
    renamed = [item for item in files if item.status == "renamed"]
    assert len(renamed) == 1, [(f.path, f.status) for f in files]
    assert renamed[0].path == "b.py"
    assert renamed[0].old_path == "a.py"
    assert renamed[0].similarity >= 50


def test_changed_files_marks_binary_without_hunks(tmp_path: Path) -> None:
    root = tmp_path / "binary"
    repo = _init_repo(root)
    _write(root, "README.md", "seed\n")
    _commit(repo, "seed", 0)

    png = root / "logo.png"
    png.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8)
    _commit(repo, "add png", 60)

    rng = resolve_rev_range(root, "HEAD~1..HEAD")
    files = {item.path: item for item in collect_diff(root, rng).files}
    entry = files["logo.png"]
    assert entry.is_binary is True
    assert entry.hunks == ()
    assert entry.additions == 0
    assert entry.deletions == 0


def test_changed_files_hunk_line_numbers_match_diff(tmp_path: Path) -> None:
    root = tmp_path / "hunks"
    repo = _init_repo(root)
    original = [f"value_{index} = {index}" for index in range(60)]
    _write(root, "src/wide.py", "\n".join(original) + "\n")
    _commit(repo, "seed", 0)

    edited = list(original)
    for index in (5, 30, 55):
        edited[index] = f"value_{index} = {index * 10}"
    _write(root, "src/wide.py", "\n".join(edited) + "\n")
    _commit(repo, "three edits", 60)

    rng = resolve_rev_range(root, "HEAD~1..HEAD")
    entry = collect_diff(root, rng).files[0]
    assert len(entry.hunks) == 3
    for hunk in entry.hunks:
        match = _HUNK_HEADER_RE.match(hunk.header)
        assert match is not None, hunk.header
        assert hunk.old_start == int(match.group(1))
        assert hunk.old_lines == int(match.group(2) or 1)
        assert hunk.new_start == int(match.group(3))
        assert hunk.new_lines == int(match.group(4) or 1)
        assert hunk.added == 1
        assert hunk.removed == 1
    assert entry.additions == 3
    assert entry.deletions == 3


def test_changed_files_classifies_categories(tmp_path: Path) -> None:
    root = tmp_path / "categories"
    repo = _init_repo(root)
    _write(root, "seed.txt", "seed\n")
    _commit(repo, "seed", 0)

    _write(root, "tests/test_thing.py", "def test_thing():\n    assert True\n")
    _write(root, "vendor/lib.js", "module.exports = 1;\n")
    _write(root, "uv.lock", "version = 1\n")
    _write(root, "README.md", "# hi\n")
    _write(root, "src/x.py", "x = 1\n")
    _write(root, "settings.yaml", "a: 1\n")
    _commit(repo, "many kinds", 60)

    rng = resolve_rev_range(root, "HEAD~1..HEAD")
    categories = {item.path: item.category for item in collect_diff(root, rng).files}
    assert categories["tests/test_thing.py"] == "test"
    assert categories["vendor/lib.js"] == "vendor"
    assert categories["uv.lock"] == "generated"
    assert categories["README.md"] == "docs"
    assert categories["src/x.py"] == "production"
    assert categories["settings.yaml"] == "config"


def test_classify_path_detects_generated_banner() -> None:
    assert classify_path("src/api_types.py", head_preview="# @generated by codegen\nx = 1\n") == "generated"
    assert classify_path("src/api_types.py", head_preview="x = 1\n") == "production"


def test_changed_files_single_pass_no_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, _repo, _shas = _linear_repo(tmp_path)

    def boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("lc review must never fork git")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(subprocess, "check_output", boom)

    rng = resolve_rev_range(root, "HEAD~1")
    packet = build_review_packet(root, rng, store_root=tmp_path / "store")
    assert packet.stats["files"] == 1
    assert packet.schema_version == 2


def test_working_tree_range_includes_untracked_file(tmp_path: Path) -> None:
    root, _repo, _shas = _linear_repo(tmp_path)
    _write(root, "src/brand_new.py", "def added_by_agent():\n    return 1\n")
    rng = resolve_rev_range(root, working_tree=True)
    files = {item.path: item for item in collect_diff(root, rng).files}
    assert "src/brand_new.py" in files
    assert files["src/brand_new.py"].status == "added"
    assert files["src/brand_new.py"].additions == 2


def test_staged_range_diffs_head_against_index(tmp_path: Path) -> None:
    root, repo, _shas = _linear_repo(tmp_path)
    _write(root, "src/staged_only.py", "x = 1\n")
    repo.index.add("src/staged_only.py")
    repo.index.write()

    rng = resolve_rev_range(root, staged=True)
    assert rng.mode == "staged"
    assert rng.head_rev == "INDEX"
    assert rng.head_sha == ""
    paths = [item.path for item in collect_diff(root, rng).files]
    assert paths == ["src/staged_only.py"]


def test_staged_range_agrees_with_git_diff_cached_on_intent_to_add(tmp_path: Path) -> None:
    """`git add -N` paths must be absent from `--staged`, and said out loud.

    They hold no staged content: `git diff --cached` hides them and `git commit`
    leaves them out of the tree it writes, while the raw tree-to-index diff
    libgit2 hands us reports each as an `A` with +0 -0. A reviewer who
    cross-checks the two must not be shown a contradiction -- and must still be
    told the paths exist, which is what the degraded signal is for.

    This is the one fixture the pygit2-only rule cannot build: ``IndexEntry``
    exposes no extended flags, so the placeholder is written with ``git`` and
    ``git diff --cached`` is used as the oracle it is being matched against.
    """

    root, _repo, _shas = _linear_repo(tmp_path)
    _write(root, "src/promised.py", "def promised():\n    return 1\n")
    _write(root, "src/genuinely_empty.py", "")
    subprocess.run(["git", "add", "-N", "src/promised.py"], cwd=root, check=True)
    subprocess.run(["git", "add", "src/genuinely_empty.py"], cwd=root, check=True)

    oracle = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert oracle == ["src/genuinely_empty.py"], oracle

    result = collect_diff(root, resolve_rev_range(root, staged=True))

    # An empty blob that was really staged is a real (if contentless) addition;
    # only the intent-to-add placeholder is dropped.
    assert [item.path for item in result.files] == oracle
    assert "intent_to_add_excluded:1" in result.degraded


def test_intent_to_add_exclusion_is_visible_in_the_rendered_review(tmp_path: Path) -> None:
    """The count and the reason both reach the reader, not just the packet."""

    from lemoncrow.pro.capabilities.review.render import render_review

    root, _repo, _shas = _linear_repo(tmp_path)
    _write(root, "src/promised.py", "def promised():\n    return 1\n")
    subprocess.run(["git", "add", "-N", "src/promised.py"], cwd=root, check=True)

    packet = build_review_packet(
        root,
        resolve_rev_range(root, staged=True),
        store_root=tmp_path / "store",
        with_impact=False,
        with_provenance=False,
    )
    rendered = render_review(packet, no_color=True)

    assert "intent_to_add_excluded:1" in rendered
    assert "`git add -N` paths hold no staged content" in rendered
    assert "src/promised.py" not in rendered


def test_undetectable_intent_to_add_degrades_instead_of_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pygit2 that stops exposing the flags must cost a signal, not the packet.

    The extended flags are read through pygit2's cffi handle, which is the only
    place libgit2 surfaces them. If a release moves it, the raw-index view comes
    back -- and the packet has to say so, because silently reverting to it is
    exactly the divergence this machinery exists to remove.
    """

    from lemoncrow.pro.capabilities.review import gitdiff

    monkeypatch.setattr(gitdiff, "_intent_to_add_paths", lambda _repo: None)

    root, repo, _shas = _linear_repo(tmp_path)
    _write(root, "src/staged.py", "x = 1\n")
    repo.index.add("src/staged.py")
    repo.index.write()

    result = collect_diff(root, resolve_rev_range(root, staged=True))

    assert [item.path for item in result.files] == ["src/staged.py"]
    assert "intent_to_add_undetermined" in result.degraded


def test_packet_is_whole_with_no_index_and_no_session_store(tmp_path: Path) -> None:
    """Every analysis is wired, and every one of them degrades to a value here.

    This fixture has no code index and no history store, which is the worst case
    all four producers have to survive: the symbol still comes back (tree-sitter),
    the order still explains itself, provenance still answers ``unknown`` with a
    reason, and the shortfalls are named in ``degraded`` rather than raised.
    """

    root, _repo, _shas = _linear_repo(tmp_path)
    rng = resolve_rev_range(root, "HEAD~1")
    packet = build_review_packet(root, rng, store_root=tmp_path / "store")

    assert [symbol.symbol_name for symbol in packet.symbols] == ["one"]
    assert packet.symbols[0].source == "tree_sitter"
    assert packet.symbols[0].caller_count == -1
    assert packet.index_status == "absent"
    assert {"symbol_relations", "centrality"} <= set(packet.degraded)

    assert packet.provenance.status == "unknown"
    assert packet.provenance.host is None
    assert packet.provenance.match_reason != ""
    assert packet.evidence == ()
    assert "provenance_store_missing" in packet.degraded

    assert [entry.path for entry in packet.order] == [item.path for item in packet.files]
    assert packet.order[0].rank == 1
    assert packet.order[0].score > 0.0
    assert packet.order[0].reasons == ("+1 -1",)

    assert packet.stats == {
        "files": 1,
        "additions": 1,
        "deletions": 1,
        "hunks": 1,
        "symbols": 1,
        "impact_sites": 0,
    }


def test_packet_order_respects_limit(tmp_path: Path) -> None:
    root = tmp_path / "limited"
    repo = _init_repo(root)
    _write(root, "seed.txt", "seed\n")
    _commit(repo, "seed", 0)
    for index in range(6):
        _write(root, f"src/mod_{index}.py", f"value = {index}\n")
    _commit(repo, "six modules", 60)

    rng = resolve_rev_range(root, "HEAD~1..HEAD")
    packet = build_review_packet(root, rng, store_root=tmp_path / "store", limit=2)
    assert len(packet.order) == 2
    assert len(packet.files) == 6

    persisted = build_review_packet_with_blobs(root, rng, store_root=tmp_path / "store", limit=2)
    assert len(persisted.packet.order) == 6
    assert {entry.path for entry in persisted.packet.order} == {item.path for item in persisted.packet.files}


def test_hunk_patch_is_empty_by_default_and_populated_on_request(tmp_path: Path) -> None:
    """``lc review`` stays what it was; only a packet being *kept* carries bodies."""

    root, _repo, _shas = _linear_repo(tmp_path)
    rng = resolve_rev_range(root, "HEAD~1")

    default_hunks = [hunk for item in collect_diff(root, rng).files for hunk in item.hunks]
    assert default_hunks
    assert all(hunk.patch == "" for hunk in default_hunks)

    captured = [hunk for item in collect_diff(root, rng, with_patch_text=True).files for hunk in item.hunks]
    assert [hunk.header for hunk in captured] == [hunk.header for hunk in default_hunks]
    assert any("+    return 3" in hunk.patch for hunk in captured)


def test_packet_schema_version_is_two_and_patch_is_the_last_hunk_key(tmp_path: Path) -> None:
    """``--json`` pins keys in declaration order, so ``patch`` may only be last."""

    from lemoncrow.pro.capabilities.review.models import SCHEMA_VERSION

    assert SCHEMA_VERSION == 2

    root, _repo, _shas = _linear_repo(tmp_path)
    rng = resolve_rev_range(root, "HEAD~1")
    payload = build_review_packet(root, rng, store_root=tmp_path / "store", with_patch_text=True).to_dict()
    keys = list(payload["files"][0]["hunks"][0])
    assert keys == [
        "old_start",
        "old_lines",
        "new_start",
        "new_lines",
        "header",
        "added",
        "removed",
        "new_ranges",
        "old_ranges",
        "patch",
    ]
    assert payload["schema_version"] == 2


def test_build_review_packet_forwards_with_patch_text(tmp_path: Path) -> None:
    root, _repo, _shas = _linear_repo(tmp_path)
    rng = resolve_rev_range(root, "HEAD~1")
    off = build_review_packet(root, rng, store_root=tmp_path / "store")
    on = build_review_packet(root, rng, store_root=tmp_path / "store", with_patch_text=True)
    assert all(hunk.patch == "" for item in off.files for hunk in item.hunks)
    assert any(hunk.patch for item in on.files for hunk in item.hunks)


def test_empty_range_yields_empty_packet(tmp_path: Path) -> None:
    root, _repo, _shas = _linear_repo(tmp_path)
    rng = resolve_rev_range(root, "HEAD..HEAD")
    diff = collect_diff(root, rng)
    assert diff.files == ()
    packet = build_review_packet(root, rng, store_root=tmp_path / "store")
    assert packet.files == ()
    assert packet.order == ()
    assert packet.stats["files"] == 0


def _repo_with_submodule(tmp_path: Path) -> tuple[Path, Path]:
    """A parent repo with a committed submodule at ``vendor/``.

    Built with the ``git`` binary for the same reason the intent-to-add fixture
    is: a submodule is three coordinated artifacts (``.gitmodules``, a gitlink
    index entry and a nested checkout with its own ``.git``), and hand-rolling
    them through pygit2 would be testing our fixture rather than libgit2's view
    of a real one.
    """

    def run(args: list[str], cwd: Path) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=Fixture Tester", "-c", "user.email=fixture@example.com", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    sub = tmp_path / "sub"
    sub.mkdir()
    run(["init", "-q"], sub)
    (sub / "lib.py").write_text("value = 1\n", encoding="utf-8")
    run(["add", "-A"], sub)
    run(["commit", "-qm", "sub one"], sub)

    parent = tmp_path / "parent"
    parent.mkdir()
    run(["init", "-q"], parent)
    (parent / "app.py").write_text("import lib\n", encoding="utf-8")
    run(["add", "-A"], parent)
    run(["commit", "-qm", "parent one"], parent)
    run(["-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor"], parent)
    run(["commit", "-qm", "add submodule"], parent)
    return parent, sub


def _bump_submodule(parent: Path, sub: Path) -> tuple[str, str]:
    """Move ``vendor``'s gitlink to a new upstream commit; return ``(before, after)``.

    A genuine pointer bump, made the way a person makes one: commit in the
    submodule, fetch it in the nested checkout, check the new commit out. The
    parent's index still records the old sha until it is added, which is exactly
    the working-tree state a reviewer sees.
    """

    def run(args: list[str], cwd: Path) -> str:
        return subprocess.run(
            ["git", "-c", "user.name=Fixture Tester", "-c", "user.email=fixture@example.com", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    before = run(["rev-parse", "HEAD:vendor"], parent).strip()
    (sub / "lib.py").write_text("value = 2\n", encoding="utf-8")
    run(["add", "-A"], sub)
    run(["commit", "-qm", "sub two"], sub)
    run(["fetch", "-q", "origin"], parent / "vendor")
    after = run(["rev-parse", "FETCH_HEAD"], parent / "vendor").strip()
    run(["checkout", "-q", after], parent / "vendor")
    return before, after


def test_dirty_submodule_workdir_is_not_a_changed_file(tmp_path: Path) -> None:
    """A submodule whose pointer did not move is not a row, and says why.

    libgit2 reports a submodule as modified when the submodule's *own* working
    tree is dirty, and renders the delta as ``-<sha>`` / ``+<sha>-dirty``: a
    ``+1 -1`` row whose two sides are the same commit. Uncommitted work inside
    another repository is not part of this change, and a reviewer offered that
    row can open it, read it, and mark reviewed a change that does not exist.
    ``git diff HEAD --ignore-submodules=dirty`` is the oracle: it is empty here,
    and so is the packet.
    """

    parent, _sub = _repo_with_submodule(tmp_path)
    # Untracked content inside the submodule -- the pointer this repo records is
    # untouched, which is exactly what makes the row a fabrication.
    (parent / "vendor" / "scratch.txt").write_text("work in progress\n", encoding="utf-8")

    oracle = subprocess.run(
        ["git", "diff", "HEAD", "--ignore-submodules=dirty", "--name-only"],
        cwd=parent,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert oracle == [], oracle

    # working_tree=True is the point of this fixture: it is about what the
    # packet says for a dirty submodule in the WORKING TREE. Leaving the mode
    # implicit made it depend on `is_dirty()` calling submodule-internal dirt
    # "dirty", which it no longer does -- that is the whole of finding 8.
    result = collect_diff(parent, resolve_rev_range(parent, working_tree=True))

    assert [item.path for item in result.files] == []
    assert "submodule_dirty:1" in result.degraded


def test_submodule_pointer_bump_stays_visible(tmp_path: Path) -> None:
    """A gitlink that really moved is a reviewable change and must survive.

    This is the half the dirty filter must not eat: two *different* shas either
    side of the gitlink is the repository choosing a new upstream commit, which
    is as reviewable as any dependency bump. It is classified ``config`` rather
    than ``production`` because a pointer has no blob, no symbols and no callers
    -- ranking it as production code would float an unreadable row above real
    files.
    """

    parent, sub = _repo_with_submodule(tmp_path)
    before, after = _bump_submodule(parent, sub)
    assert before != after

    result = collect_diff(parent, resolve_rev_range(parent))

    rows = {item.path: item for item in result.files}
    assert "vendor" in rows, sorted(rows)
    bump = rows["vendor"]
    # A gitlink has no lines; the two commit ids are the change. See
    # `test_gitlink_row_has_no_line_counts_and_no_caller_reason`.
    assert (bump.additions, bump.deletions) == (0, 0)
    assert bump.category == "config"
    assert not any(name.startswith("submodule_dirty") for name in result.degraded)


def test_submodule_pointer_bump_names_its_commits_and_claims_no_unreadable_blob(tmp_path: Path) -> None:
    """A gitlink is a pointer, so it reports a pointer -- never a failed blob read.

    ``load_blobs`` used to try both sides of a gitlink like any other file. Both
    reads necessarily fail (the old side is a commit this repository does not
    contain, the new side is a directory on disk), so a perfectly ordinary
    submodule bump raised ``blob_unreadable`` -- a degraded signal whose only
    published remedy is ``lc code index``, which cannot help: there is no blob to
    read and no index that would ever have held one. Telling a reviewer to
    rebuild an index to recover content that does not exist is worse than saying
    nothing. The row instead carries the two commit ids, which *are* the change,
    and the rendering names them.
    """

    parent, sub = _repo_with_submodule(tmp_path)
    before, after = _bump_submodule(parent, sub)

    # working_tree=True is the point of these three fixtures: they are about
    # what the packet says for a submodule in the WORKING TREE. Leaving it
    # implicit made them depend on `is_dirty()` calling submodule-internal
    # dirt "dirty", which it no longer does (that is the whole of finding 8).
    rng = resolve_rev_range(parent, working_tree=True)
    result = collect_diff(parent, rng)
    bump = next(item for item in result.files if item.path == "vendor")

    # The shape of the change, on the row: which commit, to which commit.
    assert bump.submodule_pointer == (before, after)

    blobs = load_blobs(parent, rng, result.files)
    assert "blob_unreadable" not in blobs.degraded, blobs.degraded
    # No blob was invented either: a pointer has no text side to hand the
    # impact pass, and an empty string masquerading as file content would be its
    # own small lie.
    assert "vendor" not in blobs.new and "vendor" not in blobs.old

    packet = build_review_packet(parent, rng, store_root=tmp_path / "store")
    assert "blob_unreadable" not in packet.degraded, packet.degraded

    from lemoncrow.pro.capabilities.review.render import render_review

    rendered = render_review(packet, show_all=True)
    assert f"submodule {before[:7]} → {after[:7]}" in rendered, rendered


def test_a_submodule_only_change_is_never_headlined_as_zero_lines(tmp_path: Path) -> None:
    """``1 file · +0 -0`` reads as "nothing happened" over a real pointer move.

    The zeros on the row are deliberate and right: a gitlink has no lines, and
    counting git's own ``Subproject commit`` prose as ``+1 -1`` was the defect
    that put text nobody can open into the header. Summing those zeros into the
    range header threw the same honesty away in the other direction -- the first
    line a reviewer reads announced a change of zero lines for a dependency bump
    they will have to judge. It names the pointers instead.
    """

    from lemoncrow.pro.capabilities.review.render import render_review

    parent, sub = _repo_with_submodule(tmp_path)
    before, after = _bump_submodule(parent, sub)
    packet = build_review_packet(parent, resolve_rev_range(parent), store_root=tmp_path / "store")

    rendered = render_review(packet, show_all=True)
    assert "+0 -0" not in rendered, rendered
    assert "1 file · 1 submodule pointer" in rendered, rendered
    # The counts are not suppressed, only the empty ones: what actually moved is
    # still on the row underneath.
    assert f"submodule {before[:7]} → {after[:7]}" in rendered, rendered


def test_gitlink_row_has_no_line_counts_and_no_caller_reason(tmp_path: Path) -> None:
    """A submodule pointer is counted as a pointer, never as one added line.

    git renders a pointer bump as a two-line patch -- ``-Subproject commit
    <old>`` / ``+Subproject commit <new>`` -- and reading those two lines as
    ``+1 -1`` charged git's own prose to the change. The range header a reviewer
    reads first said ``1 file · +1 -1`` for a change containing no line anyone
    can open, the JSON carried the same fiction in ``additions``/``deletions``
    and ``stats``, and because the row then held a hunk, the symbol pass had a
    line range to anchor on and could hang a ``N known callers`` reason off a
    pointer -- a claim about code the delta does not contain.

    So the row states what actually changed (old sha → new sha, on
    ``submodule_pointer`` and in the rendering) and nothing else: no hunks, no
    counts, no caller or impact reason. The suppressed half -- a submodule that
    is merely dirty -- must keep contributing nothing at all.
    """

    parent, sub = _repo_with_submodule(tmp_path)
    before, after = _bump_submodule(parent, sub)

    # working_tree=True is the point of these three fixtures: they are about
    # what the packet says for a submodule in the WORKING TREE. Leaving it
    # implicit made them depend on `is_dirty()` calling submodule-internal
    # dirt "dirty", which it no longer does (that is the whole of finding 8).
    rng = resolve_rev_range(parent, working_tree=True)
    result = collect_diff(parent, rng)
    bump = next(item for item in result.files if item.path == "vendor")

    # A pointer has no lines, so it has no hunks and no line counts.
    assert bump.hunks == ()
    assert (bump.additions, bump.deletions) == (0, 0)
    # ... and "there is a delta but no patch to show" is the wrong story for a
    # row that says exactly what it changed.
    assert "mode_only" not in result.degraded, result.degraded

    packet = build_review_packet(parent, rng, store_root=tmp_path / "store")
    assert packet.stats["files"] == 1
    assert packet.stats["additions"] == 0
    assert packet.stats["deletions"] == 0
    assert packet.stats["hunks"] == 0

    payload = packet.to_dict()
    row = next(item for item in payload["files"] if item["path"] == "vendor")
    assert row["additions"] == 0 and row["deletions"] == 0
    assert row["submodule_pointer"] == [before, after] or row["submodule_pointer"] == (before, after)

    # No reason may talk about callers or impacted sites: both are statements
    # about source, and there is none behind a gitlink.
    reasons = tuple(reason for entry in packet.order if entry.path == "vendor" for reason in entry.reasons)
    assert not any("caller" in reason or "impacted site" in reason for reason in reasons), reasons
    assert not any(symbol.file_path == "vendor" for symbol in packet.symbols)

    from lemoncrow.pro.capabilities.review.render import render_review

    rendered = render_review(packet, show_all=True)
    assert "+1 -1" not in rendered, rendered
    assert f"submodule {before[:7]} → {after[:7]}" in rendered, rendered

    # The other half: a submodule that only has a dirty working tree is not a
    # row, so it must not move any total either.
    (tmp_path / "dirty").mkdir()
    dirty_parent, _dirty_sub = _repo_with_submodule(tmp_path / "dirty")
    (dirty_parent / "vendor" / "scratch.txt").write_text("work in progress\n", encoding="utf-8")
    dirty_packet = build_review_packet(
        dirty_parent, resolve_rev_range(dirty_parent, working_tree=True), store_root=tmp_path / "dirty-store"
    )
    assert dirty_packet.stats["files"] == 0
    assert dirty_packet.stats["additions"] == 0
    assert dirty_packet.stats["deletions"] == 0


def test_dirty_submodule_reports_only_the_dirty_signal(tmp_path: Path) -> None:
    """The suppressed half must not leak an unreadable-blob claim either.

    The dirty submodule is dropped from the file list, so nothing should try to
    read it -- but a regression that reinstated the blob read would put
    ``blob_unreadable`` back in the packet for a repository whose ``git diff
    HEAD --ignore-submodules=dirty`` is empty. ``submodule_dirty`` is the one
    signal this state may raise, and its remedy (commit or discard the work
    inside the submodule) is one the reviewer can actually act on.
    """

    parent, _sub = _repo_with_submodule(tmp_path)
    (parent / "vendor" / "scratch.txt").write_text("work in progress\n", encoding="utf-8")

    # working_tree=True is the point of these three fixtures: they are about
    # what the packet says for a submodule in the WORKING TREE. Leaving it
    # implicit made them depend on `is_dirty()` calling submodule-internal
    # dirt "dirty", which it no longer does (that is the whole of finding 8).
    rng = resolve_rev_range(parent, working_tree=True)
    result = collect_diff(parent, rng)
    assert "submodule_dirty:1" in result.degraded

    blobs = load_blobs(parent, rng, result.files)
    assert "blob_unreadable" not in blobs.degraded, blobs.degraded

    packet = build_review_packet(parent, rng, store_root=tmp_path / "store")
    assert "blob_unreadable" not in packet.degraded, packet.degraded


def _submodule_discount_fixture(tmp_path: Path) -> Path:
    """A three-commit parent repo whose only dirt lives inside its submodule.

    Three commits, not the fixture's two, so ``HEAD~2..HEAD~1`` exists: an
    explicit ``--base/--head`` pair that names a range the fallback would never
    have picked is the control for the reopen case below.
    """

    parent, _sub = _repo_with_submodule(tmp_path)
    (parent / "app.py").write_text("import lib\n\nvalue = 2\n", encoding="utf-8")
    for args in (["add", "-A"], ["commit", "-qm", "parent two"]):
        subprocess.run(
            ["git", "-c", "user.name=Fixture Tester", "-c", "user.email=fixture@example.com", *args],
            cwd=parent,
            check=True,
            capture_output=True,
            text=True,
        )
    # Untracked content inside the submodule: `git status` reports `vendor`, the
    # gitlink this repo records is untouched, and `is_dirty` discounts it.
    (parent / "vendor" / "scratch.txt").write_text("work in progress\n", encoding="utf-8")
    return parent


def _reopened(parent: Path, rng: RevRange) -> RevRange:
    """The same range as the reader refetches it: stored, then re-resolved.

    ``source_ref`` + ``range_for_session`` is exactly what reopening a session
    and ``POST /api/reviews/{id}/refresh`` do, so every branch below is pinned
    together with this round trip. The property is one property: what the CLI
    said about the submodule is what the reader says about it, or the reviewer
    watches a signal appear or vanish over a range that never moved.
    """

    session = ReviewSession(
        id="rev-fixture",
        subject_type="commit_range" if rng.mode == "commit_range" else "local_change",
        repo_root=str(parent.resolve()),
        range_mode=rng.mode,
        source_ref=source_ref(rng, repo_root=parent),
    )
    return range_for_session(parent, session)


def _submodule_signals(parent: Path, rng: RevRange) -> list[str]:
    return [signal for signal in collect_diff(parent, rng).degraded if signal.startswith("submodule_dirty")]


def test_the_staged_range_never_carries_the_submodule_discount(tmp_path: Path) -> None:
    """``--staged`` substitutes nothing, and the index cannot hold a submodule's dirt.

    Raising ``submodule_dirty`` here would tell a reviewer something was
    withheld from a diff that withheld nothing, and every degraded signal a
    reviewer learns to ignore costs the ones that are real. A staged session
    also reopens by ``range_mode``, never by SHA, so the branch is re-entered
    as itself and cannot disagree with the CLI later.
    """

    parent = _submodule_discount_fixture(tmp_path)
    staged = resolve_rev_range(parent, staged=True)
    assert staged.submodule_dirt_discounted == 0
    assert _submodule_signals(parent, staged) == []

    reopened = _reopened(parent, staged)
    assert reopened.mode == "staged"
    assert reopened.submodule_dirt_discounted == 0
    assert _submodule_signals(parent, reopened) == []


def test_the_working_tree_range_owes_its_signal_to_the_skip_not_the_discount(tmp_path: Path) -> None:
    """``--working-tree`` discloses the submodule, but not as a range-picking note.

    The stamp stays 0 because nothing was substituted; the signal is still
    raised, by ``skipped_submodule_dirty``, because a working-tree diff really
    does drop the gitlink. Keeping the two apart is what lets the stamp mean
    "the range you are reading is not the range you asked for".
    """

    parent = _submodule_discount_fixture(tmp_path)
    working = resolve_rev_range(parent, working_tree=True)
    assert working.submodule_dirt_discounted == 0
    assert _submodule_signals(parent, working) == ["submodule_dirty:1"]

    reopened = _reopened(parent, working)
    assert reopened.mode == "working_tree"
    assert reopened.submodule_dirt_discounted == 0
    assert _submodule_signals(parent, reopened) == ["submodule_dirty:1"]


def test_the_no_argument_fallback_carries_the_submodule_discount(tmp_path: Path) -> None:
    """The range the discount chose, and the state the disclosure exists for.

    A bare ``lc review`` over this fixture reads the tree as clean only because
    the submodule's own dirt was set aside, then reviews the last commit
    instead -- a header that contradicts ``git status`` unless the packet says
    what was set aside.
    """

    parent = _submodule_discount_fixture(tmp_path)
    fallback = resolve_rev_range(parent)
    assert (fallback.mode, fallback.base_rev, fallback.head_rev) == ("commit_range", "HEAD~1", "HEAD")
    assert fallback.submodule_dirt_discounted == 1
    assert _submodule_signals(parent, fallback) == ["submodule_dirty:1"]
    assert _reopened(parent, fallback).submodule_dirt_discounted == 1


def test_a_two_dot_range_spelling_the_fallback_carries_the_same_discount(tmp_path: Path) -> None:
    """``HEAD~1..HEAD`` is the fallback, spelled out, and must read the same.

    ``source_ref`` only keys a session by name when *both* endpoints are proven
    canonical refs, which ``HEAD~1..HEAD`` is not: it stores the same
    ``<parent>..<head>`` SHA pair the bare fallback stores, into the same
    ReviewSession. Nothing downstream can tell which words opened it, so
    silence here does not keep the signal quiet -- it schedules a
    contradiction, because the reopen re-resolves those SHAs through
    ``--base/--head``, recognises the fallback and raises the very signal the
    CLI withheld.
    """

    parent = _submodule_discount_fixture(tmp_path)
    fallback = resolve_rev_range(parent)
    spelled = resolve_rev_range(parent, "HEAD~1..HEAD")
    assert (spelled.base_sha, spelled.head_sha) == (fallback.base_sha, fallback.head_sha)
    assert source_ref(spelled, repo_root=parent) == source_ref(fallback, repo_root=parent)
    assert spelled.submodule_dirt_discounted == 1
    assert _submodule_signals(parent, spelled) == ["submodule_dirty:1"]
    assert _reopened(parent, spelled).submodule_dirt_discounted == 1
    packet = build_review_packet(parent, spelled, store_root=tmp_path / "two-dot")
    assert "submodule_dirty:1" in packet.degraded, packet.degraded

    # A commit range the discount never chose stays silent on both sides.
    unrelated = resolve_rev_range(parent, "HEAD~2..HEAD~1")
    assert unrelated.submodule_dirt_discounted == 0
    assert _submodule_signals(parent, unrelated) == []
    assert _reopened(parent, unrelated).submodule_dirt_discounted == 0


def test_a_three_dot_range_spelling_the_fallback_carries_the_same_discount(tmp_path: Path) -> None:
    """``HEAD~1...HEAD`` stores the merge base, which is HEAD's first parent.

    So it too persists ``<parent>..<head>`` and reopens through
    ``--base/--head``. The three-dot spelling is pinned separately from the
    two-dot one because it reaches that stored key by a different route --
    ``_merge_base_sha`` rather than the literal left endpoint -- and a fix that
    only recognised the literal one would leave this branch contradicting its
    own reopen.
    """

    parent = _submodule_discount_fixture(tmp_path)
    fallback = resolve_rev_range(parent)
    spelled = resolve_rev_range(parent, "HEAD~1...HEAD")
    assert spelled.merge_base_sha == fallback.base_sha, "the merge base is HEAD's first parent here"
    assert source_ref(spelled, repo_root=parent) == source_ref(fallback, repo_root=parent)
    assert spelled.submodule_dirt_discounted == 1
    assert _submodule_signals(parent, spelled) == ["submodule_dirty:1"]
    assert _reopened(parent, spelled).submodule_dirt_discounted == 1

    unrelated = resolve_rev_range(parent, "HEAD~2...HEAD~1")
    assert unrelated.submodule_dirt_discounted == 0
    assert _submodule_signals(parent, unrelated) == []
    assert _reopened(parent, unrelated).submodule_dirt_discounted == 0


def test_a_single_rev_resolving_to_the_fallback_carries_the_same_discount(tmp_path: Path) -> None:
    """``lc review HEAD~1`` is the fallback range under a third spelling.

    A single spec is merge-based against HEAD, so ``HEAD~1`` resolves to
    ``<parent>..<head>`` and shares the fallback's session too. ``HEAD~2`` is
    the control: a range the discount never chose, silent on both sides.
    """

    parent = _submodule_discount_fixture(tmp_path)
    fallback = resolve_rev_range(parent)
    spelled = resolve_rev_range(parent, "HEAD~1")
    assert (spelled.base_sha, spelled.head_sha) == (fallback.base_sha, fallback.head_sha)
    assert source_ref(spelled, repo_root=parent) == source_ref(fallback, repo_root=parent)
    assert spelled.submodule_dirt_discounted == 1
    assert _submodule_signals(parent, spelled) == ["submodule_dirty:1"]
    assert _reopened(parent, spelled).submodule_dirt_discounted == 1

    unrelated = resolve_rev_range(parent, "HEAD~2")
    assert unrelated.submodule_dirt_discounted == 0
    assert _submodule_signals(parent, unrelated) == []
    assert _reopened(parent, unrelated).submodule_dirt_discounted == 0


def test_an_explicit_pair_that_is_not_the_fallback_carries_nothing(tmp_path: Path) -> None:
    """The ``--base/--head`` branch recognises a shape, not the flag that used it.

    ``HEAD~2..HEAD~1`` is a range the fallback would never have picked, so the
    branch that has to re-recognise the fallback for every reopen must not
    stamp this one -- the recognition is (first parent, HEAD, clean tree), and
    this pair fails it.
    """

    parent = _submodule_discount_fixture(tmp_path)
    unrelated = resolve_rev_range(parent, base="HEAD~2", head="HEAD~1")
    assert unrelated.submodule_dirt_discounted == 0
    assert _submodule_signals(parent, unrelated) == []
    assert _reopened(parent, unrelated).submodule_dirt_discounted == 0


def test_reopening_the_fallback_range_keeps_the_submodule_disclosure(tmp_path: Path) -> None:
    """The disclosure must not vanish when the reader refetches the same range.

    A bare ``lc review --track`` resolves the fallback, and ``source_ref`` stores
    it as two resolved SHAs -- no named ref survives ``HEAD~1..HEAD``. So every
    reopen and every ``POST /api/reviews/{id}/refresh`` re-resolves that range
    through ``resolve_rev_range(base=..., head=...)``, the explicit branch. If
    that branch cannot recognise the fallback it was handed, the reviewer sees
    ``submodule_dirty:1`` in the CLI packet and watches "Uncertain signals"
    empty itself on Refresh, over a range that has not moved.
    """

    parent = _submodule_discount_fixture(tmp_path)
    fallback = resolve_rev_range(parent)
    assert fallback.submodule_dirt_discounted == 1, "fixture no longer exercises the discount"

    session = ReviewSession(
        id="rev-fixture",
        subject_type="commit_range",
        repo_root=str(parent.resolve()),
        range_mode=fallback.mode,
        source_ref=source_ref(fallback, repo_root=parent),
    )
    assert session.source_ref == f"{fallback.base_sha}..{fallback.head_sha}", session.source_ref

    reopened = range_for_session(parent, session)
    assert (reopened.base_sha, reopened.head_sha) == (fallback.base_sha, fallback.head_sha)
    assert reopened.submodule_dirt_discounted == 1
    assert "submodule_dirty:1" in collect_diff(parent, reopened).degraded
    packet = build_review_packet(parent, reopened, store_root=tmp_path / "reopened")
    assert "submodule_dirty:1" in packet.degraded, packet.degraded
