"""Unit tests for BenchMode singleton and capability guards (MODE-01 through MODE-06).

Covers:
- BenchMode.is_off() / mode() / bootstrap() behaviour (MODE-01, MODE-02)
- Singleton idempotency (MODE-03)
- make_arm_env() helper (MODE-05)
- mcp_tool_visible_to_llm bench-off guard (MODE-04)
- MemoryRegistry._load() bench-off short-circuit (MODE-06)
- ContextCompressionCapability.compress_with_provenance() passthrough (MODE-06)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Force the submodule into sys.modules so we can retrieve it reliably.
# NOTE: lemoncrow/bench/__init__.py exports a *function* called `mode`, which
# shadows the submodule at attribute lookup (`lemoncrow.bench.mode` resolves to
# the function when accessed as an attribute).  Using sys.modules avoids this.
import lemoncrow.bench.mode  # noqa: F401 — ensures sys.modules entry exists

_bm = sys.modules["lemoncrow.bench.mode"]

from lemoncrow.bench.mode import BenchMode  # noqa: E402

# ---------------------------------------------------------------------------
# Autouse fixture: reset the _mode singleton and LEMONCROW_BENCH_MODE env var
# before *every* test so they are fully independent.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_bench_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset singleton and env var so each test starts clean."""
    monkeypatch.setattr(_bm, "_mode", None)
    monkeypatch.delenv("LEMONCROW_BENCH_MODE", raising=False)
    monkeypatch.delenv("LEMONCROW_DEV_MODE", raising=False)


# ---------------------------------------------------------------------------
# MODE-01 / MODE-02 — is_off() basic cases
# ---------------------------------------------------------------------------


def test_is_off_when_env_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """LEMONCROW_BENCH_MODE=off → is_off() returns True."""
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "off")
    from lemoncrow.bench.mode import is_off

    assert is_off() is True


def test_is_on_when_env_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """LEMONCROW_BENCH_MODE=on → is_off() returns False."""
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "on")
    from lemoncrow.bench.mode import is_off

    assert is_off() is False


def test_is_on_when_env_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Env var not set → default is bench-on → is_off() returns False."""
    # monkeypatch already deleted LEMONCROW_BENCH_MODE in autouse fixture
    from lemoncrow.bench.mode import is_off

    assert is_off() is False


def test_is_off_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    """LEMONCROW_BENCH_MODE=OFF (uppercase) → is_off() returns True."""
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "OFF")
    from lemoncrow.bench.mode import is_off

    assert is_off() is True


# ---------------------------------------------------------------------------
# MODE-03 — bootstrap() idempotency
# ---------------------------------------------------------------------------


def test_bootstrap_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """bootstrap() is idempotent: env changes after first call don't affect _mode."""
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "on")
    from lemoncrow.bench.mode import bootstrap

    bootstrap()
    assert _bm._mode == BenchMode.ON

    # Change env *after* first bootstrap; second call must be a no-op
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "off")
    bootstrap()

    # Must still be ON from first call
    assert _bm._mode == BenchMode.ON


# ---------------------------------------------------------------------------
# mode() accessor
# ---------------------------------------------------------------------------


def test_mode_returns_bench_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """mode() returns a BenchMode instance."""
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "on")
    from lemoncrow.bench.mode import mode

    result = mode()
    assert isinstance(result, BenchMode)
    assert result == BenchMode.ON


# ---------------------------------------------------------------------------
# MODE-05 — make_arm_env()
# ---------------------------------------------------------------------------


def test_make_arm_env_sets_root(tmp_path: Path) -> None:
    """make_arm_env(tmp_path, mode=BenchMode.OFF) sets LEMONCROW_ROOT and LEMONCROW_BENCH_MODE=off."""
    from lemoncrow.bench.mode import make_arm_env

    env = make_arm_env(tmp_path, mode=BenchMode.OFF)

    assert env["LEMONCROW_ROOT"] == str(tmp_path)
    assert env["LEMONCROW_BENCH_MODE"] == "off"


def test_make_arm_env_preserves_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """make_arm_env result contains pre-existing environment variables."""
    monkeypatch.setenv("CUSTOM_TEST_VAR_12345", "sentinel_value")
    from lemoncrow.bench.mode import make_arm_env

    env = make_arm_env(tmp_path, mode=BenchMode.ON)

    assert env.get("CUSTOM_TEST_VAR_12345") == "sentinel_value"


# ---------------------------------------------------------------------------
# MODE-04 — mcp_tool_visible_to_llm bench-off guard
# ---------------------------------------------------------------------------


def test_mcp_tools_hidden_bench_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """mcp_tool_visible_to_llm('compact') returns False when bench is off."""
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "off")
    from lemoncrow.core.environment import mcp_tool_visible_to_llm

    assert mcp_tool_visible_to_llm("compact") is False


def test_mcp_tools_visible_bench_on(monkeypatch: pytest.MonkeyPatch) -> None:
    """bench-on restores the normal hidden/public policy for MCP tools."""
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "on")
    from lemoncrow.core.environment import mcp_tool_visible_to_llm

    assert mcp_tool_visible_to_llm("compact") is False


def test_bench_off_overrides_dev_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """bench-off hides the public MCP surface (MODE-04)."""
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "off")
    from lemoncrow.core.environment import mcp_tool_visible_to_llm

    assert mcp_tool_visible_to_llm("compact") is False


# ---------------------------------------------------------------------------
# MODE-06 — MemoryRegistry._load() short-circuit
# ---------------------------------------------------------------------------


def test_memory_returns_empty_bench_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """MemoryRegistry._load() returns [] when bench is off (no adapter calls made)."""
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "off")
    from lemoncrow.pro.capabilities.cross_vendor_memory.registry import MemoryRegistry

    # Supply empty adapter list so no real I/O can happen
    registry = MemoryRegistry(adapters=[])
    result = registry._load()

    assert result == []


# ---------------------------------------------------------------------------
# MODE-06 — ContextCompressionCapability.compress_with_provenance() passthrough
# ---------------------------------------------------------------------------


def test_compression_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """compress_with_provenance returns passthrough result when bench is off.

    A passthrough CompressionResult has reduction_pct=0 and dropped=[].
    """
    monkeypatch.setenv("LEMONCROW_BENCH_MODE", "off")
    from lemoncrow.pro.capabilities.context_compression.capability import (
        ContextCompressionCapability,
    )

    ledger = MagicMock()  # not touched by bench-off early-return path
    cap = ContextCompressionCapability()
    result = cap.compress_with_provenance(ledger)

    assert result.reduction_pct == 0.0
    assert result.dropped == []
