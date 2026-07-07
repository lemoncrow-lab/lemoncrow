"""Tests for RouteExecutionContract (WP-31)."""

from __future__ import annotations

import pytest

from atelier.core.capabilities.quality_router.capability import QualityRouterCapability
from atelier.core.capabilities.quality_router.execution_contract import (
    RouteExecutionContract,
    route_execution_contract,
)

# --------------------------------------------------------------------------- #
# Model tests                                                                 #
# --------------------------------------------------------------------------- #


def test_contract_is_pydantic_model() -> None:
    contract = route_execution_contract("claude")
    assert isinstance(contract, RouteExecutionContract)


def test_provider_enforced_always_disabled() -> None:
    """provider_enforced mode must never be the active mode for any host."""
    for host in ("claude", "codex", "copilot", "opencode", "antigravity"):
        contract = route_execution_contract(host)
        assert contract.mode != "provider_enforced", f"host={host!r}: provider_enforced must not be the active mode"
        assert contract.provider_enforced_disabled is True, f"host={host!r}: provider_enforced_disabled must be True"


def test_unknown_host_raises() -> None:
    with pytest.raises(ValueError, match="Unknown host"):
        route_execution_contract("unknown_host")


# --------------------------------------------------------------------------- #
# Claude hook enforcement                                                     #
# --------------------------------------------------------------------------- #


def test_claude_wrapper_enforced() -> None:
    contract = route_execution_contract("claude")
    assert contract.host == "claude"
    assert contract.mode == "wrapper_enforced"
    assert contract.can_block_start is True
    assert contract.can_require_verification is True
    assert contract.fallback_mode == "advisory"


def test_claude_cannot_force_model() -> None:
    """Model selection stays host-native even for Claude."""
    contract = route_execution_contract("claude")
    assert contract.can_force_model is False
    assert "model" in contract.host_native_owner


# --------------------------------------------------------------------------- #
# Codex wrapper enforcement                                                   #
# --------------------------------------------------------------------------- #


def test_codex_wrapper_enforced() -> None:
    contract = route_execution_contract("codex")
    assert contract.host == "codex"
    assert contract.mode == "wrapper_enforced"
    assert contract.can_block_start is True
    assert contract.can_require_verification is True
    assert contract.fallback_mode == "advisory"


def test_codex_unsupported_has_hook_note() -> None:
    contract = route_execution_contract("codex")
    assert "hook_enforced" in contract.unsupported_reason


# --------------------------------------------------------------------------- #
# Copilot advisory mode                                                       #
# --------------------------------------------------------------------------- #


def test_copilot_advisory() -> None:
    contract = route_execution_contract("copilot")
    assert contract.host == "copilot"
    assert contract.mode == "advisory"
    assert contract.can_block_start is False
    assert contract.can_force_model is False
    assert contract.can_require_verification is False
    assert contract.fallback_mode == "advisory"


def test_copilot_host_native_owner_covers_model_and_edit() -> None:
    contract = route_execution_contract("copilot")
    owner_parts = {p.strip() for p in contract.host_native_owner.split(",")}
    assert "model" in owner_parts
    assert "edit" in owner_parts


# --------------------------------------------------------------------------- #
# opencode wrapper enforcement                                                #
# --------------------------------------------------------------------------- #


def test_opencode_wrapper_enforced() -> None:
    contract = route_execution_contract("opencode")
    assert contract.host == "opencode"
    assert contract.mode == "wrapper_enforced"
    assert contract.can_block_start is True


# --------------------------------------------------------------------------- #
# Gemini advisory mode                                                        #
# --------------------------------------------------------------------------- #


def test_antigravity_advisory() -> None:
    contract = route_execution_contract("antigravity")
    assert contract.host == "antigravity"
    assert contract.mode == "advisory"
    assert contract.can_block_start is False
    assert contract.fallback_mode == "advisory"


# --------------------------------------------------------------------------- #
# Supported tiers                                                             #
# --------------------------------------------------------------------------- #


def test_all_hosts_support_standard_tiers() -> None:
    expected = {"cheap", "mid", "premium", "deterministic"}
    for host in ("claude", "codex", "copilot", "opencode", "antigravity"):
        contract = route_execution_contract(host)
        assert (
            set(contract.supported_tiers) == expected
        ), f"host={host!r}: unexpected tiers {contract.supported_tiers!r}"


# --------------------------------------------------------------------------- #
# QualityRouterCapability.contract() delegation                               #
# --------------------------------------------------------------------------- #


def test_capability_contract_delegates_correctly(tmp_path: pytest.fixture) -> None:  # type: ignore[valid-type]
    """QualityRouterCapability.contract() must return the same result as route_execution_contract."""
    from unittest.mock import MagicMock

    store = MagicMock()
    capability = QualityRouterCapability(store=store, repo_root=tmp_path)

    contract = capability.contract("codex")
    expected = route_execution_contract("codex")
    assert contract == expected


def test_capability_contract_propagates_unknown_host(tmp_path: pytest.fixture) -> None:  # type: ignore[valid-type]
    from unittest.mock import MagicMock

    store = MagicMock()
    capability = QualityRouterCapability(store=store, repo_root=tmp_path)
    with pytest.raises(ValueError):
        capability.contract("unknown")
