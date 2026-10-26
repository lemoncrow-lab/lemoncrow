"""Linked worktrees are selected by paths and stay sticky per thin-client session."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from _stub import StubServer
from conftest import make_config, write_tree
from lemoncrow_client.config import ClientConfig
from lemoncrow_client.dispatcher import Dispatcher
from lemoncrow_client.errors import ClientError
from lemoncrow_client.session import RemoteSession
from lemoncrow_client.worktrees import WorktreeCatalog


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=cwd,
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path]:
    """A real repository with one linked worktree nested under .lc-worktrees."""
    main = (tmp_path / "main").resolve()
    main.mkdir()
    write_tree(main, {"pkg/alpha.py": b"def alpha():\n    return 1\n", "README.md": b"# main\n"})
    _git(main, "init", "-b", "main")
    _git(main, "add", "-A")
    _git(main, "commit", "-m", "init")
    linked = main / ".lc-worktrees" / "feature-x"
    _git(main, "worktree", "add", "-b", "feat/x", str(linked))
    return main, linked.resolve()


@pytest.fixture
def catalog(repos: tuple[Path, Path]) -> WorktreeCatalog:
    return WorktreeCatalog(repos[0])


def test_paths_inside_a_worktree_route_and_rebase(catalog: WorktreeCatalog, repos: tuple[Path, Path]) -> None:
    _, linked = repos
    routed = catalog.route(
        {
            "query": "alpha",
            "paths": [".lc-worktrees/feature-x/pkg", str(linked / "README.md")],
            "cwd": str(linked),
            "files": [f"{linked}/pkg/alpha.py:L1-L2", {"path": str(linked / "README.md")}],
            "edits": [{"path": f"{linked}/pkg/alpha.py:L1-L2", "new": "x"}],
        }
    )
    assert routed.worktree is not None and routed.worktree.root == linked
    assert routed.explicit and not routed.sticky
    assert routed.arguments == {
        "query": "alpha",
        "paths": ["pkg", "README.md"],
        "cwd": ".",
        "files": ["pkg/alpha.py:L1-L2", {"path": "README.md"}],
        "edits": [{"path": "pkg/alpha.py:L1-L2", "new": "x"}],
    }


@pytest.mark.parametrize("nested", [False, True])
def test_worktree_created_after_catalog_cache_is_immediately_routable(
    catalog: WorktreeCatalog, repos: tuple[Path, Path], nested: bool
) -> None:
    main, _ = repos
    assert len(catalog.entries()) == 2  # Prime the cache before creation.
    new = (main / ".lc-worktrees" / "new-feature") if nested else (main.parent / "new-feature")
    _git(main, "worktree", "add", "--detach", str(new), "HEAD")

    routed = catalog.route({"edits": [{"path": str(new / "pkg/alpha.py"), "old": "return 1", "new": "return 2"}]})
    assert routed.worktree is not None and routed.worktree.root == new.resolve()
    assert routed.arguments["edits"][0]["path"] == "pkg/alpha.py"
    assert routed.explicit


def test_project_id_is_not_a_worktree_selector(catalog: WorktreeCatalog) -> None:
    routed = catalog.route({"project_id": "feature-x", "query": "q"})
    assert routed.worktree is None
    assert not routed.explicit and not routed.sticky
    assert routed.arguments == {"project_id": "feature-x", "query": "q"}


def test_active_worktree_handles_relative_paths_and_explicit_main_switch(
    catalog: WorktreeCatalog, repos: tuple[Path, Path]
) -> None:
    main, linked = repos
    selected = catalog.route({"paths": str(linked / "pkg")})
    assert selected.worktree is not None

    sticky = catalog.route({"query": "alpha"}, active=selected.worktree)
    assert sticky.worktree == selected.worktree and sticky.sticky and not sticky.explicit

    relative = catalog.route({"files": ["pkg/alpha.py"]}, active=selected.worktree)
    assert relative.worktree == selected.worktree and relative.explicit and not relative.sticky
    assert relative.arguments["files"] == ["pkg/alpha.py"]

    outside = catalog.route({"cwd": "/tmp"}, active=selected.worktree)
    assert outside.worktree == selected.worktree and outside.sticky and not outside.explicit

    back_to_main = catalog.route({"cwd": str(main)}, active=selected.worktree)
    assert back_to_main.worktree is None and back_to_main.explicit and not back_to_main.sticky
    assert back_to_main.arguments["cwd"] == "."


def test_spanning_registered_checkouts_is_refused(catalog: WorktreeCatalog, repos: tuple[Path, Path]) -> None:
    main, linked = repos
    with pytest.raises(ClientError, match="spans several"):
        catalog.route({"paths": [str(main / "pkg"), str(linked / "pkg")]})


@pytest.fixture
def routed_dispatcher(
    stub: StubServer, repos: tuple[Path, Path], state_dir: Path
) -> Iterator[tuple[Dispatcher, ClientConfig]]:
    config = make_config(url=stub.url, worktree=repos[0], state_dir=state_dir)
    session = RemoteSession(config)
    assert session.bootstrap(deadline_s=10.0).ok
    dispatcher = Dispatcher(config, session)
    yield dispatcher, config
    dispatcher.close_worktrees()
    session.close()


def test_dispatcher_sticks_to_worktree_then_switches_back_to_main(
    stub: StubServer, routed_dispatcher: tuple[Dispatcher, ClientConfig], repos: tuple[Path, Path]
) -> None:
    dispatcher, _ = routed_dispatcher
    main, linked = repos

    here = dispatcher.call("code_search", {"query": "alpha"})
    assert not here.is_error and here.view_id == ""

    there = dispatcher.call("code_search", {"query": "alpha", "paths": str(linked / "pkg")})
    assert not there.is_error, there.content
    assert there.view_id and there.session_id
    name, arguments, _bound = stub.state.tool_calls[-1]
    assert name == "code_search" and arguments["paths"] == "pkg"

    sticky = dispatcher.call("code_search", {"query": "alpha"})
    assert not sticky.is_error and sticky.view_id == there.view_id and sticky.session_id == there.session_id
    assert sticky.content[0]["text"].startswith("[worktree feature-x]\n")

    relative = dispatcher.call("bash", {"command": "pwd", "cwd": "."})
    assert not relative.is_error
    assert str(linked) in relative.content[0]["text"]

    switched = dispatcher.call("bash", {"command": "pwd", "cwd": str(main)})
    assert not switched.is_error and str(main) in switched.content[0]["text"]

    back_here = dispatcher.call("code_search", {"query": "alpha"})
    assert not back_here.is_error and back_here.view_id == ""
    assert not back_here.content[0]["text"].startswith("[worktree ")


def test_dispatcher_edits_a_just_created_external_worktree_without_touching_main(
    routed_dispatcher: tuple[Dispatcher, ClientConfig], repos: tuple[Path, Path]
) -> None:
    dispatcher, _ = routed_dispatcher
    main, _ = repos
    assert len(dispatcher._catalog.entries()) == 2  # The creation was not cached.
    fresh = main.parent / "just-created-checkout"
    _git(main, "worktree", "add", "--detach", str(fresh), "HEAD")

    edited = dispatcher.call(
        "edit", {"edits": [{"path": str(fresh / "pkg/alpha.py"), "old": "return 1", "new": "return 42"}]}
    )
    assert not edited.is_error, edited.content
    assert "return 42" in (fresh / "pkg/alpha.py").read_text(encoding="utf-8")
    assert "return 1" in (main / "pkg/alpha.py").read_text(encoding="utf-8")
    assert edited.session_id and edited.view_id

    # The selected checkout remains active for relative edits/read calls.
    relative = dispatcher.call("edit", {"edits": [{"path": "pkg/alpha.py", "old": "return 42", "new": "return 43"}]})
    assert not relative.is_error, relative.content
    assert "return 43" in (fresh / "pkg/alpha.py").read_text(encoding="utf-8")
    read_back = dispatcher.call("read", {"files": ["pkg/alpha.py:full"]})
    assert not read_back.is_error and "return 43" in read_back.content[0]["text"]

    # Returning to main is explicit and cannot inherit the stale checkout.
    switched = dispatcher.call("bash", {"command": "pwd", "cwd": str(main)})
    assert not switched.is_error
    assert str(main) in switched.content[0]["text"]
    assert "return 1" in (main / "pkg/alpha.py").read_text(encoding="utf-8")


def test_unrelated_git_repo_is_not_a_worktree_route(catalog: WorktreeCatalog, repos: tuple[Path, Path]) -> None:
    main, _ = repos
    catalog.entries()
    foreign = main.parent / "unrelated"
    foreign.mkdir()
    _git(foreign, "init")
    routed = catalog.route({"edits": [{"path": str(foreign / "not-allowed.txt"), "new": "unsafe", "replace": True}]})
    assert routed.worktree is None and not routed.explicit
    assert routed.arguments["edits"][0]["path"] == str(foreign / "not-allowed.txt")


def test_thin_client_still_refuses_project_id(
    routed_dispatcher: tuple[Dispatcher, ClientConfig],
) -> None:
    dispatcher, _ = routed_dispatcher
    refused = dispatcher.call("bash", {"command": "pwd", "project_id": "feature-x"})
    assert refused.is_error
    assert "project_id is not supported by this client" in refused.content[0]["text"]


def test_worktree_session_is_opened_once_and_closed_with_the_server(
    stub: StubServer, routed_dispatcher: tuple[Dispatcher, ClientConfig], repos: tuple[Path, Path]
) -> None:
    dispatcher, _ = routed_dispatcher
    _, linked = repos
    dispatcher.call("code_search", {"query": "a", "paths": str(linked)})
    dispatcher.call("code_search", {"query": "b", "paths": str(linked)})
    assert len(dispatcher._children) == 1
    closed_before = len(stub.state.closed_sessions)
    dispatcher.close_worktrees()
    assert not dispatcher._children
    assert len(stub.state.closed_sessions) == closed_before + 1
