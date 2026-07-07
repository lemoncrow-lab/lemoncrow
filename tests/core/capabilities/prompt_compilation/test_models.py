"""Tests for prompt_compilation.models (P0)."""

from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256
from typing import Any

import pytest

from atelier.core.capabilities.prompt_compilation.models import (
    COUNTEREXAMPLE_METADATA_KEY,
    DEFAULT_STABILITY,
    BlockKind,
    PromptBlock,
    Stability,
)


def _block(
    *,
    id: str = "test/block",
    kind: BlockKind = BlockKind.SYSTEM,
    content: str = "hello world",
    stability: Stability = Stability.STATIC,
    cacheable: bool = True,
    metadata: Mapping[str, Any] | None = None,
    stability_override_reason: str | None = None,
) -> PromptBlock:
    """Build a minimal valid PromptBlock with overrideable defaults."""
    return PromptBlock(
        id=id,
        kind=kind,
        content=content,
        stability=stability,
        cacheable=cacheable,
        metadata=metadata or {},
        stability_override_reason=stability_override_reason,
    )


class TestDefaultStabilityPerKind:
    def test_every_kind_has_a_default(self) -> None:
        for kind in BlockKind:
            assert kind in DEFAULT_STABILITY, f"{kind} missing from DEFAULT_STABILITY"

    def test_known_mappings(self) -> None:
        assert DEFAULT_STABILITY[BlockKind.TOOL_SCHEMA] == Stability.STATIC
        assert DEFAULT_STABILITY[BlockKind.REPO_SUMMARY] == Stability.SESSION
        assert DEFAULT_STABILITY[BlockKind.PLAYBOOK] == Stability.BRANCH
        assert DEFAULT_STABILITY[BlockKind.USER_TASK] == Stability.TURN
        assert DEFAULT_STABILITY[BlockKind.SCRATCHPAD] == Stability.VOLATILE


class TestOverrideRequiresReason:
    def test_override_without_reason_raises(self) -> None:
        with pytest.raises(ValueError, match="stability_override_reason"):
            _block(
                kind=BlockKind.SYSTEM,
                stability=Stability.SESSION,  # overrides STATIC default
            )

    def test_override_with_reason_succeeds(self) -> None:
        b = _block(
            kind=BlockKind.SYSTEM,
            stability=Stability.SESSION,
            stability_override_reason="test suite override",
        )
        assert b.stability == Stability.SESSION

    def test_no_override_no_reason_needed(self) -> None:
        b = _block(kind=BlockKind.SYSTEM, stability=Stability.STATIC)
        assert b.stability_override_reason is None


class TestVersionHashStableAcrossProcesses:
    KNOWN_HASH = sha256(b"hello world").hexdigest()

    def test_hash_matches_known_sha256(self) -> None:
        b = _block(content="hello world")
        assert b.version_hash == self.KNOWN_HASH

    def test_hash_differs_for_different_content(self) -> None:
        b1 = _block(content="hello world")
        b2 = _block(content="hello world!")
        assert b1.version_hash != b2.version_hash

    def test_hash_same_for_same_content(self) -> None:
        b1 = _block(content="stable content")
        b2 = _block(content="stable content")
        assert b1.version_hash == b2.version_hash


class TestVolatileForcesUncacheable:
    def test_turn_block_not_cacheable(self) -> None:
        b = _block(kind=BlockKind.USER_TASK, stability=Stability.TURN, cacheable=True)
        assert b.cacheable is False

    def test_volatile_block_not_cacheable(self) -> None:
        b = _block(
            kind=BlockKind.SCRATCHPAD,
            stability=Stability.VOLATILE,
            cacheable=True,
        )
        assert b.cacheable is False

    def test_static_block_remains_cacheable(self) -> None:
        b = _block(kind=BlockKind.SYSTEM, stability=Stability.STATIC, cacheable=True)
        assert b.cacheable is True


class TestIdValidation:
    def test_valid_ids_accepted(self) -> None:
        for id_ in ["tools/v1", "sys.core", "block:42", "a-b_c"]:
            b = _block(id=id_)
            assert b.id == id_

    def test_invalid_id_raises(self) -> None:
        with pytest.raises(ValueError, match="invalid"):
            _block(id="UPPER_CASE")


class TestEmptyContent:
    def test_empty_content_raises(self) -> None:
        with pytest.raises(ValueError, match="content"):
            _block(content="")


class TestCounterexampleMarker:
    def test_counterexample_metadata_sets_property(self) -> None:
        block = _block(
            kind=BlockKind.TOOL_RESULT,
            stability=Stability.TURN,
            metadata={COUNTEREXAMPLE_METADATA_KEY: True},
        )
        assert block.is_counterexample is True

    def test_regular_block_is_not_counterexample(self) -> None:
        assert _block().is_counterexample is False
