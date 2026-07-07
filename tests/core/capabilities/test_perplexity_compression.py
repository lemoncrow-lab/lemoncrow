"""Headless tests for perplexity/entropy-guided compression (T10 + T11).

All tests run with the structural-entropy fallback (no model, no network):
the internal LLM backend is forced to ``none`` so ``logprobs`` returns ``None``
and every scorer falls back to Shannon-entropy / token-rarity. The final test
asserts that backend=none never touches the network.
"""

from __future__ import annotations

import pytest

from atelier.core.capabilities.budget_optimizer.optimizer import (
    ContextBlock,
    PromptBudgetOptimizer,
)
from atelier.core.capabilities.code_context.ppl_rank import (
    rank_code_chunks,
    split_code_chunks,
)
from atelier.core.capabilities.context_compression.perplexity_pruning import (
    prune_block,
)
from atelier.infra.internal_llm import chunk_entropy, logprobs


@pytest.fixture(autouse=True)
def _backend_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the disabled backend so the entropy fallback path is exercised."""
    monkeypatch.setenv("ATELIER_LLM_BACKEND", "none")


# A dense, logic-heavy function: many distinct identifiers, operators, branches.
DENSE_FUNCTION = """\
def solve(grid, weights, budget):
    selected = []
    remaining = budget
    for cost, item, score in sorted(zip(weights, grid, range(len(grid)))):
        if cost <= remaining and score > 0.3:
            selected.append(item)
            remaining -= cost
        elif remaining <= 0:
            break
    return selected, remaining
"""

# A comment-only block: low entropy, highly repetitive natural-language filler.
COMMENT_BLOCK = """\
# this is a comment
# this is a comment
# this is a comment
# this is a comment
# this is a comment
# this is a comment
# this is a comment
"""


def test_entropy_ranks_dense_function_above_comment_block() -> None:
    """The structural entropy scorer must score dense logic above filler."""
    assert chunk_entropy(DENSE_FUNCTION) > chunk_entropy(COMMENT_BLOCK)


def test_logprobs_none_when_backend_disabled() -> None:
    """With backend=none, logprobs returns None so callers use entropy."""
    assert logprobs(DENSE_FUNCTION) is None


def test_split_code_chunks_at_function_boundaries() -> None:
    code = DENSE_FUNCTION + "\n" + COMMENT_BLOCK + "\ndef helper(x):\n    return x + 1\n"
    chunks = split_code_chunks(code)
    # solve(...) and helper(...) are distinct boundaries -> at least 2 chunks.
    assert len(chunks) >= 2
    joined = "\n".join(text for text, _s, _e in chunks)
    assert "def solve" in joined
    assert "def helper" in joined
    # Line ranges are 1-based and ordered.
    starts = [s for _t, s, _e in chunks]
    assert starts == sorted(starts)
    assert starts[0] == 1


def test_rank_code_chunks_dense_outranks_comment() -> None:
    """T10: the dense function chunk must earn higher utility than filler."""
    code = COMMENT_BLOCK + "\n" + DENSE_FUNCTION
    chunks = rank_code_chunks(code, instruction="")
    assert chunks
    assert all(0.0 <= c.utility <= 1.0 for c in chunks)
    assert all(c.source == "entropy" for c in chunks)
    dense = max(chunks, key=lambda c: c.utility)
    assert "def solve" in dense.text


def test_rank_code_chunks_instruction_relevance_boosts_match() -> None:
    """Instruction overlap raises the utility of the matching chunk."""
    code = (
        "def parse_config(path):\n    data = read(path)\n    return data\n\n"
        "def render_html(template):\n    return template.format()\n"
    )
    chunks = rank_code_chunks(code, instruction="parse the config path", relevance_weight=0.8)
    top = max(chunks, key=lambda c: c.utility)
    assert "parse_config" in top.text
    assert top.relevance > 0.0


def test_knapsack_honors_external_ppl_utilities_under_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T10 -> budget: external utilities flip selection when the flag is on."""
    blocks = [
        ContextBlock("a", "x", token_cost=50, utility=0.9, source="s"),
        ContextBlock("b", "y", token_cost=50, utility=0.1, source="s"),
    ]
    opt = PromptBudgetOptimizer(diversity_bonus=0.0)
    monkeypatch.setenv("ATELIER_PERPLEXITY_COMPRESSION", "1")
    plan = opt.solve(blocks, token_budget=50, utility_source={"a": 0.1, "b": 0.95})
    assert [b.id for b in plan.selected] == ["b"]


def test_knapsack_flag_off_preserves_static_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Flag off: utility_source is ignored, static utilities win (a over b)."""
    blocks = [
        ContextBlock("a", "x", token_cost=50, utility=0.9, source="s"),
        ContextBlock("b", "y", token_cost=50, utility=0.1, source="s"),
    ]
    opt = PromptBudgetOptimizer(diversity_bonus=0.0)
    monkeypatch.delenv("ATELIER_PERPLEXITY_COMPRESSION", raising=False)
    plan = opt.solve(blocks, token_budget=50, utility_source={"a": 0.1, "b": 0.95})
    assert [b.id for b in plan.selected] == ["a"]


def test_pruning_reduces_tokens_keeping_high_entropy_spans() -> None:
    """T11: pruning trims low-signal filler but keeps the dense logic span."""
    block = COMMENT_BLOCK + DENSE_FUNCTION
    result = prune_block(block, target_tokens=result_target(block))
    assert result.pruned_tokens < result.original_tokens
    assert result.saved_tokens > 0
    assert result.source == "entropy"
    # The dense computational lines survive; the repeated comment filler is cut.
    assert "selected.append(item)" in result.text
    assert result.text.count("# this is a comment") < COMMENT_BLOCK.count("# this is a comment")


def test_pruning_preserves_keystone_control_flow_lines() -> None:
    """Control-flow / return lines are pinned even under an aggressive target."""
    result = prune_block(DENSE_FUNCTION, target_tokens=1)
    assert "return selected, remaining" in result.text
    assert any(line.strip().startswith("if ") for line in result.text.splitlines())


def test_pruning_noop_when_already_under_budget() -> None:
    small = "def f(x):\n    return x\n"
    result = prune_block(small, target_tokens=10_000)
    assert result.text == small
    assert result.dropped_lines == []


def result_target(block: str) -> int:
    """Half the block's token budget — forces a real prune."""
    from atelier.core.capabilities.context_compression.perplexity_pruning import (
        _token_count,
    )

    return max(1, _token_count(block) // 2)


def test_backend_none_never_touches_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """backend=none must short-circuit before any client/network code runs.

    We poison both provider client factories: if logprobs tried to build a
    client under backend=none it would raise. It must return None instead.
    """
    import atelier.infra.internal_llm.litellm_client as lc
    import atelier.infra.internal_llm.openai_client as oc

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("network/client accessed under backend=none")

    monkeypatch.setattr(oc, "_resolve_client", _boom)
    monkeypatch.setattr(lc, "_litellm_module", _boom)
    monkeypatch.setenv("ATELIER_LLM_BACKEND", "none")

    assert logprobs(DENSE_FUNCTION) is None
    # The full T10/T11 pipeline must also stay model-free under backend=none.
    chunks = rank_code_chunks(DENSE_FUNCTION, instruction="solve budget")
    assert all(c.source == "entropy" for c in chunks)
    pruned = prune_block(COMMENT_BLOCK + DENSE_FUNCTION, target_tokens=20)
    assert pruned.source == "entropy"
