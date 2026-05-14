"""Retriever — score and rank ReasonBlocks against a task context.

Scoring (deterministic, no LLM):

    Without vector search (default):
        0.30 domain / task_type match
        0.25 file / tool / module overlap
        0.20 trigger phrase match
        0.15 failure / error signal match
        0.10 success history (capped)

    With vector search (ATELIER_VECTOR_SEARCH_ENABLED=true):
        0.30 domain / task_type match
        0.25 file / tool / module overlap
        0.20 trigger phrase match
        0.15 failure / error signal match
        0.05 success history (capped)
        0.05 vector cosine similarity

Quarantined blocks are excluded. Deprecated blocks are excluded by default.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import TypeVar

import tiktoken
from datasketch import MinHash, MinHashLSH

from atelier.core.capabilities.archival_recall.ranking import rank_archival_passages
from atelier.core.foundation.memory_models import ArchivalPassage
from atelier.core.foundation.models import ReasonBlock
from atelier.core.foundation.renderer import render_block_for_agent
from atelier.core.foundation.store import ContextStore

T = TypeVar("T")
_DEFAULT_TOKEN_BUDGET = 2000
_DEDUP_THRESHOLD = 0.75
_MINHASH_PERMUTATIONS = 128
_MIN_DEDUP_TOKENS = 5


@dataclass
class TaskContext:
    """Inputs used to score reasoning relevance for a single agent task."""

    task: str
    domain: str | None = None
    task_type: str | None = None
    files: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def text_blob(self) -> str:
        parts = [self.task]
        if self.task_type:
            parts.append(self.task_type)
        parts.extend(self.errors)
        parts.extend(self.tools)
        return " ".join(parts).lower()


@dataclass
class ScoredBlock:
    block: ReasonBlock
    score: float
    breakdown: dict[str, float]


@dataclass(frozen=True)
class RecalledPassageSummary:
    id: str
    source: str
    score: float


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    """Count tokens with the model-agnostic cl100k_base encoding."""
    return len(_encoding().encode(text))


def count_reasonblock_tokens(block: ReasonBlock) -> int:
    """Count tokens for the compact injected representation of one ReasonBlock."""
    return count_tokens(render_block_for_agent(block))


def passage_in_agent_scope(passage: ArchivalPassage, requested_agent_id: str) -> bool:
    """Return whether a passage may be injected for the requested agent."""
    return passage.agent_id == requested_agent_id or "agent:any" in passage.tags


def filter_scoped_passages(passages: Sequence[ArchivalPassage], *, requested_agent_id: str) -> list[ArchivalPassage]:
    """Keep only same-agent passages and explicit global lessons."""
    return [passage for passage in passages if passage_in_agent_scope(passage, requested_agent_id)]


def render_memory_for_agent(passages: Sequence[ArchivalPassage]) -> str:
    """Render recalled archival passages for context injection."""
    if not passages:
        return ""
    out = ["<memory>"]
    for passage in passages:
        source = passage.source_ref or passage.source
        out.append("")
        out.append(f"Passage: {passage.id}  [{source}]")
        out.append(passage.text.strip())
    out.append("</memory>")
    return "\n".join(out) + "\n"


def summarize_recalled_passages(passages: Sequence[ArchivalPassage], *, query: str) -> list[dict[str, str | float]]:
    """Return compact metadata for passages injected into context."""
    scores = {
        item.passage.id: item.score
        for item in rank_archival_passages(query=query, passages=list(passages), top_k=len(passages))
    }
    return [
        {
            "id": passage.id,
            "source": passage.source_ref or passage.source,
            "score": round(float(scores.get(passage.id, 0.0)), 6),
        }
        for passage in passages
    ]


def _dedup_text(block: ReasonBlock) -> str:
    return " ".join([*block.dead_ends, *block.procedure])


def _dedup_tokens(block: ReasonBlock) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", _dedup_text(block).lower()))


def _minhash(tokens: set[str]) -> MinHash:
    signature = MinHash(num_perm=_MINHASH_PERMUTATIONS)
    for token in sorted(tokens):
        signature.update(token.encode("utf-8"))
    return signature


def _jaccard_tokens(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def deduplicate_by_reasonblock(
    items: Sequence[T],
    block_getter: Callable[[T], ReasonBlock],
    *,
    threshold: float = _DEDUP_THRESHOLD,
) -> list[T]:
    """Drop near-duplicate ReasonBlocks, keeping the first/highest-ranked item."""
    if len(items) < 2:
        return list(items)

    lsh = MinHashLSH(threshold=threshold, num_perm=_MINHASH_PERMUTATIONS)
    kept: list[T] = []
    kept_tokens: dict[str, set[str]] = {}

    for item in items:
        block = block_getter(item)
        tokens = _dedup_tokens(block)
        if len(tokens) < _MIN_DEDUP_TOKENS:
            kept.append(item)
            continue
        signature = _minhash(tokens)
        matches = lsh.query(signature)
        if any(_jaccard_tokens(tokens, kept_tokens[key]) >= threshold for key in matches):
            continue

        key = f"{len(kept)}:{block.id}"
        lsh.insert(key, signature)
        kept_tokens[key] = tokens
        kept.append(item)

    return kept


def pack_by_reasonblock_token_budget(
    items: Sequence[T],
    block_getter: Callable[[T], ReasonBlock],
    *,
    limit: int,
    token_budget: int | None,
) -> list[T]:
    """Greedily pack highest-ranked ReasonBlocks until the token budget is reached."""
    packed: list[T] = []
    tokens_used = 0

    for item in items:
        if len(packed) >= limit:
            break
        block_tokens = count_reasonblock_tokens(block_getter(item))
        if token_budget is not None and token_budget >= 0:
            if tokens_used + block_tokens > token_budget and packed:
                continue
            if token_budget == 0 and not packed:
                break
        packed.append(item)
        tokens_used += block_tokens

    return packed


def deduplicate_scored_blocks(
    scored: Sequence[ScoredBlock],
    *,
    threshold: float = _DEDUP_THRESHOLD,
) -> list[ScoredBlock]:
    return deduplicate_by_reasonblock(scored, lambda item: item.block, threshold=threshold)


# --------------------------------------------------------------------------- #
# Scoring helpers                                                             #
# --------------------------------------------------------------------------- #


def _domain_score(block: ReasonBlock, ctx: TaskContext) -> float:
    if ctx.domain and block.domain == ctx.domain:
        return 1.0
    if ctx.domain and block.domain.startswith(ctx.domain.split(".")[0]):
        return 0.5
    if ctx.task_type and ctx.task_type in block.task_types:
        return 0.7
    return 0.0


def _pattern_overlap(patterns: list[str], items: list[str]) -> float:
    if not patterns or not items:
        return 0.0
    matched = 0
    for it in items:
        for pat in patterns:
            if fnmatch.fnmatch(it, pat):
                matched += 1
                break
    return matched / len(items)


def _scope_score(block: ReasonBlock, ctx: TaskContext) -> float:
    f = _pattern_overlap(block.file_patterns, ctx.files)
    t = _pattern_overlap(block.tool_patterns, ctx.tools)
    if not block.file_patterns and not block.tool_patterns:
        return 0.0
    n = (1 if block.file_patterns else 0) + (1 if block.tool_patterns else 0)
    return (f + t) / n


def _trigger_score(block: ReasonBlock, ctx: TaskContext) -> float:
    if not block.triggers:
        return 0.0
    blob = ctx.text_blob()
    matched = sum(1 for t in block.triggers if t.lower() in blob)
    return min(1.0, matched / max(1, len(block.triggers)))


def _failure_signal_score(block: ReasonBlock, ctx: TaskContext) -> float:
    if not block.failure_signals or not ctx.errors:
        return 0.0
    err_blob = " ".join(ctx.errors).lower()
    matched = sum(1 for f in block.failure_signals if f.lower() in err_blob)
    return min(1.0, matched / max(1, len(block.failure_signals)))


def _success_history_score(block: ReasonBlock) -> float:
    total = block.success_count + block.failure_count
    if total == 0:
        return 0.5  # neutral prior
    return block.success_count / total


# --------------------------------------------------------------------------- #
# Public API                                                                  #
# --------------------------------------------------------------------------- #

# Weights when vector search is disabled (default)
WEIGHTS: dict[str, float] = {
    "domain": 0.30,
    "scope": 0.25,
    "triggers": 0.20,
    "failures": 0.15,
    "history": 0.10,
}

# Weights when vector search is enabled (history redistributed to vector)
WEIGHTS_WITH_VECTOR: dict[str, float] = {
    "domain": 0.30,
    "scope": 0.25,
    "triggers": 0.20,
    "failures": 0.15,
    "history": 0.05,
    "vector": 0.05,
}


def score_block(
    block: ReasonBlock,
    ctx: TaskContext,
    *,
    vector_score: float | None = None,
    use_vector_weights: bool | None = None,
) -> ScoredBlock:
    """Score a ReasonBlock against a task context.

    Args:
        block: The candidate ReasonBlock.
        ctx: The current task context.
        vector_score: Pre-computed cosine similarity in [0, 1].  When
            provided and ``use_vector_weights`` is True (or auto-detected
            via ``ATELIER_VECTOR_SEARCH_ENABLED``), the vector component
            is included in the score.
        use_vector_weights: Override the env-var check.  Pass True/False
            explicitly or leave as None to auto-detect from env.
    """
    from atelier.infra.storage.vector import is_vector_enabled

    _use_vec = is_vector_enabled() if use_vector_weights is None else use_vector_weights
    w = WEIGHTS_WITH_VECTOR if _use_vec else WEIGHTS

    breakdown: dict[str, float] = {
        "domain": _domain_score(block, ctx) * w["domain"],
        "scope": _scope_score(block, ctx) * w["scope"],
        "triggers": _trigger_score(block, ctx) * w["triggers"],
        "failures": _failure_signal_score(block, ctx) * w["failures"],
        "history": _success_history_score(block) * w["history"],
    }
    if _use_vec:
        raw_vs = vector_score if vector_score is not None else 0.0
        breakdown["vector"] = max(0.0, min(1.0, raw_vs)) * w["vector"]

    return ScoredBlock(block=block, score=sum(breakdown.values()), breakdown=breakdown)


def retrieve(
    store: ContextStore,
    ctx: TaskContext,
    *,
    limit: int = 5,
    min_score: float = 0.15,
    include_deprecated: bool = False,
    vector_scores: dict[str, float] | None = None,
    use_vector_weights: bool | None = None,
    dedup: bool = True,
    token_budget: int | None = _DEFAULT_TOKEN_BUDGET,
) -> list[ScoredBlock]:
    """Return top-N relevant ReasonBlocks for a task context.

    Args:
        store: The backing store.
        ctx: The current task context.
        limit: Maximum number of results.
        min_score: Minimum score to include a block.
        include_deprecated: Include deprecated blocks.
        vector_scores: Mapping of block_id -> cosine similarity score.
            Pass pre-computed scores from a vector search; they will be
            incorporated when vector scoring is enabled.
        use_vector_weights: Override the env-var check for weight selection.
        dedup: Drop near-duplicate blocks using MinHash LSH over dead-ends
            and procedure text, keeping the highest-ranked block.
        token_budget: Greedy-pack compact rendered blocks under this token
            budget. Pass None to disable budget packing.
    """
    candidates: list[ReasonBlock] = []

    # 1. FTS5 keyword pre-filter using the task + errors as the query.
    query = " ".join([ctx.task, *ctx.errors]).strip()
    if query:
        candidates.extend(store.search_blocks(query, limit=50))

    # 2. Always include all active blocks for the domain so triggers/scope
    #    can match even if FTS misses them.
    if ctx.domain:
        candidates.extend(store.list_blocks(domain=ctx.domain))

    # Deduplicate by id while preserving order.
    seen: set[str] = set()
    unique: list[ReasonBlock] = []
    for b in candidates:
        if b.id in seen:
            continue
        if b.status == "quarantined":
            continue
        if b.status == "deprecated" and not include_deprecated:
            continue
        seen.add(b.id)
        unique.append(b)

    scored = [
        score_block(
            b,
            ctx,
            vector_score=(vector_scores or {}).get(b.id),
            use_vector_weights=use_vector_weights,
        )
        for b in unique
    ]
    scored = [s for s in scored if s.score >= min_score]
    scored.sort(key=lambda s: s.score, reverse=True)
    if dedup:
        scored = deduplicate_scored_blocks(scored)
    return pack_by_reasonblock_token_budget(
        scored,
        lambda item: item.block,
        limit=limit,
        token_budget=token_budget,
    )
