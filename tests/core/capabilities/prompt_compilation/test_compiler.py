"""Tests for prompt_compilation.compiler (P1)."""

from __future__ import annotations

import random
from hashlib import sha256

import pytest

from lemoncrow.pro.capabilities.prompt_compilation.compiler import (
    BudgetTooSmall,
    compile_prompt,
)
from lemoncrow.pro.capabilities.prompt_compilation.models import (
    COUNTEREXAMPLE_METADATA_KEY,
    BlockKind,
    PromptBlock,
    Stability,
)


def _block(
    *,
    id: str,
    kind: BlockKind,
    content: str,
    stability: Stability,
    cacheable: bool = True,
    metadata: dict[str, object] | None = None,
) -> PromptBlock:
    return PromptBlock(
        id=id,
        kind=kind,
        content=content,
        stability=stability,
        cacheable=cacheable,
        metadata=metadata or {},
    )


def _sample_blocks() -> list[PromptBlock]:
    return [
        _block(
            id="task/current",
            kind=BlockKind.USER_TASK,
            content="Implement compiler and tests",
            stability=Stability.TURN,
        ),
        _block(
            id="tools/v1",
            kind=BlockKind.TOOL_SCHEMA,
            content='{"name":"search"}',
            stability=Stability.STATIC,
        ),
        _block(
            id="repo/summary",
            kind=BlockKind.REPO_SUMMARY,
            content="Architecture: gateway -> core -> infra",
            stability=Stability.SESSION,
        ),
        _block(
            id="scratch/main",
            kind=BlockKind.SCRATCHPAD,
            content="scratch notes",
            stability=Stability.VOLATILE,
        ),
        _block(
            id="sys/v1",
            kind=BlockKind.SYSTEM,
            content="You are LemonCrow.",
            stability=Stability.STATIC,
        ),
        _block(
            id="rb/team",
            kind=BlockKind.PLAYBOOK,
            content="Always preserve deterministic order",
            stability=Stability.BRANCH,
        ),
    ]


def test_stable_blocks_sort_before_volatile() -> None:
    compiled = compile_prompt(_sample_blocks())
    kinds = [block.kind for block in compiled.blocks]
    assert kinds == [
        BlockKind.TOOL_SCHEMA,
        BlockKind.SYSTEM,
        BlockKind.REPO_SUMMARY,
        BlockKind.PLAYBOOK,
        BlockKind.USER_TASK,
        BlockKind.SCRATCHPAD,
    ]
    assert compiled.prefix_end_index == 3


@pytest.mark.parametrize("seed", list(range(50)))
def test_shuffle_invariant(seed: int) -> None:
    blocks = _sample_blocks()
    expected = compile_prompt(blocks)
    random.Random(seed).shuffle(blocks)
    got = compile_prompt(blocks)
    assert got == expected


def test_prefix_hash_matches_golden_sha256() -> None:
    blocks = [
        _block(
            id="tools/v1",
            kind=BlockKind.TOOL_SCHEMA,
            content='{"name":"search"}',
            stability=Stability.STATIC,
        ),
        _block(
            id="sys/v1",
            kind=BlockKind.SYSTEM,
            content="System text",
            stability=Stability.STATIC,
        ),
        _block(
            id="task/v1",
            kind=BlockKind.USER_TASK,
            content="Solve task",
            stability=Stability.TURN,
        ),
    ]
    compiled = compile_prompt(blocks)
    assert compiled.prefix_end_index == 1
    assert compiled.stable_prefix_hash == "7628695eb2a0a8545a423f23551f8dc0eb8c06bcc967d7ab6a5c899fa23f1e4c"


def test_prefix_end_index_when_no_stable_blocks() -> None:
    blocks = [
        _block(
            id="task/v1",
            kind=BlockKind.USER_TASK,
            content="Solve task",
            stability=Stability.TURN,
        ),
        _block(
            id="scratch/v1",
            kind=BlockKind.SCRATCHPAD,
            content="volatile",
            stability=Stability.VOLATILE,
        ),
    ]
    compiled = compile_prompt(blocks)
    assert compiled.prefix_end_index == -1
    assert compiled.stable_prefix_tokens == 0
    assert compiled.stable_prefix_hash == sha256(b"").hexdigest()


