"""Tests for per-request project isolation in the stdio MCP server (N10)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from lemoncrow.gateway.adapters import mcp_server


@pytest.fixture(autouse=True)
def _reset_request_project() -> Iterator[None]:
    mcp_server._request_project.value = None
    yield
    mcp_server._request_project.value = None


def test_default_workspace_when_no_override(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    mcp_server._request_project.value = None
    assert mcp_server._workspace_root() == Path(str(tmp_path))


def test_override_wins_over_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # H1 — a wire override must opt in AND stay inside the workspace root, so the
    # accepted override here is a subdirectory of the configured workspace.
    monkeypatch.setenv("LEMONCROW_HTTP_ALLOW_PROJECT_OVERRIDE", "1")
    env_ws = tmp_path / "env_ws"
    nested = env_ws / "sub_repo"
    env_ws.mkdir()
    nested.mkdir()
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(env_ws))

    prior = mcp_server._set_request_project(str(nested))
    try:
        assert mcp_server._workspace_root().resolve() == nested.resolve()
    finally:
        mcp_server._clear_request_project(prior)

    # Cleared -> back to the env workspace.
    assert mcp_server._workspace_root() == Path(str(env_ws))


def test_extract_from_meta_header(tmp_path: Path) -> None:
    params = {"_meta": {"mcp-project-path": str(tmp_path)}}
    assert mcp_server._extract_request_project(params, {}) == str(tmp_path)


def test_extract_from_arg_and_pops_it(tmp_path: Path) -> None:
    args = {"project_path": str(tmp_path), "query": "x"}
    assert mcp_server._extract_request_project({}, args) == str(tmp_path)
    # The reserved arg must be removed so it never reaches the tool handler.
    assert "project_path" not in args
    assert args == {"query": "x"}


def test_extract_absent_returns_none() -> None:
    assert mcp_server._extract_request_project({}, {"query": "x"}) is None
    assert mcp_server._extract_request_project({"_meta": {}}, {}) is None


def test_set_rejects_nonexistent_path(tmp_path: Path) -> None:
    mcp_server._set_request_project(str(tmp_path / "missing"))
    assert mcp_server._request_project.value is None


def test_set_rejects_empty_string() -> None:
    mcp_server._set_request_project("   ")
    assert mcp_server._request_project.value is None


def test_set_rejects_out_of_root_even_with_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # H1 — a real directory OUTSIDE the workspace root must be rejected even when
    # the opt-in flag is set, so a wire override can never pivot out of bounds.
    monkeypatch.setenv("LEMONCROW_HTTP_ALLOW_PROJECT_OVERRIDE", "1")
    root = tmp_path / "workspace"
    outside = tmp_path / "elsewhere"
    root.mkdir()
    outside.mkdir()
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(root))

    mcp_server._set_request_project(str(outside))
    assert mcp_server._request_project.value is None  # rejected -> no pivot
    assert mcp_server._workspace_root() == Path(str(root))  # still the configured root


def test_set_rejects_in_root_when_opt_in_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # H1 — default (flag off) rejects even an in-root override; acceptance is
    # strictly opt-in.
    monkeypatch.delenv("LEMONCROW_HTTP_ALLOW_PROJECT_OVERRIDE", raising=False)
    root = tmp_path / "workspace"
    nested = root / "sub"
    root.mkdir()
    nested.mkdir()
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(root))

    mcp_server._set_request_project(str(nested))
    assert mcp_server._request_project.value is None  # flag off -> not honored


def test_set_accepts_in_root_with_opt_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # H1 — an in-root directory with the opt-in flag set is honored.
    monkeypatch.setenv("LEMONCROW_HTTP_ALLOW_PROJECT_OVERRIDE", "1")
    root = tmp_path / "workspace"
    nested = root / "sub"
    root.mkdir()
    nested.mkdir()
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(root))

    mcp_server._set_request_project(str(nested))
    assert mcp_server._request_project.value == str(nested.resolve())
    assert mcp_server._workspace_root().resolve() == nested.resolve()


def test_set_returns_prior_for_nesting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # H1 — opt in and keep both candidates inside the workspace root so they are
    # accepted; this test only exercises the prior-value nesting contract.
    monkeypatch.setenv("LEMONCROW_HTTP_ALLOW_PROJECT_OVERRIDE", "1")
    monkeypatch.setenv("CLAUDE_WORKSPACE_ROOT", str(tmp_path))
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    p0 = mcp_server._set_request_project(str(a))
    assert p0 is None
    p1 = mcp_server._set_request_project(str(b))
    assert p1 == str(a.resolve())
    mcp_server._clear_request_project(p1)
    assert mcp_server._request_project.value == str(a.resolve())
    mcp_server._clear_request_project(p0)
    assert mcp_server._request_project.value is None
