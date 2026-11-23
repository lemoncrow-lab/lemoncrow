"""`lc review` on the local Review server shows the workspace impact in the Reader's Brief (multi-repo workspace P4).

A real local server and two Git repositories side by side in a folder: the capture stores the packet's
``workspace_impact`` slice, and the Brief projection returns it without any source line from the other repository.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.config import ClientConfig
from lemoncrow_client.review import _capture, resolve_review_range
from lemoncrow_server_core import local_app
from lemoncrow_server_core.bootstrap import build_local_server
from lemoncrow_server_core.config import LocalServerConfig

from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range
from lemoncrow.pro.capabilities.review.hosted import capture_server_review, server_review_request
from lemoncrow.pro.capabilities.review.workspace_impact import WORKSPACE_ENV


def _repo(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True)
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.name", "W"),
        ("config", "user.email", "w@example.invalid"),
        ("config", "commit.gpgsign", "false"),
    ):
        subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)
    _write(root, files)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=root, check=True, capture_output=True)
    return root


def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def _two_repositories(tmp_path: Path) -> Path:
    """``projects/billing`` and ``projects/bazaar``, where bazaar imports billing; returns billing."""

    projects = tmp_path / "projects"
    billing = _repo(
        projects / "billing",
        {
            "pyproject.toml": '[project]\nname = "billing"\nversion = "1.0"\n',
            "billing/__init__.py": "",
            "billing/invoices.py": "def total(lines):\n    return sum(lines)\n",
        },
    )
    _repo(
        projects / "bazaar",
        {
            "pyproject.toml": '[project]\nname = "bazaar"\nversion = "1.0"\ndependencies = ["billing"]\n',
            "bazaar/__init__.py": "",
            "bazaar/checkout.py": "from billing import invoices\n\n\ndef pay(lines):\n    return invoices.total(lines)\n",
        },
    )
    return billing


def test_the_installed_client_s_lc_review_records_the_reach_and_its_other_captures_never_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The installed `lc review` sends Views and lets the local server derive the packet, so the server scans the
    # checkout it was granted; a capture that does not ask (a watch, a hook, an agent) stores an empty slice.
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    asyncio.run(_exercise_derived(tmp_path, _two_repositories(tmp_path)))


async def _exercise_derived(tmp_path: Path, billing: Path) -> None:
    server, _state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
    )
    url = await server.start()
    config = ClientConfig(
        url=url,
        token="",
        repo_root=billing,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=60.0,
        resolution_cache_bytes=0,
        offer_local_fs=True,
        token_source="test",
    )

    def capture(text: str, *, with_workspace: bool) -> dict[str, Any]:
        _write(billing, {"billing/invoices.py": text})
        response, _link = _capture(
            config,
            resolve_review_range(billing, None, working_tree=True),
            with_impact=True,
            restore_archived=False,
            with_workspace=with_workspace,
        )
        review_id = str(dict(response["review"])["id"])  # type: ignore[call-overload]
        return dict(server_review_request(config, "GET", f"/api/reviews/{review_id}/projection"))

    try:
        unasked = await asyncio.to_thread(
            capture, "def total(lines):\n    return round(sum(lines))\n", with_workspace=False
        )
        asked = await asyncio.to_thread(
            capture, "def total(lines):\n    return round(sum(lines), 2)\n", with_workspace=True
        )
    finally:
        await server.stop()

    assert unasked["workspace_impact"] == {}
    impact = asked["workspace_impact"]
    assert impact["repo"] == "billing" and impact["root"] == str(billing.parent.resolve())
    assert [hit["repo"] for hit in impact["reached"]] == ["bazaar"]
    assert '"detail"' not in json.dumps(impact)
    assert asked["counts"] == unasked["counts"]


def test_the_brief_projection_returns_the_slice_a_local_capture_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    projects = tmp_path / "projects"
    billing = _repo(
        projects / "billing",
        {
            "pyproject.toml": '[project]\nname = "billing"\nversion = "1.0"\n',
            "billing/__init__.py": "",
            "billing/invoices.py": "def total(lines):\n    return sum(lines)\n",
        },
    )
    _repo(
        projects / "bazaar",
        {
            "pyproject.toml": '[project]\nname = "bazaar"\nversion = "1.0"\ndependencies = ["billing"]\n',
            "bazaar/__init__.py": "",
            "bazaar/checkout.py": "from billing import invoices\n\n\ndef pay(lines):\n    return invoices.total(lines)\n",
        },
    )
    asyncio.run(_exercise(tmp_path, billing))


async def _exercise(tmp_path: Path, billing: Path) -> None:
    server, _state = build_local_server(
        LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
    )
    url = await server.start()
    config = ClientConfig(
        url=url,
        token="",
        repo_root=billing,
        state_dir=tmp_path / "client",
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=60.0,
        resolution_cache_bytes=0,
        offer_local_fs=True,
        token_source="test",
    )

    def capture(text: str, *, with_workspace: bool) -> dict[str, Any]:
        _write(billing, {"billing/invoices.py": text})
        captured = capture_server_review(
            config,
            billing,
            resolve_rev_range(billing, working_tree=True),
            store_root=tmp_path / "client-store",
            session_id=None,
            limit=40,
            with_impact=True,
            with_provenance=False,
            with_workspace=with_workspace,
        )
        return dict(server_review_request(config, "GET", f"/api/reviews/{captured.review_id}/projection"))

    try:
        without = await asyncio.to_thread(
            capture, "def total(lines):\n    return round(sum(lines))\n", with_workspace=False
        )
        found = await asyncio.to_thread(
            capture, "def total(lines):\n    return round(sum(lines), 2)\n", with_workspace=True
        )
    finally:
        await server.stop()

    assert without["workspace_impact"] == {}
    impact = found["workspace_impact"]
    assert impact["repo"] == "billing" and impact["root"] == str(billing.parent.resolve())
    assert [hit["repo"] for hit in impact["reached"]] == ["bazaar"]
    links = [link for edge in impact["reached"][0]["edges"] for link in edge["links"]]
    assert any(link["consumer"]["path"] == "bazaar/bazaar/checkout.py" for link in links), links
    # The other repository's source lines stay out of the Reader.
    assert '"detail"' not in json.dumps(impact)
    # Never a finding: the Brief's queue counts are the same with and without it.
    assert found["counts"] == without["counts"]

    # Nor a Risk fact: billing code reads High either way, for the same facts, and names no workspace.
    risk, before = found["summary"]["risk"], without["summary"]["risk"]
    assert risk["level"] == before["level"] == "high"
    assert risk["facts"] == before["facts"]
    assert risk["text"] == before["text"] == "High · total is billing code"


def _projects(tmp_path: Path) -> tuple[Path, Path]:
    projects = tmp_path / "projects"
    billing = _repo(
        projects / "billing",
        {
            "pyproject.toml": '[project]\nname = "billing"\nversion = "1.0"\n',
            "billing/__init__.py": "",
            "billing/invoices.py": "def total(lines):\n    return sum(lines)\n",
        },
    )
    _repo(
        projects / "bazaar",
        {
            "pyproject.toml": '[project]\nname = "bazaar"\nversion = "1.0"\ndependencies = ["billing"]\n',
            "bazaar/__init__.py": "",
            "bazaar/checkout.py": "from billing import invoices\n\n\ndef pay(lines):\n    return invoices.total(lines)\n",
        },
    )
    return projects, billing


def _client(url: str, repo: Path, state: Path, *, offer_local_fs: bool = True) -> ClientConfig:
    return ClientConfig(
        url=url,
        token="",
        repo_root=repo,
        state_dir=state,
        install_mode="local",
        startup_budget_s=10.0,
        request_timeout_s=10.0,
        tool_timeout_s=60.0,
        resolution_cache_bytes=0,
        offer_local_fs=offer_local_fs,
        token_source="test",
    )


def test_the_installed_client_gets_the_slice_only_for_a_review_a_person_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thin client captures through the server, which derives the packet on a scratch copy of the Views."""

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    projects, billing = _projects(tmp_path)
    worktree = tmp_path / "elsewhere"
    subprocess.run(["git", "worktree", "add", "-q", "-b", "side", str(worktree)], cwd=billing, check=True)
    for name, value in (("user.name", "W"), ("user.email", "w@example.invalid")):
        subprocess.run(["git", "config", name, value], cwd=worktree, check=True)
    edits = iter(range(100))

    async def run() -> dict[str, dict[str, Any]]:
        server, _state = build_local_server(
            LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
        )
        url = await server.start()

        def capture(repo: Path, *, offer_local_fs: bool = True, edit: bool = True, **options: Any) -> dict[str, Any]:
            if edit:
                _write(repo, {"billing/invoices.py": f"def total(lines):\n    return sum(lines) + {next(edits)}\n"})
            config = _client(url, repo, tmp_path / "client", offer_local_fs=offer_local_fs)
            response, _url = _capture(
                config,
                resolve_review_range(repo, working_tree=True),
                restore_archived=False,
                **{"with_impact": True, **options},
            )
            review = response["review"]
            assert isinstance(review, dict)
            return dict(server_review_request(config, "GET", f"/api/reviews/{review['id']}/projection"))

        def scenario() -> dict[str, dict[str, Any]]:
            return {
                "checkpoint": capture(billing),
                "no_impact": capture(billing, with_impact=False, with_workspace=True),
                "no_grant": capture(billing, with_workspace=True, offer_local_fs=False),
                # Unchanged bytes keep their Snapshot, which was made without the slice: it is added in place.
                "same_bytes": capture(billing, edit=False, with_workspace=True),
                "person": capture(billing, with_workspace=True),
                "worktree": capture(worktree, with_workspace=True),
            }

        try:
            return await asyncio.to_thread(scenario)
        finally:
            await server.stop()

    found = asyncio.run(run())

    # A watch loop or an agent checkpoint captures with no slice, as does --no-impact or a client that proved nothing.
    for name in ("checkpoint", "no_impact", "no_grant"):
        assert found[name]["workspace_impact"] == {}, name
    for name in ("same_bytes", "person", "worktree"):
        impact = found[name]["workspace_impact"]
        assert impact["repo"] == "billing" and impact["root"] == str(projects.resolve()), name
        assert [hit["repo"] for hit in impact["reached"]] == ["bazaar"], name
        assert '"detail"' not in json.dumps(impact)
    assert found["person"]["counts"] == found["checkpoint"]["counts"]


