"""Runtime Lite roots with member repositories.

A folder that is not a Git repository has its immediate child repositories as
members; a repository has itself plus its checked-out submodules, recursively.
Every member keeps its own index, every path is relative to the root, and an
answer that could not cover every member says which it left out.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from lemoncrow_client import cli, host_trial
from lemoncrow_client import workspace_members as lite_workspace
from lemoncrow_client.lite import mcp as lite_mcp
from lemoncrow_client.lite.index import LiteIndex
from lemoncrow_client.lite.mcp import LiteMcp
from lemoncrow_client.workspace_members import WorkspaceCatalog, discover_members, head_label

_INDEX = Path(".lemoncrow") / "workspace" / "runtime-lite-v3.sqlite3"


def _git(cwd: Path, *args: str) -> str:
    # A submodule clone has no repository identity, and an isolated HOME has no global one.
    identity = ("-c", "user.email=lite@example.invalid", "-c", "user.name=Runtime Lite")
    completed = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *identity, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _repo(root: Path, files: dict[str, str], *, branch: str = "main") -> Path:
    root.mkdir(parents=True)
    _git(root, "init", "-q", "-b", branch)
    _git(root, "config", "user.email", "lite@example.invalid")
    _git(root, "config", "user.name", "Runtime Lite")
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")
    return root


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A folder of repositories: billing serves a field that bazaar reads."""

    root = tmp_path / "projects"
    root.mkdir()
    billing = _repo(
        root / "billing",
        {
            "billing/api.py": ("def subscription_response(plan):\n    return {'subscription_plan_code': plan.code}\n"),
        },
    )
    _repo(
        root / "bazaar",
        {
            "bazaar/client.py": ("def read_subscription(payload):\n    return payload['subscription_plan_code']\n"),
        },
        branch="feature/plans",
    )
    # A sibling linked worktree of billing: the same repository, never a second member.
    _git(billing, "worktree", "add", "-q", "../billing-feature", "-b", "feature/x")
    # Not supported: a repository inside a non-Git child folder is never scanned.
    _repo(root / "services" / "authward", {"authward/tokens.py": "def issue_token():\n    return 'token'\n"})
    # Never members: hidden and symlinked children.
    _repo(root / ".cache" / "clone", {"x.py": "subscription_plan_code = 1\n"})
    (root / "alias").symlink_to(billing, target_is_directory=True)
    (root / "docker-compose.yml").write_text("services:\n  billing:\n    subscription_plan_code: x\n", encoding="utf-8")
    return root


def _with_submodules(tmp_path: Path, *, stale: bool = False) -> Path:
    """app declares vendor/lib (which declares vendor/inner) and vendor/extra (not checked out)."""

    sources = tmp_path / "sources"
    inner = _repo(sources / "inner", {"inner/core.py": "def inner_core():\n    return 1\n"})
    lib = _repo(sources / "lib", {"lib/pay.py": "def charge_card():\n    return 'ok'\n"})
    _git(lib, "submodule", "add", "-q", str(inner), "vendor/inner")
    _git(lib, "commit", "-qm", "inner")
    extra = _repo(sources / "extra", {"extra/x.py": "def extra_thing():\n    return 2\n"})
    app = _repo(tmp_path / "app", {"app/main.py": "def main():\n    return charge_card()\n"})
    _git(app, "submodule", "add", "-q", str(lib), "vendor/lib")
    _git(app, "submodule", "add", "-q", str(extra), "vendor/extra")
    if stale:
        _git(app, "submodule", "add", "-q", str(extra), "vendor/stale")
    _git(app, "commit", "-qm", "submodules")
    _git(app, "submodule", "update", "--init", "--recursive", "-q")
    _git(app, "submodule", "deinit", "-q", "-f", "vendor/extra")
    if stale:
        shutil.rmtree(app / ".git" / "modules" / "vendor" / "stale")
    return app


def _env(root: Path, tmp_path: Path) -> dict[str, str]:
    return {
        "LEMONCROW_WORKSPACE_ROOT": str(root),
        "LEMONCROW_HOME": str(tmp_path / "state"),
        "HOME": str(tmp_path),
        "LEMONCROW_RUNTIME_TRACE": "off",
    }


def _text(result: dict[str, object]) -> str:
    return "\n".join(str(block.get("text") or "") for block in result["content"])  # type: ignore[union-attr,attr-defined]


