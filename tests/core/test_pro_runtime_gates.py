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
from lemoncrow.core.capabilities.optimization.policy import load_current_policy
from lemoncrow.core.service import code_warm
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


def test_free_policy_is_unoptimized(tmp_path: Path) -> None:
    # No license/overlay (autouse _clean) -> the savings engine is off.
    policy = load_current_policy(tmp_path)
    assert policy.preset == "custom"
    assert policy.compaction.trigger_at_context_fraction == 1.0


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
    assert "source_projection:python" in out  # source_projection is free: AST projection applies


def test_free_context_compression_is_passthrough() -> None:
    from typing import ClassVar

    from lemoncrow.core.capabilities.context_compression.capability import ContextCompressionCapability

    class _Ledger:
        session_id = "s"
        events: ClassVar[list[object]] = []

    cap = ContextCompressionCapability()
    result = cap.compress_with_provenance(_Ledger(), token_budget=100)
    assert result.reduction_pct == 0.0  # Free: no license -> passthrough, no compression


def test_free_scoped_context_pull_is_locked() -> None:
    from lemoncrow.core.capabilities.licensing import FeatureLocked
    from lemoncrow.gateway.adapters import mcp_server

    with pytest.raises(FeatureLocked) as exc_info:
        mcp_server.tool_get_context({"task": "x", "mode": "pull"})
    assert exc_info.value.feature == "scoped_context"
    assert "LemonCrow Pro" in str(exc_info.value)