def test_tail_budget_drops_low_utility_first() -> None:
    blocks = [
        _block(
            id="sys/v1",
            kind=BlockKind.SYSTEM,
            content="System text",
            stability=Stability.STATIC,
        ),
        _block(
            id="task/v1",
            kind=BlockKind.USER_TASK,
            content="Implement prompt compiler " * 20,
            stability=Stability.TURN,
        ),
        _block(
            id="diff/v1",
            kind=BlockKind.GIT_DIFF,
            content="diff --git a b " * 20,
            stability=Stability.TURN,
        ),
        _block(
            id="tool/success",
            kind=BlockKind.TOOL_RESULT,
            content="tests passed " * 20,
            stability=Stability.TURN,
            metadata={"is_error": False},
        ),
        _block(
            id="scratch/v1",
            kind=BlockKind.SCRATCHPAD,
            content="temp notes " * 20,
            stability=Stability.VOLATILE,
        ),
    ]

    baseline = compile_prompt(blocks)
    tail = baseline.blocks[baseline.prefix_end_index + 1 :]
    task_block = next(block for block in tail if block.kind is BlockKind.USER_TASK)
    diff_block = next(block for block in tail if block.kind is BlockKind.GIT_DIFF)
    tool_block = next(block for block in tail if block.id == "tool/success")
    budget = task_block.token_estimate + diff_block.token_estimate + tool_block.token_estimate

    constrained = compile_prompt(blocks, tail_budget_tokens=budget)
    constrained_ids = {block.id for block in constrained.blocks}
    assert "scratch/v1" not in constrained_ids
    assert {"task/v1", "diff/v1", "tool/success"} <= constrained_ids


def test_user_task_never_dropped() -> None:
    blocks = [
        _block(
            id="task/v1",
            kind=BlockKind.USER_TASK,
            content="critical user task " * 500,
            stability=Stability.TURN,
        ),
        _block(
            id="scratch/v1",
            kind=BlockKind.SCRATCHPAD,
            content="noise " * 500,
            stability=Stability.VOLATILE,
        ),
    ]
    task_tokens = blocks[0].token_estimate
    with pytest.raises(BudgetTooSmall):
        compile_prompt(blocks, tail_budget_tokens=max(1, task_tokens - 1))


def test_compile_prompt_accepts_turn_tool_result_counterexample() -> None:
    counterexample = _block(
        id="counterexample/typecheck/ok",
        kind=BlockKind.TOOL_RESULT,
        content='<counterexample check="typecheck"></counterexample>',
        stability=Stability.TURN,
        metadata={COUNTEREXAMPLE_METADATA_KEY: True},
    )

    compiled = compile_prompt([counterexample])

    assert compiled.blocks == (counterexample,)


def test_compile_prompt_rejects_counterexample_with_wrong_kind() -> None:
    counterexample = PromptBlock(
        id="counterexample/typecheck/bad-kind",
        kind=BlockKind.SYSTEM,
        content='<counterexample check="typecheck"></counterexample>',
        stability=Stability.STATIC,
        metadata={COUNTEREXAMPLE_METADATA_KEY: True},
    )

    with pytest.raises(ValueError, match="kind=tool_result"):
        compile_prompt([counterexample])


def test_compile_prompt_rejects_counterexample_with_wrong_stability() -> None:
    counterexample = PromptBlock(
        id="counterexample/typecheck/bad-stability",
        kind=BlockKind.TOOL_RESULT,
        content='<counterexample check="typecheck"></counterexample>',
        stability=Stability.BRANCH,
        metadata={COUNTEREXAMPLE_METADATA_KEY: True},
        stability_override_reason="test invalid counterexample stability",
    )

    with pytest.raises(ValueError, match="stability=turn"):
        compile_prompt([counterexample])
