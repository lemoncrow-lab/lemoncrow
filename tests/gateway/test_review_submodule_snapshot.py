from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from lemoncrow.pro.capabilities.review import snapshot
from lemoncrow.pro.capabilities.review.api import _frozen_nested_web_preview_payload
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range, source_state
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.snapshot import frozen_submodule_changed_paths, materialize_review_side
from lemoncrow.pro.capabilities.review.sources.local import open_or_create_session, snapshot_revision
from lemoncrow.pro.capabilities.review.store import ReviewStore
from lemoncrow.pro.capabilities.review.surface_providers import WebSurfaceProvider
from lemoncrow.pro.capabilities.review.surfaces import SurfaceContext, load_review_surface_config


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.test", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _nested_astro_repo(tmp_path: Path) -> tuple[Path, Path]:
    sub = tmp_path / "landing-source"
    sub.mkdir()
    _git(sub, "init", "--quiet", "--initial-branch=main", ".")
    _write(sub, "package.json", '{"scripts":{"build":"astro build"},"dependencies":{"astro":"5"}}\n')
    _write(sub, "src/pages/index.astro", "<h1>before</h1>\n")
    _git(sub, "add", "-A")
    _git(sub, "commit", "--quiet", "-m", "landing base")

    parent = tmp_path / "parent"
    parent.mkdir()
    _git(parent, "init", "--quiet", "--initial-branch=main", ".")
    _write(parent, "README.md", "parent\n")
    _write(
        parent,
        ".lemoncrow/review.yaml",
        "version: 1\n"
        "surfaces:\n"
        "  - id: landing\n"
        "    provider: web\n"
        "    root: landing\n"
        "    framework: astro\n"
        "    routes: auto\n",
    )
    _git(parent, "add", "-A")
    _git(parent, "commit", "--quiet", "-m", "parent base")
    _git(parent, "-c", "protocol.file.allow=always", "submodule", "add", "--quiet", str(sub), "landing")
    _git(parent, "commit", "--quiet", "-am", "add landing")
    return parent, parent / "landing"


def test_new_side_source_tree_read_tolerates_pre_side_review_store() -> None:
    class LegacyStore:
        def read_source_tree_artifact(self, review_id: str, revision_id: str):
            assert review_id == "review-1"
            assert revision_id == "revision-1"
            return {"app.py": {"content_digest": "abc", "mode": 0o100644, "size": 3}}

    store = LegacyStore()
    assert snapshot._read_source_tree_artifact(store, "review-1", "revision-1", side="new") == {
        "app.py": {"content_digest": "abc", "mode": 0o100644, "size": 3}
    }
    with pytest.raises(TypeError, match="unexpected keyword argument 'side'"):
        snapshot._read_source_tree_artifact(store, "review-1", "revision-1", side="old")


def test_dirty_web_submodule_is_frozen_as_nested_revision_and_discovered_as_surface(tmp_path: Path) -> None:
    parent, landing = _nested_astro_repo(tmp_path)
    _write(landing, "src/pages/index.astro", "<h1>after</h1>\n")

    rng = resolve_rev_range(parent)
    assert rng.mode == "working_tree"
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    session = open_or_create_session(store, parent, rng)
    build = build_review_packet_with_blobs(
        parent,
        rng,
        store_root=store_root,
        with_impact=False,
        with_provenance=False,
        with_patch_text=True,
    )
    revision = snapshot_revision(store, session, parent, rng, store_root=store_root, build=build)

    assert frozen_submodule_changed_paths(store_root, revision) == ("landing/src/pages/index.astro",)
    assert "submodule_dirty:1" in revision.degraded

    context = SurfaceContext(
        parent,
        store_root,
        revision,
        ("landing",),
        load_review_surface_config(parent),
    )
    surfaces = WebSurfaceProvider().discover(context)
    landing_surface = next(surface for surface in surfaces if surface.id == "landing:/")
    assert landing_surface.affected_paths == ("landing/src/pages/index.astro",)
    assert landing_surface.metadata == {"framework": "astro", "root": "landing", "surface_id": "landing"}
    nested_preview = _frozen_nested_web_preview_payload(store, revision, parent, "landing/src/pages/index.astro")
    assert nested_preview is not None
    assert nested_preview["root"] == "landing"
    assert nested_preview["routes"] == ["/"]

    materialized = tmp_path / "materialized"
    materialize_review_side(store_root, parent, revision, side="old", target=materialized, scope="landing")
    assert (materialized / "landing/src/pages/index.astro").read_text() == "<h1>before</h1>\n"
    materialize_review_side(store_root, parent, revision, side="new", target=materialized, scope="landing")
    assert (materialized / "landing/src/pages/index.astro").read_text() == "<h1>after</h1>\n"

    # The review is frozen: later writes inside the submodule move source_state,
    # but materializing this revision still returns the bytes captured above.
    before_state = revision.source_fingerprint
    _write(landing, "src/pages/index.astro", "<h1>later</h1>\n")
    assert source_state(parent, resolve_rev_range(parent, working_tree=True)).fingerprint != before_state
    materialize_review_side(store_root, parent, revision, side="new", target=materialized, scope="landing")
    assert (materialized / "landing/src/pages/index.astro").read_text() == "<h1>after</h1>\n"