def _paths(result: dict[str, object]) -> list[str]:
    return [item["path"] for item in result["structuredContent"]["files"]]  # type: ignore[index]


# -- membership --------------------------------------------------------------- #


def test_a_folder_root_has_its_immediate_child_repositories_as_members(workspace: Path) -> None:
    membership = discover_members(workspace)
    assert [member.relpath for member in membership.members] == ["bazaar", "billing"]
    assert membership.other_checkouts == (("billing-feature", "billing"),)
    assert not membership.git_root and membership.multi and not membership.truncated
    assert head_label(workspace / "bazaar") == "feature/plans"
    assert head_label(workspace / "billing-feature") == "feature/x"


def test_a_stray_git_folder_holding_no_repository_leaves_a_folder_root(workspace: Path, tmp_path: Path) -> None:
    # Seen in a real projects folder: a tool left `.git/<name>/` and no repository.
    (workspace / ".git" / "lemoncrow").mkdir(parents=True)
    (workspace / "stale" / ".git").mkdir(parents=True)
    membership = discover_members(workspace)
    assert [member.relpath for member in membership.members] == ["bazaar", "billing"]
    assert not membership.git_root and membership.broken == ("stale",)
    assert lite_workspace.resolve_root(workspace) == workspace.resolve()
    server = LiteMcp(environment=_env(workspace, tmp_path))
    try:
        assert server._workspace is not None and not server._git_workspace
    finally:
        server.close()


