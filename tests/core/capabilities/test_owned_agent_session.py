from __future__ import annotations

from pathlib import Path

from atelier.core.capabilities.owned_agent_session import (
    OwnedAgentSession,
    PhaseTokens,
    SessionReceipt,
    exact_file_content,
    minify_file_content,
)


def test_new_session_has_unique_id() -> None:
    a = OwnedAgentSession.new(provider="anthropic", model="claude", transport="api")
    b = OwnedAgentSession.new(provider="anthropic", model="claude", transport="api")
    assert a.session_id != b.session_id
    assert a.session_id.startswith("atelier-run-")


def test_session_save_load_roundtrip(tmp_path: Path) -> None:
    session = OwnedAgentSession.new(provider="anthropic", model="claude-sonnet-4-5", transport="api")
    session.add_user_turn("hello")
    session.add_assistant_turn("hi there", mark_breakpoint=True)
    path = session.save(root=tmp_path)
    assert path.exists()

    loaded = OwnedAgentSession.load(session.session_id, root=tmp_path)
    assert loaded.session_id == session.session_id
    assert loaded.provider == "anthropic"
    assert loaded.model == "claude-sonnet-4-5"
    assert loaded.transport == "api"
    assert len(loaded.messages) == 2
    assert loaded.messages[0]["role"] == "user"
    assert loaded.messages[1]["role"] == "assistant"


def test_minify_file_content_strips_and_collapses() -> None:
    raw = "def f():   \n    return 1   \n\n\n\nx = 2  \n"
    out = minify_file_content(raw)
    assert "   \n" not in out
    assert "\n\n\n" not in out
    assert "def f():" in out
    assert "    return 1" in out  # indentation preserved


def test_exact_file_content_unchanged() -> None:
    raw = "def f():   \n    return 1   \n\n\n\n"
    assert exact_file_content(raw) == raw


def test_cache_efficiency_pct() -> None:
    receipt = SessionReceipt(session_id="s", provider="anthropic", model="claude")
    receipt.phases.append(
        PhaseTokens(
            phase="survey",
            input_tokens=100,
            cache_read_tokens=600,
            cache_write_tokens=300,
        )
    )
    # 600 / (600 + 300 + 100) = 60%
    assert receipt.cache_efficiency_pct == 60.0


def test_cache_efficiency_pct_zero() -> None:
    receipt = SessionReceipt(session_id="s", provider="anthropic", model="claude")
    assert receipt.cache_efficiency_pct == 0.0


def test_savings_usd_nonnegative() -> None:
    receipt = SessionReceipt(session_id="s", provider="anthropic", model="claude-sonnet-4-5")
    receipt.phases.append(
        PhaseTokens(
            phase="plan",
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=5000,
            cache_write_tokens=0,
        )
    )
    savings = receipt.savings_usd()
    assert savings >= 0.0
    # naive treats cache_read as fresh input -> should cost more than cached
    assert receipt.naive_cost_usd() >= receipt.cost_usd()


def test_format_receipt_nonempty() -> None:
    receipt = SessionReceipt(session_id="s", provider="anthropic", model="claude")
    receipt.phases.append(PhaseTokens(phase="survey", input_tokens=10, output_tokens=5))
    text = receipt.format_receipt()
    assert text
    assert "Session: s" in text
    assert "Cache efficiency" in text
