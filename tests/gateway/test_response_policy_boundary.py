from __future__ import annotations

from lemoncrow.gateway.adapters import mcp_server
from lemoncrow.gateway.tools import loop_review, savings


def test_mcp_reexports_canonical_savings_helpers() -> None:
    assert mcp_server._coerce_saved_tokens is savings.coerce_saved_tokens
    assert mcp_server._extract_tokens_saved is savings.extract_tokens_saved
    assert mcp_server._extract_compact_output_tokens_saved is savings.extract_compact_output_tokens_saved


def test_loop_tracker_state_identity_is_preserved() -> None:
    assert mcp_server._loop_tracker_sessions is loop_review.loop_tracker_sessions
    assert mcp_server._LOOP_TRACKER_LOCK is loop_review.LOOP_TRACKER_LOCK
    assert mcp_server._MAX_LOOP_TRACKER_SESSIONS == loop_review.MAX_LOOP_TRACKER_SESSIONS


def test_saved_token_extraction_prefers_direct_value() -> None:
    assert savings.coerce_saved_tokens({"a": 4, "b": 2.9, "ignored": True}) == 6
    assert savings.extract_tokens_saved({"tokens_saved": 11, "tokens_saved_vs_naive": 5}) == 11