def test_a_bare_repository_with_worktrees_is_one_member_on_its_default_branch(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    seed = _repo(tmp_path / "seed", {"pay.py": "x = 1\n"})
    _git(root, "clone", "-q", "--bare", str(seed), "pay.git")
    _git(root / "pay.git", "worktree", "add", "-q", "../pay-dev", "-b", "dev")
    _git(root / "pay.git", "worktree", "add", "-q", "../pay-main", "main")
    membership = discover_members(root)
    assert [member.relpath for member in membership.members] == ["pay-main"]
    assert membership.other_checkouts == (("pay-dev", "pay-main"),)


def test_a_repository_root_has_itself_and_its_submodules_recursively(tmp_path: Path) -> None:
    app = _with_submodules(tmp_path, stale=True)
    membership = discover_members(app)
    assert [(member.relpath, member.parent) for member in membership.members] == [
        (".", None),
        ("vendor/lib", "."),
        ("vendor/lib/vendor/inner", "vendor/lib"),
    ]
    assert membership.git_root and membership.multi
    assert membership.unchecked == ("vendor/extra",)
    assert membership.broken == ("vendor/stale",)

    plain = _repo(tmp_path / "plain", {"a.py": "A = 1\n"})
    assert not discover_members(plain).multi, "a repository without submodules is served as before"


def test_the_member_bound_is_reported_and_paths_past_it_still_route(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lite_workspace, "MAX_REPOSITORIES", 1)
    membership = discover_members(workspace)
    assert [member.relpath for member in membership.members] == ["bazaar"]
    assert membership.truncated
    catalog = WorkspaceCatalog(workspace)
    beyond = catalog.checkout_for((workspace / "billing" / "billing" / "api.py").resolve())
    assert beyond is not None and beyond.relpath == "billing"
    assert catalog.checkout_for(workspace / "docker-compose.yml") is None


# -- a folder of repositories ------------------------------------------------- #


def test_folder_session_lists_members_and_indexes_each_in_place(workspace: Path, tmp_path: Path) -> None:
    server = LiteMcp(environment=_env(workspace, tmp_path))
    try:
        instructions = server.handle({"id": 1, "method": "initialize"})["result"]["instructions"]  # type: ignore[index]
        assert "folder of Git repositories" in instructions
        assert "bazaar (feature/plans), billing (main)" in instructions
        assert "billing-feature (worktree of billing, feature/x)" in instructions
        assert "authward" not in instructions
        assert "bash cwd=<repository>" in instructions
        assert "`lc workspace status|branch|commit|push|pr`" in instructions and "kind=impact" in instructions
        prepared = server.prepare()
        assert prepared["mode"] == "workspace"
        assert set(prepared["repositories"]) == {"bazaar", "billing"}  # type: ignore[arg-type]
        for name in ("bazaar", "billing"):
            assert (workspace / name / _INDEX).is_file()
        for never in ("billing-feature", "services/authward", ".cache/clone"):
            assert not (workspace / never / _INDEX).exists()
        assert not (workspace / ".lemoncrow").exists(), "a non-Git root holds no LemonCrow state"
        assert not (workspace / ".git").exists()
    finally:
        server.close()


def test_pathless_search_covers_every_member_once_with_root_relative_paths(workspace: Path, tmp_path: Path) -> None:
    server = LiteMcp(environment=_env(workspace, tmp_path))
    try:
        server.prepare()
        found = server.call("code_search", {"query": "subscription_plan_code"})
        assert found["isError"] is False
        assert set(_paths(found)) == {"billing/billing/api.py", "bazaar/bazaar/client.py"}
        assert found["structuredContent"]["workspace"]["complete"] is True  # type: ignore[index]
        assert "## billing/billing/api.py:L" in _text(found)
        # A result path is directly usable by read and edit from the root.
        read = server.call("read", {"files": ["bazaar/bazaar/client.py"]})
        assert read["isError"] is False and "subscription_plan_code" in _text(read)

        assert _paths(server.call("code_search", {"query": "subscription_plan_code", "paths": "billing"})) == [
            "billing/billing/api.py"
        ]
        inner = server.call("code_search", {"query": "subscription_plan_code", "paths": ["bazaar/bazaar"]})
        assert _paths(inner) == ["bazaar/bazaar/client.py"]
        # Another checkout of a member is reachable by path, through its own index.
        other = server.call("code_search", {"query": "subscription_plan_code", "paths": "billing-feature"})
        assert _paths(other) == ["billing-feature/billing/api.py"]

        loose = server.call("code_search", {"query": "subscription_plan_code", "paths": "docker-compose.yml"})
        assert loose["isError"] is False
        assert "[no git: literal grep docker-compose.yml]" in _text(loose)

        escaped = server.call("code_search", {"query": "x", "paths": "../outside"})
        assert escaped["isError"] is True
    finally:
        server.close()


def test_one_edit_changes_two_members_and_both_indexes_see_it(workspace: Path, tmp_path: Path) -> None:
    server = LiteMcp(environment=_env(workspace, tmp_path))
    try:
        server.prepare()
        server.call("code_search", {"query": "subscription_plan_code"})
        edited = server.call(
            "edit",
            {
                "edits": [
                    {"path": "billing/billing/api.py", "old": "subscription_plan_code", "new": "plan_identifier_v2"},
                    {"path": "bazaar/bazaar/client.py", "old": "subscription_plan_code", "new": "plan_identifier_v2"},
                ]
            },
        )
        assert edited["isError"] is False, _text(edited)
        renamed = server.call("code_search", {"query": "plan_identifier_v2"})
        assert set(_paths(renamed)) == {"billing/billing/api.py", "bazaar/bazaar/client.py"}
        status = server.call("bash", {"command": "git status --porcelain", "cwd": "billing"})
        assert "billing/api.py" in _text(status)
    finally:
        server.close()


def test_symbols_relations_graph_and_blame_answer_across_members(workspace: Path, tmp_path: Path) -> None:
    server = LiteMcp(environment=_env(workspace, tmp_path))
    try:
        server.prepare()
        symbol = server.call("read", {"symbol": "read_subscription"})
        assert symbol["isError"] is False
        assert symbol["structuredContent"]["symbols"][0]["path"] == "bazaar/bazaar/client.py"  # type: ignore[index]
        usages = server.call("relations", {"symbol": "subscription_plan_code", "kind": "usages"})
        assert {item["repo"] for item in usages["structuredContent"]["items"]} == {"billing", "bazaar"}  # type: ignore[index]
        mapped = server.call("graph", {"path": "bazaar"})
        assert [item["path"] for item in mapped["structuredContent"]["items"]] == ["bazaar/bazaar/client.py"]  # type: ignore[index]
        blamed = server.call("blame", {"path": "billing/billing/api.py"})
        assert blamed["isError"] is False, _text(blamed)
        loose = server.call("blame", {"path": "docker-compose.yml"})
        assert loose["isError"] is True and "[lc:git_required]" in _text(loose)
    finally:
        server.close()


def test_a_session_inside_a_member_shares_that_members_index(workspace: Path, tmp_path: Path) -> None:
    parent = LiteMcp(environment=_env(workspace, tmp_path))
    try:
        built = parent.prepare()["repositories"]["billing"]  # type: ignore[index]
    finally:
        parent.close()
    # An explicit root inside a work tree is served as that repository.
    inside = LiteMcp(environment=_env(workspace / "billing" / "billing", tmp_path))
    try:
        assert inside.repo_root == (workspace / "billing").resolve()
        assert inside._workspace is None
        adopted = inside.prepare()
        assert adopted["index_path"] == built["index_path"]  # type: ignore[index]
        assert adopted["generation"] == built["generation"]  # type: ignore[index]
        assert adopted["files_indexed"] == 0
    finally:
        inside.close()


# -- a repository with submodules --------------------------------------------- #


def test_repository_session_searches_its_submodules_without_duplicates(tmp_path: Path) -> None:
    app = _with_submodules(tmp_path)
    _git(app / "vendor" / "lib", "commit", "--allow-empty", "-qm", "ahead of the recorded commit")
    server = LiteMcp(environment=_env(app, tmp_path))
    try:
        instructions = server.handle({"id": 1, "method": "initialize"})["result"]["instructions"]  # type: ignore[index]
        assert "this Git repository (listed as .) and its checked-out submodules" in instructions
        assert "vendor/lib (main, submodule of ., not at the commit . records)" in instructions
        assert "vendor/lib/vendor/inner (detached " in instructions
        assert "Submodules not checked out (not searched): vendor/extra." in instructions
        assert "For a submodule: create a branch in it" in instructions
        prepared = server.prepare()
        assert set(prepared["repositories"]) == {".", "vendor/lib", "vendor/lib/vendor/inner"}  # type: ignore[arg-type]

        found = server.call("code_search", {"query": "charge_card"})
        assert sorted(_paths(found)) == ["app/main.py", "vendor/lib/lib/pay.py"]
        nested = server.call("code_search", {"query": "inner_core"})
        assert _paths(nested) == ["vendor/lib/vendor/inner/inner/core.py"]
        scoped = server.call("code_search", {"query": "charge_card", "paths": "vendor"})
        assert _paths(scoped) == ["vendor/lib/lib/pay.py"]
        edited = server.call(
            "edit", {"edits": [{"path": "vendor/lib/lib/pay.py", "old": "charge_card", "new": "charge_card_v2"}]}
        )
        assert edited["isError"] is False, _text(edited)
        assert _paths(server.call("code_search", {"query": "charge_card_v2"})) == ["vendor/lib/lib/pay.py"]
        blamed = server.call("blame", {"path": "vendor/lib/vendor/inner/inner/core.py"})
        assert blamed["isError"] is False, _text(blamed)
    finally:
        server.close()
    # Every index lives in its own checkout and is excluded there: the parent stays clean.
    for checkout in (app, app / "vendor" / "lib", app / "vendor" / "lib" / "vendor" / "inner"):
        assert (checkout / _INDEX).is_file()
    assert ".lemoncrow" not in _git(app, "status", "--porcelain", "--ignore-submodules=none")


def test_a_session_inside_a_submodule_is_rooted_there(tmp_path: Path) -> None:
    app = _with_submodules(tmp_path)
    server = LiteMcp(environment=_env(app / "vendor" / "lib", tmp_path))
    try:
        assert server.repo_root == (app / "vendor" / "lib").resolve()
        assert server._workspace is not None
        assert [member.relpath for member in server._workspace.membership().members] == [".", "vendor/inner"]
    finally:
        server.close()


# -- index building and coverage ---------------------------------------------- #


def test_a_slow_member_does_not_hold_back_the_others_and_is_named(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    release = threading.Event()
    original = LiteIndex.adopt_prepared

    def gated(self: LiteIndex, *, progress: object = None) -> object:
        if self.repo_root.name == "bazaar":
            assert release.wait(10)
        return original(self, progress=progress)  # type: ignore[arg-type]

    monkeypatch.setattr(LiteIndex, "adopt_prepared", gated)
    monkeypatch.setattr(lite_mcp, "_INDEX_WAIT_S", 2.0)
    server = LiteMcp(environment=_env(workspace, tmp_path))
    try:
        server.handle({"id": 1, "method": "initialize"})
        partial = server.call("code_search", {"query": "subscription_plan_code"})
        assert partial["isError"] is False, _text(partial)
        coverage = partial["structuredContent"]["workspace"]  # type: ignore[index]
        assert coverage["searched"] == ["billing"] and coverage["building"] == ["bazaar"]
        assert coverage["complete"] is False
        assert "[lc:index_building]" in _text(partial) and "bazaar" in _text(partial)
        assert _paths(partial) == ["billing/billing/api.py"]
        only_building = server.call("code_search", {"query": "subscription_plan_code", "paths": "bazaar"})
        assert only_building["isError"] is True
        release.set()
        complete = server.call("code_search", {"query": "subscription_plan_code"})
        assert complete["structuredContent"]["workspace"]["complete"] is True  # type: ignore[index]
        assert "bazaar/bazaar/client.py" in _paths(complete)
    finally:
        release.set()
        server.close()


def test_a_member_a_call_names_is_built_ahead_of_queued_ones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "projects"
    for name in ("alpha", "bravo", "charlie"):
        _repo(root / name, {f"{name}.py": f"def {name}_entry():\n    return 1\n"})
    started = threading.Event()
    release = threading.Event()
    order: list[str] = []
    original = LiteIndex.adopt_prepared

    def gated(self: LiteIndex, *, progress: object = None) -> object:
        order.append(self.repo_root.name)
        if self.repo_root.name == "alpha":
            started.set()
            assert release.wait(10)
        return original(self, progress=progress)  # type: ignore[arg-type]

    monkeypatch.setattr(LiteIndex, "adopt_prepared", gated)
    monkeypatch.setattr(lite_mcp, "_BUILD_WORKERS", 1)
    monkeypatch.setattr(lite_mcp, "_INDEX_WAIT_S", 0.2)
    server = LiteMcp(environment=_env(root, tmp_path))
    try:
        server.handle({"id": 1, "method": "initialize"})
        assert started.wait(10)
        waiting = server.call("code_search", {"query": "charlie_entry", "paths": "charlie"})
        assert waiting["isError"] is True and "[lc:index_building]" in _text(waiting)
        release.set()
        server.prepare()
        assert order == ["alpha", "charlie", "bravo"]
    finally:
        release.set()
        server.close()


def test_a_failed_member_is_named_and_the_others_still_answer(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = LiteIndex.adopt_prepared

    def broken(self: LiteIndex, *, progress: object = None) -> object:
        if self.repo_root.name == "bazaar":
            raise RuntimeError("disk is read-only")
        return original(self, progress=progress)  # type: ignore[arg-type]

    monkeypatch.setattr(LiteIndex, "adopt_prepared", broken)
    server = LiteMcp(environment=_env(workspace, tmp_path))
    try:
        found = server.call("code_search", {"query": "subscription_plan_code"})
        assert found["isError"] is False
        assert found["structuredContent"]["workspace"]["failed"] == ["bazaar"]  # type: ignore[index]
        assert "[lc:lite_failed] bazaar was not searched: disk is read-only" in _text(found)
        assert _paths(found) == ["billing/billing/api.py"]
    finally:
        server.close()


def test_search_past_the_member_bound_says_it_is_partial(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(lite_workspace, "MAX_REPOSITORIES", 1)
    server = LiteMcp(environment=_env(workspace, tmp_path))
    try:
        server.prepare()
        pathless = server.call("code_search", {"query": "subscription_plan_code"})
        assert pathless["structuredContent"]["workspace"]["truncated"] is True  # type: ignore[index]
        assert "[lc:workspace_partial]" in _text(pathless)
        assert _paths(pathless) == ["bazaar/bazaar/client.py"]
        named = server.call("code_search", {"query": "subscription_plan_code", "paths": "billing"})
        assert _paths(named) == ["billing/billing/api.py"]
    finally:
        server.close()


# -- entry points --------------------------------------------------------------- #


def test_lc_with_and_runtime_index_accept_a_root_with_members(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(workspace)
    assert host_trial._trial_workspace_root() == workspace.resolve()
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.chdir(empty)
    with pytest.raises(host_trial.HostTrialError, match="folder that holds git repositories"):
        host_trial._trial_workspace_root()

    sink = io.StringIO()
    assert cli._runtime_index(["--repo-root", str(workspace), "--json"], sink) == 0
    assert set(json.loads(sink.getvalue())["repositories"]) == {"bazaar", "billing"}
    for name in ("bazaar", "billing"):
        assert (workspace / name / _INDEX).is_file()
    assert not (workspace / ".lemoncrow").exists()

    app = _with_submodules(tmp_path)
    sink = io.StringIO()
    assert cli._runtime_index(["--repo-root", str(app), "--json"], sink) == 0
    assert set(json.loads(sink.getvalue())["repositories"]) == {".", "vendor/lib", "vendor/lib/vendor/inner"}
