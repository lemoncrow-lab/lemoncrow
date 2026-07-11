"""Token log-probabilities and a model-free structural-entropy fallback.

This module backs perplexity/entropy-guided context compression (T10 + T11).
It exposes two scorers with deliberately different cost/fidelity tradeoffs:

``logprobs(text, model)``
    Real per-token log-probabilities from the configured backend
    (``openai`` / ``litellm`` passthrough via the ``logprobs`` echo trick).
    Returns ``None`` whenever no model/network is available
    (``LEMONCROW_LLM_BACKEND=none``, the default) so callers can fall back
    cleanly. Never raises for the disabled-backend case.

``chunk_entropy(text)``
    A purely structural scorer: Shannon entropy of the token distribution
    combined with a token-rarity (inverse-frequency) signal. No model, no
    network, fully deterministic. High-signal, information-dense spans
    (e.g. a logic-heavy function body) score higher than low-signal,
    repetitive spans (e.g. a comment-only block or boilerplate).

The entropy fallback is the headless v1 default: with the LLM backend off,
the whole compression pipeline still works using only ``chunk_entropy``.
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter

__all__ = ["chunk_entropy", "logprobs", "token_surprisals"]

# Word/identifier-ish tokens plus standalone punctuation/operators. Splitting
# this way keeps the structural scorer language-agnostic while still treating
# operators and symbols (which carry real code signal) as distinct tokens.
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|[^\sA-Za-z0-9_]")


def _backend() -> str:
    return os.environ.get("LEMONCROW_LLM_BACKEND", "none").lower().strip()


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


def token_surprisals(text: str) -> list[float]:
    """Per-token self-information (``-log2 p(token)``) under the text's own distribution.

    This is the structural analogue of model log-probabilities: rarer tokens
    inside the span carry more surprisal (information). Returns one value per
    token in order; an empty list for empty/whitespace-only input.
    """
    tokens = _tokenize(text)
    if not tokens:
        return []
    total = len(tokens)
    counts = Counter(tokens)
    return [-math.log2(counts[tok] / total) for tok in tokens]


def chunk_entropy(text: str) -> float:
    """Model-free information-density score for a span of text/code.

    Combines two structural signals:

    * **Shannon entropy** of the token distribution (in bits): how varied /
      unpredictable the token stream is. Repetitive boilerplate has low
      entropy; dense, varied logic has high entropy.
    * **Token rarity**: the mean self-information of the *distinct* token
      vocabulary, rewarding spans built from many uncommon tokens rather
      than a few repeated ones.

    The result is non-negative and unbounded above, but in practice grows
    with both vocabulary size and per-token surprisal. It is deterministic
    and requires no model or network. Empty / whitespace-only input scores
    ``0.0``.
    """
    tokens = _tokenize(text)
    if not tokens:
        return 0.0
    total = len(tokens)
    counts = Counter(tokens)

    # Shannon entropy of the token distribution (bits).
    entropy = -sum((c / total) * math.log2(c / total) for c in counts.values())

    # Mean self-information of the distinct vocabulary (token rarity): a span
    # made of many low-frequency tokens is more information-dense than one
    # dominated by a handful of repeated tokens.
    rarity = sum(-math.log2(c / total) for c in counts.values()) / len(counts)

    # Light length term so a long dense block is not tied with a tiny one of
    # identical per-token entropy; log-scaled to avoid runaway length bias.
    length_factor = math.log2(total + 1)

    return entropy + rarity + length_factor


def logprobs(text: str, model: str | None = None) -> list[float] | None:
    """Return per-token log-probabilities for *text*, or ``None`` if unavailable.

    Behaviour by ``LEMONCROW_LLM_BACKEND``:

    * ``none`` (default) — returns ``None`` immediately; never touches the
      network. Callers should fall back to :func:`chunk_entropy` /
      :func:`token_surprisals`.
    * ``openai`` / ``openai_compatible`` / ``litellm`` — requests the model
      to echo *text* with ``logprobs`` enabled and extracts the per-token
      values. Returns ``None`` (rather than raising) on any provider/transport
      failure so the structural fallback always remains available.
    * ``ollama`` — no stable logprobs contract for the echo trick here;
      returns ``None`` so callers use the structural fallback.
    """
    backend = _backend()
    if backend == "none":
        return None
    if not text.strip():
        return []
    try:
        if backend in ("openai", "openai_compatible"):
            return _openai_logprobs(text, model)
        if backend == "litellm":
            return _litellm_logprobs(text, model)
    except Exception:  # noqa: BLE001 - any backend failure -> structural fallback
        return None
    # ollama / unknown backends: no logprobs contract -> structural fallback.
    return None


def _extract_logprobs(response: object) -> list[float] | None:
    """Pull per-token logprob values from an OpenAI-shaped completion response."""
    try:
        choice = response.choices[0]  # type: ignore[attr-defined]
        content = choice.logprobs.content  # type: ignore[attr-defined]
    except (AttributeError, IndexError, TypeError):
        return None
    values: list[float] = []
    for entry in content or []:
        lp = getattr(entry, "logprob", None)
        if lp is None and isinstance(entry, dict):
            lp = entry.get("logprob")
        if lp is not None:
            values.append(float(lp))
    return values or None


def _openai_logprobs(text: str, model: str | None) -> list[float] | None:
    from lemoncrow.infra.internal_llm.openai_client import _resolve_client, _resolve_model

    client = _resolve_client()
    chosen = model or _resolve_model()
    response = client.chat.completions.create(
        model=chosen,
        messages=[
            {"role": "system", "content": "Echo the user's text back verbatim."},
            {"role": "user", "content": text},
        ],
        logprobs=True,
        max_tokens=max(16, len(text)),
        temperature=0.0,
    )
    return _extract_logprobs(response)


def _litellm_logprobs(text: str, model: str | None) -> list[float] | None:
    from lemoncrow.infra.internal_llm.litellm_client import _litellm_module, _resolve_model

    litellm = _litellm_module()
    chosen = _resolve_model(model)
    response = litellm.completion(
        model=chosen,
        messages=[
            {"role": "system", "content": "Echo the user's text back verbatim."},
            {"role": "user", "content": text},
        ],
        logprobs=True,
        max_tokens=max(16, len(text)),
        temperature=0.0,
    )
    return _extract_logprobs(response)