def test_a_workspace_root_set_in_the_shell_reaches_the_long_running_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    projects, billing = _projects(tmp_path)
    # platform holds billing as a submodule, so billing belongs to the folder and to platform; a root narrows it.
    platform = _repo(projects / "platform", {"README.md": "platform\n"})
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(billing), "billing"],
        cwd=platform,
        check=True,
    )
    subprocess.run(["git", "commit", "-qm", "billing"], cwd=platform, check=True)
    member = platform / "billing"
    for name, value in (("user.name", "W"), ("user.email", "w@example.invalid")):
        subprocess.run(["git", "config", name, value], cwd=member, check=True)

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        server, _state = build_local_server(
            LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
        )
        url = await server.start()
        config = _client(url, member, tmp_path / "client")

        def capture(text: str) -> dict[str, Any]:
            _write(member, {"billing/invoices.py": text})
            response, _url = _capture(
                config,
                resolve_review_range(member, working_tree=True),
                with_impact=True,
                restore_archived=False,
                with_workspace=True,
            )
            review = response["review"]
            assert isinstance(review, dict)
            return dict(server_review_request(config, "GET", f"/api/reviews/{review['id']}/projection"))

        def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
            folder = capture("def total(lines):\n    return sum(lines) + 1\n")
            monkeypatch.setenv(WORKSPACE_ENV, str(platform))
            return folder, capture("def total(lines):\n    return sum(lines) + 2\n")

        try:
            return await asyncio.to_thread(scenario)
        finally:
            await server.stop()

    folder, named = asyncio.run(run())
    assert folder["workspace_impact"]["root"] == str(projects.resolve())
    assert named["workspace_impact"]["root"] == str(platform.resolve())


