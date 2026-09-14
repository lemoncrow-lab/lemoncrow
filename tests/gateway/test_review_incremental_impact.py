from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pygit2
import pytest

import lemoncrow.pro.capabilities.review.impact as impact_module
from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.packet import build_review_packet_with_blobs
from lemoncrow.pro.capabilities.review.sources.local import (
    open_or_create_session,
    read_packet_json,
    refresh,
    snapshot_revision,
)
from lemoncrow.pro.capabilities.review.store import ReviewStore


def _init_repo(root: Path) -> Any:
    root.mkdir(parents=True, exist_ok=True)
    repo = pygit2.init_repository(str(root), initial_head="main")
    repo.config["user.name"] = "Fixture Tester"
    repo.config["user.email"] = "fixture@example.invalid"
    return repo


def _write(root: Path, path: str, text: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _commit(repo: Any, message: str) -> None:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    signature = pygit2.Signature("Fixture Tester", "fixture@example.invalid", 1700000000, 0)
    repo.create_commit("HEAD", signature, signature, message, tree, parents)


def _capture(store: ReviewStore, repo_root: Path, store_root: Path) -> tuple[Any, Any]:
    rng = resolve_rev_range(repo_root, working_tree=True)
    session = open_or_create_session(store, repo_root, rng, title="incremental")
    revision = snapshot_revision(store, session, repo_root, rng, store_root=store_root)
    return session, revision


def _semantic(packet: dict[str, Any]) -> dict[str, Any]:
    return {
        key: packet.get(key) for key in ("files", "symbols", "impact", "order", "index_status", "degraded", "stats")
    }


def test_refresh_reuses_stable_detector_sources_but_matches_a_full_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "repo"
    repo = _init_repo(repo_root)
    _write(repo_root, "a.py", 'def a():\n    return "legacy-a"\n')
    _write(repo_root, "b.py", "def b():\n    return 1\n")
    _write(repo_root, "consumer.py", 'TOKEN = "legacy-a"\n')
    _commit(repo, "base")

    _write(repo_root, "a.py", 'def a():\n    return "modern-a"\n')
    _write(repo_root, "b.py", "def b():\n    return 2\n")
    store_root = tmp_path / "store"
    store = ReviewStore(store_root)

    # Keep the detector-completeness precondition stable even on a machine that
    # does not have ast-grep installed. The detector itself still has its text
    # fallback; this only avoids declaring the prior result uncacheable.
    monkeypatch.setattr(impact_module, "_astgrep_ready", lambda _root: True)
    session, _ = _capture(store, repo_root, store_root)

    calls: list[tuple[str, ...]] = []
    original = impact_module._detector_sites

    def observed(*args: Any, **kwargs: Any) -> Any:
        files = args[1]
        calls.append(tuple(item.path for item in files))
        return original(*args, **kwargs)

    monkeypatch.setattr(impact_module, "_detector_sites", observed)
    _write(repo_root, "b.py", "def b():\n    return 3\n")
    rng = resolve_rev_range(repo_root, working_tree=True)
    result = refresh(store, session, repo_root, store_root=store_root, rng=rng)

    assert result.created is True
    assert calls == [("b.py",)], "a.py's unchanged source edit should not pay the detector scan again"

    incremental = read_packet_json(store, result.revision)
    assert incremental is not None
    full = build_review_packet_with_blobs(
        repo_root,
        rng,
        store_root=store_root,
        with_patch_text=True,
        unbounded_patch_text=True,
    ).packet.to_dict()
    full_json = json.loads(json.dumps(full))
    assert _semantic(incremental) == _semantic(full_json)


def test_a_path_leaving_the_patch_invalidates_detector_reuse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo_root = tmp_path / "repo"
    repo = _init_repo(repo_root)
    for name in ("a", "b", "c"):
        _write(repo_root, f"{name}.py", f"def {name}():\n    return 1\n")
    _commit(repo, "base")
    for name in ("a", "b", "c"):
        _write(repo_root, f"{name}.py", f"def {name}():\n    return 2\n")

    store_root = tmp_path / "store"
    store = ReviewStore(store_root)
    monkeypatch.setattr(impact_module, "_astgrep_ready", lambda _root: True)
    session, _ = _capture(store, repo_root, store_root)

    calls: list[tuple[str, ...]] = []
    original = impact_module._detector_sites

    def observed(*args: Any, **kwargs: Any) -> Any:
        files = args[1]
        calls.append(tuple(item.path for item in files))
        return original(*args, **kwargs)

    monkeypatch.setattr(impact_module, "_detector_sites", observed)
    # a.py leaves the patch entirely; c.py itself is unchanged since the first
    # review. A safe implementation must still re-run c.py because the search
    # corpus just gained a.py as a possible outside consumer.
    _write(repo_root, "a.py", "def a():\n    return 1\n")
    _write(repo_root, "b.py", "def b():\n    return 3\n")
    refresh(store, session, repo_root, store_root=store_root, rng=resolve_rev_range(repo_root, working_tree=True))

    assert calls == [("b.py", "c.py")]
