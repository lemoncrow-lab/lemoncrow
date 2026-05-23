from __future__ import annotations

import pytest

from atelier.core.capabilities.tool_supervision.compact_output import compact
from atelier.infra.internal_llm.ollama_client import OllamaUnavailable


def test_compact_passthrough_under_threshold() -> None:
    # "short output" is well under the 1800-char threshold
    result = compact("short output", content_type="tool_output")
    assert result.method == "passthrough"
    assert result.compacted == "short output"


def test_compact_passthrough_boundary() -> None:
    # Exactly at threshold: passthrough; over threshold: truncate
    at_limit = "a" * 1800
    result = compact(at_limit, content_type="unknown")
    assert result.method == "passthrough"

    over_limit = "a" * 1801
    result2 = compact(over_limit, content_type="unknown")
    assert result2.method == "deterministic_truncate"


def test_compact_groups_grep_output_deterministically() -> None:
    content = "\n".join(f"src/app.py:{i}: hit" for i in range(800))
    result = compact(content, content_type="grep", budget_tokens=80)
    assert result.method == "deterministic_truncate"
    assert "and 797 more" in result.compacted
    assert result.compacted_tokens < result.original_tokens


def test_compact_uses_ollama_when_enable_ollama_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "atelier.core.capabilities.tool_supervision.compact_output.summarize",
        lambda prompt, max_tokens=500: "ollama compacted",
    )
    result = compact("alpha " * 2500, content_type="bash", enable_ollama=True)
    assert result.method == "ollama_summary"
    assert result.compacted == "ollama compacted"


def test_compact_does_not_use_ollama_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ollama should NOT be tried unless enable_ollama=True — head/tail is enough."""
    called = []

    def _should_not_be_called(prompt: str, max_tokens: int = 500) -> str:
        called.append(prompt)
        return "ollama compacted"

    monkeypatch.setattr(
        "atelier.core.capabilities.tool_supervision.compact_output.summarize",
        _should_not_be_called,
    )
    result = compact("alpha " * 2500, content_type="bash")
    assert result.method == "deterministic_truncate"
    assert not called, "Ollama should not be called without enable_ollama=True"


def test_compact_large_output_falls_back_when_ollama_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(prompt: str, max_tokens: int = 500) -> str:
        _ = (prompt, max_tokens)
        raise OllamaUnavailable("offline")

    monkeypatch.setattr(
        "atelier.core.capabilities.tool_supervision.compact_output.summarize",
        unavailable,
    )
    result = compact("alpha " * 2500, content_type="unknown", budget_tokens=100, enable_ollama=True)
    assert result.method == "deterministic_truncate"
    assert result.compacted_tokens < result.original_tokens


def test_compress_tool_output_standalone() -> None:
    """compress_tool_output() standalone helper matches RB API."""
    from atelier.core.capabilities.tool_supervision.compact_output import compress_tool_output

    short = "hello world"
    assert compress_tool_output(short) == short

    big = "x" * 2000
    out = compress_tool_output(big, threshold_chars=1800, head_chars=900, tail_chars=700)
    assert out.startswith("x" * 900)
    assert out.endswith("x" * 700)
    assert "400 chars truncated" in out
    assert len(out) < len(big)


def test_head_tail_asymmetric_split() -> None:
    """Head should be ~60% of budget, tail ~40% (more signal at start)."""
    content = "HEADER" + "M" * 2000 + "TAIL"
    result = compact(content, content_type="unknown", budget_tokens=250)
    assert result.method == "deterministic_truncate"
    # Head (600 chars of 1000 budget) should include "HEADER"
    assert "HEADER" in result.compacted
    assert "TAIL" in result.compacted
