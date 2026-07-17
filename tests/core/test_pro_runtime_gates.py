"""Runtime entitlement gates (no CLI control surface).

These exercise gates wired into runtime/service code rather than a CLI command:
the savings-engine policy switch for Free vs. Pro (`optimizer`). The
code-warmer's per-repo cap and the read-side AST source projection used to be
gated on `unlimited_repos` / `source_projection`; both are free now, so the
regression tests below assert they stay unlocked without a Pro license.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from lemoncrow.core.capabilities.licensing import entitlements
from lemoncrow.core.service import code_warm
from lemoncrow.pro.capabilities.optimization.policy import load_current_policy
from tests.helpers import deny_oauth, grant_oauth_pro


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    deny_oauth(monkeypatch)
    yield
    entitlements.reload()


def _setup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, n: int) -> None:
    workspaces = []
    for i in range(n):
        ws = tmp_path / f"ws{i}"
        ws.mkdir()
        workspaces.append(ws)
    monkeypatch.setattr(code_warm, "discover_workspaces", lambda: list(workspaces))
    # Suppress actual subprocess launches; we only count which workspaces were fired.
    monkeypatch.setattr(code_warm, "_fire_index_subprocess", lambda workspace: None)


def test_code_warmer_warms_all_repos_without_pro(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup(monkeypatch, tmp_path, 3)
    warmer = code_warm._CodeWarmer()
    warmer._warm_once()
    assert len(warmer._fired) == 3  # unlimited_repos is free: no per-repo cap


def test_signed_out_policy_is_balanced(tmp_path: Path) -> None:
    # Signed out is fully unlocked now (autouse _clean denies OAuth but every
    # feature is granted locally), so the savings engine runs the optimized
    # "balanced" preset rather than the old unoptimized Free baseline.
    policy = load_current_policy(tmp_path)
    assert policy.preset == "balanced"
    assert policy.name != "Free (unoptimized)"


def test_pro_policy_is_balanced(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    grant_oauth_pro(monkeypatch)
    policy = load_current_policy(tmp_path)
    assert policy.preset == "balanced"


def _big_python_src() -> str:
    return "def f():\n" + "\n\n\n".join(f"    x{i} = {i}  " for i in range(2000)) + "\n"


def test_read_uses_source_projection_without_pro(monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.gateway.adapters import mcp_server

    monkeypatch.setenv("LEMONCROW_AUTO_COMPACT_OUTPUT", "1")
    monkeypatch.setenv("LEMONCROW_MCP_COMPACT_RESULT_CHARS", "2000")
    out = mcp_server._auto_compact_result_text(_big_python_src(), "read", {"path": "mod.py"})
    assert "projection:python" in out  # source_projection is free: AST projection applies


def test_free_context_compression_is_passthrough() -> None:
    from typing import ClassVar

    from lemoncrow.pro.capabilities.context_compression.capability import ContextCompressionCapability

    class _Ledger:
        session_id = "s"
        events: ClassVar[list[object]] = []

    cap = ContextCompressionCapability()
    result = cap.compress_with_provenance(_Ledger(), token_budget=100)
    assert result.reduction_pct == 0.0  # Free: no license -> passthrough, no compression


def test_scoped_context_pull_is_no_longer_locked() -> None:
    # Pull-mode scoped context used to be Pro-gated: tool_get_context(mode="pull")
    # called licensing.require("scoped_context"), which raised FeatureLocked when
    # signed out. That gate is neutralized now — require never raises and the
    # feature resolves as granted locally.
    from lemoncrow.core.capabilities import licensing

    assert licensing.require("scoped_context") is None  # never raises FeatureLocked
    assert licensing.has_feature("scoped_context") is True
