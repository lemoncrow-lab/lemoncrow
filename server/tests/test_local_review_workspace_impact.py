"""`lc review` on the local Review server shows the workspace impact in the Reader's Brief (multi-repo workspace P4).

A real local server and two Git repositories side by side in a folder: the capture stores the packet's
``workspace_impact`` slice, and the Brief projection returns it without any source line from the other repository.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from lemoncrow_client.config import ClientConfig
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