def test_scoped_git_materialization_does_not_walk_unrelated_trees(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "--quiet", "--initial-branch=main", ".")
    _write(repo, "frontend/src/page.tsx", "export default 1\n")
    _write(repo, "backend/deep/unused.py", "print('unused')\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "--quiet", "-m", "fixture")
    sha = _git(repo, "rev-parse", "HEAD")

    prefixes: list[str] = []
    original = snapshot._walk_tree

    def observed(repo_obj, tree_obj, prefix: str = ""):
        prefixes.append(prefix)
        yield from original(repo_obj, tree_obj, prefix)

    monkeypatch.setattr(snapshot, "_walk_tree", observed)
    target = tmp_path / "materialized"
    snapshot.materialize_git_revision(repo, sha, target, scope="frontend")

    assert (target / "frontend/src/page.tsx").read_text() == "export default 1\n"
    assert not (target / "backend").exists()
    assert prefixes and prefixes[0] == "frontend"
    assert "" not in prefixes


def test_clean_submodule_commit_range_materializes_exact_old_and_new_gitlinks(tmp_path: Path) -> None:
    parent, landing = _nested_astro_repo(tmp_path)
    base_parent = _git(parent, "rev-parse", "HEAD")
    _write(landing, "src/pages/index.astro", "<h1>clean-after</h1>\n")
    _git(landing, "add", "-A")
    _git(landing, "commit", "--quiet", "-m", "landing update")
    _git(parent, "add", "landing")
    _git(parent, "commit", "--quiet", "-m", "advance landing")
    head_parent = _git(parent, "rev-parse", "HEAD")

    revision = type(
        "Revision",
        (),
        {
            "id": "revision",
            "review_id": "review",
            "range_mode": "commit_range",
            "base_sha": base_parent,
            "head_sha": head_parent,
            "source_fingerprint": "",
            "tree_fingerprint": "",
            "packet_path": "",
        },
    )()
    store_root = tmp_path / "store"
    materialized = tmp_path / "materialized-clean"

    materialize_review_side(store_root, parent, revision, side="old", target=materialized, scope="landing")
    assert (materialized / "landing/src/pages/index.astro").read_text() == "<h1>before</h1>\n"
    materialize_review_side(store_root, parent, revision, side="new", target=materialized, scope="landing")
    assert (materialized / "landing/src/pages/index.astro").read_text() == "<h1>clean-after</h1>\n"

    context = SurfaceContext(
        parent,
        store_root,
        revision,
        ("landing",),
        load_review_surface_config(parent),
    )
    surfaces = WebSurfaceProvider().discover(context)
    landing_surface = next(surface for surface in surfaces if surface.id == "landing:/")
    assert landing_surface.affected_paths == ("landing/src/pages/index.astro",)


def test_working_tree_gitlink_bump_does_not_invalidate_parent_runtime_snapshot(tmp_path: Path) -> None:
    parent, landing = _nested_astro_repo(tmp_path)
    _write(landing, "src/pages/index.astro", "<h1>pointer-after</h1>\n")
    _git(landing, "add", "-A")
    _git(landing, "commit", "--quiet", "-m", "landing pointer update")

    rng = resolve_rev_range(parent)
    assert rng.mode == "working_tree"
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    session = open_or_create_session(store, parent, rng)
    build = build_review_packet_with_blobs(
        parent,
        rng,
        store_root=store_root,
        with_impact=False,
        with_provenance=False,
        with_patch_text=True,
    )
    revision = snapshot_revision(store, session, parent, rng, store_root=store_root, build=build)

    overlay = snapshot.frozen_revision_overlay(store_root, revision, scope=".")
    assert overlay == {}

    materialized = tmp_path / "materialized-parent"
    materialize_review_side(store_root, parent, revision, side="new", target=materialized)
    assert (materialized / "README.md").read_text() == "parent\n"
    assert not (materialized / "landing").exists()

    # A runtime explicitly scoped to the submodule still gets the exact new pointer.
    materialize_review_side(store_root, parent, revision, side="new", target=materialized, scope="landing")
