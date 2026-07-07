"""Tests for the T12 goal-conditioned per-line skimmer.

Headless: no real embedding backend is required. The lexical path is exercised
directly; the embedding path is driven through an injected fake ranker so the
blend and the lexical-fallback degrade both run deterministically offline.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field

import pytest

from atelier.core.capabilities.repo_map.budget import count_tokens
from atelier.core.capabilities.scoped_context import ScopedContextCapability, Subtask
from atelier.core.capabilities.scoped_context.line_skimmer import (
    LineSkimmer,
    build_goal_text,
    is_line_skim_enabled,
    is_structural_anchor,
    skim_chunks,
)

# A chunk body mixing goal-relevant lines, irrelevant noise, and anchors.
_CHUNK_BODY = """\
import os
import logging


class PaymentGateway:
    def __init__(self, client):
        self.client = client
        self.audit_log = []

    def charge_card(self, amount, currency):
        # validate the payment amount before charging the card
        if amount <= 0:
            raise ValueError("amount must be positive")
        receipt = self.client.charge(amount, currency)
        self.audit_log.append(receipt)
        return receipt

    def render_dashboard_widget(self):
        colors = ["red", "green", "blue"]
        layout = compute_grid_layout(colors)
        return layout
"""

_GOAL = "fix the charge card payment amount validation"


@dataclass
class _FakeRecord:
    file_path: str
    symbol_name: str
    kind: str = "function"
    language: str = "python"
    qualified_name: str = ""
    signature: str = ""
    snippet: str = ""
    score: float | None = None
    provenance: str = "local"
    commit_sha: str = ""


class _FakeEngine:
    """Minimal engine exposing the search_symbols subset pull() calls."""

    def __init__(self, records: list[_FakeRecord]) -> None:
        self._records = records
        self.index_version = 0

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        mode: str = "auto",
        snippet: str = "none",
        snippet_lines: int = 8,
        file_glob: str | None = None,
        provenance_filter: str | None = None,
        **_: object,
    ) -> list[_FakeRecord]:
        recs = self._records
        if file_glob is not None:
            recs = [r for r in recs if r.file_path == file_glob or fnmatch.fnmatch(r.file_path, file_glob)]
        if provenance_filter is not None:
            recs = [r for r in recs if r.provenance == provenance_filter]
        return recs[:limit]

    def _current_index_version(self) -> int:
        return self.index_version


@dataclass
class _FakeChunk:
    snippet: str
    path: str = "src/pay.py"
    symbol: str = "charge_card"


@dataclass
class _FakeRanker:
    """Deterministic offline ranker: bag-of-words vectors over a fixed vocab."""

    available: bool = True
    vocab: tuple[str, ...] = ("charge", "card", "payment", "amount", "validate", "validation")
    calls: list[str] = field(default_factory=list)

    def _vec(self, text: str) -> list[float]:
        low = text.lower()
        return [1.0 if term in low else 0.0 for term in self.vocab]

    def embed_query(self, text: str) -> list[float]:
        self.calls.append(text)
        return self._vec(text)

    def embed_text(self, text: str) -> list[float]:
        return self._vec(text)


def _relevant_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.strip()]


# --- unit: helpers ----------------------------------------------------------


def test_is_line_skim_enabled_default_off() -> None:
    assert is_line_skim_enabled(env={}) is False
    assert is_line_skim_enabled(env={"ATELIER_LINE_SKIM": "1"}) is True
    assert is_line_skim_enabled(env={"ATELIER_LINE_SKIM": "0"}) is False


def test_build_goal_text_combines_fields() -> None:
    sub = Subtask(
        description="fix validation",
        keywords=["charge", "card"],
        affected_paths=["src/pay.py"],
    )
    goal = build_goal_text(sub)
    assert "fix validation" in goal
    assert "charge" in goal and "card" in goal
    assert "src/pay.py" in goal


def test_structural_anchor_detection() -> None:
    assert is_structural_anchor("def charge_card(self):")
    assert is_structural_anchor("    class Foo:")
    assert is_structural_anchor("@decorator")
    assert is_structural_anchor("import os")
    assert not is_structural_anchor("        colors = ['red']")
    assert not is_structural_anchor("")


# --- unit: lexical skim (no embedding model) --------------------------------


def test_lexical_skim_keeps_relevant_and_anchors_drops_noise() -> None:
    skimmer = LineSkimmer(ranker=None)  # no embedding model -> lexical fallback
    result = skimmer.skim(_CHUNK_BODY, _GOAL)

    assert result.used_embedding is False
    assert result.changed
    assert result.dropped_lines > 0
    assert result.kept_lines == result.total_lines - result.dropped_lines

    kept = _relevant_lines(result.text)
    # Structural anchors always survive so the output stays parseable.
    assert "class PaymentGateway:" in kept
    assert "def charge_card(self, amount, currency):" in kept
    assert "def render_dashboard_widget(self):" in kept
    # Goal-relevant lines survive.
    assert any("amount must be positive" in line for line in kept)
    assert any("validate the payment amount" in line for line in kept)
    # Irrelevant noise beyond an anchor's neighbor window is dropped. The colors
    # line is the immediate neighbor of the render_dashboard_widget anchor (and
    # so kept by design); the lines past the window are not.
    assert not any("compute_grid_layout" in line for line in kept)
    assert not any(line == "return layout" for line in kept)


def test_lexical_skim_reduces_tokens() -> None:
    skimmer = LineSkimmer(ranker=None)
    result = skimmer.skim(_CHUNK_BODY, _GOAL)
    assert count_tokens(result.text) < count_tokens(_CHUNK_BODY)


def test_skim_output_stays_parseable() -> None:
    import ast

    skimmer = LineSkimmer(ranker=None)
    result = skimmer.skim(_CHUNK_BODY, _GOAL)
    # The pruned body still parses: anchors + indentation are preserved.
    ast.parse(result.text)


def test_empty_goal_is_noop() -> None:
    skimmer = LineSkimmer(ranker=None)
    result = skimmer.skim(_CHUNK_BODY, "   ")
    assert result.dropped_lines == 0
    assert result.text == _CHUNK_BODY


def test_no_signal_keeps_chunk_whole() -> None:
    skimmer = LineSkimmer(ranker=None)
    result = skimmer.skim(_CHUNK_BODY, "quantum chromodynamics lattice")
    # Nothing matches: do not gut the chunk down to anchors only.
    assert result.dropped_lines == 0
    assert result.text == _CHUNK_BODY


def test_tiny_body_is_noop() -> None:
    skimmer = LineSkimmer(ranker=None)
    body = "a = 1\nb = 2"
    result = skimmer.skim(body, _GOAL)
    assert result.dropped_lines == 0
    assert result.text == body


# --- unit: embedding path + degrade -----------------------------------------


def test_embedding_path_used_when_ranker_available() -> None:
    ranker = _FakeRanker(available=True)
    skimmer = LineSkimmer(ranker=ranker)
    result = skimmer.skim(_CHUNK_BODY, _GOAL)
    assert result.used_embedding is True
    assert result.changed
    assert ranker.calls  # goal was embedded
    kept = _relevant_lines(result.text)
    assert any("amount must be positive" in line for line in kept)


def test_degrades_to_lexical_when_embedding_unavailable() -> None:
    ranker = _FakeRanker(available=False)
    skimmer = LineSkimmer(ranker=ranker)
    result = skimmer.skim(_CHUNK_BODY, _GOAL)
    # available=False -> never touches the embedder, scores lexically.
    assert result.used_embedding is False
    assert not ranker.calls
    assert result.changed


# --- unit: skim_chunks ------------------------------------------------------


def test_skim_chunks_mutates_snippet_in_place() -> None:
    chunk = _FakeChunk(snippet=_CHUNK_BODY)
    before = count_tokens(chunk.snippet)
    out = skim_chunks([chunk], goal=_GOAL, ranker=None)
    assert out[0] is chunk
    assert count_tokens(chunk.snippet) < before


def test_skim_chunks_empty_goal_noop() -> None:
    chunk = _FakeChunk(snippet=_CHUNK_BODY)
    skim_chunks([chunk], goal="  ", ranker=None)
    assert chunk.snippet == _CHUNK_BODY


# --- integration: pull() wiring + default-off flag --------------------------


def _records() -> list[_FakeRecord]:
    return [
        _FakeRecord(
            "src/pay.py",
            "charge_card",
            score=0.9,
            signature="def charge_card(self, amount, currency): ...",
            snippet=_CHUNK_BODY,
        ),
    ]


def test_pull_unchanged_when_flag_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ATELIER_LINE_SKIM", raising=False)
    cap = ScopedContextCapability(_FakeEngine(_records()))
    result = cap.pull(
        Subtask(
            description="fix the charge card payment amount validation",
            keywords=["charge", "card", "amount"],
            affected_paths=["src/pay.py"],
            budget_tokens=4000,
        )
    )
    assert result.chunks
    # Flag off: chunk body is the untouched original snippet.
    assert result.chunks[0].snippet == _CHUNK_BODY
    assert "line-skim" not in result.rationale


def test_pull_skims_when_flag_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ATELIER_LINE_SKIM", "1")
    cap = ScopedContextCapability(_FakeEngine(_records()))
    result = cap.pull(
        Subtask(
            description="fix the charge card payment amount validation",
            keywords=["charge", "card", "amount"],
            affected_paths=["src/pay.py"],
            budget_tokens=4000,
        )
    )
    assert result.chunks
    skimmed = result.chunks[0].snippet
    assert skimmed != _CHUNK_BODY
    assert count_tokens(skimmed) < count_tokens(_CHUNK_BODY)
    # Anchors preserved; relevant content preserved; noise dropped.
    assert "def charge_card(self, amount, currency):" in skimmed
    assert "amount must be positive" in skimmed
    assert "compute_grid_layout" not in skimmed
    assert "line-skim" in result.rationale