def test_a_scan_past_the_wait_never_holds_the_capture_and_still_reaches_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from lemoncrow_client import workspace_graph

    monkeypatch.setenv("LEMONCROW_AST_GREP_BIN", str(tmp_path / "sg"))
    monkeypatch.delenv(WORKSPACE_ENV, raising=False)
    monkeypatch.setattr(local_app, "_WORKSPACE_SCAN_WAIT_S", 0.0)
    _projects_root, billing = _projects(tmp_path)
    real, release = workspace_graph.impact, threading.Event()

    def slow(*args: Any, **kwargs: Any) -> Any:
        release.wait(60)
        return real(*args, **kwargs)

    monkeypatch.setattr(workspace_graph, "impact", slow)

    async def run() -> tuple[dict[str, Any], dict[str, Any]]:
        server, _state = build_local_server(
            LocalServerConfig(listen_port=0, state_root=tmp_path / "state", local_no_auth=True, allow_local_fs=True)
        )
        url = await server.start()

        def scenario() -> tuple[dict[str, Any], dict[str, Any]]:
            config = _client(url, billing, tmp_path / "client")
            _write(billing, {"billing/invoices.py": "def total(lines):\n    return sum(lines) + 1\n"})
            response, _url = _capture(
                config,
                resolve_review_range(billing, working_tree=True),
                with_impact=True,
                restore_archived=False,
                with_workspace=True,
            )
            review = response["review"]
            assert isinstance(review, dict)
            path = f"/api/reviews/{review['id']}/projection"
            # The scan is still blocked, and the Snapshot is already readable.
            before = dict(server_review_request(config, "GET", path))
            release.set()
            deadline = time.monotonic() + 30
            while True:
                after = dict(server_review_request(config, "GET", path))
                if after["workspace_impact"] or time.monotonic() > deadline:
                    return before, after
                time.sleep(0.1)

        try:
            return await asyncio.to_thread(scenario)
        finally:
            release.set()
            await server.stop()

    before, after = asyncio.run(run())
    assert before["workspace_impact"] == {}
    assert [hit["repo"] for hit in after["workspace_impact"]["reached"]] == ["bazaar"]
