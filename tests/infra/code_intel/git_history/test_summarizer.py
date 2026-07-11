"""Unit tests for the LLM-based commit summariser."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from lemoncrow.infra.code_intel.git_history.models import CommitRecord, CommitSummary
from lemoncrow.infra.code_intel.git_history.summarizer import (
    _CURRENT_PROMPT_VERSION,
    SummarizerError,
    summarize_commit,
)


def _make_record(**kwargs: Any) -> CommitRecord:
    defaults: dict[str, Any] = {
        "sha": "abc123def456abc123def456",
        "author_date": 1700000000,
        "message": "Fix auth session leak",
        "files_touched": ["src/auth.py", "tests/test_auth.py"],
        "is_merge": False,
    }
    return CommitRecord(**{**defaults, **kwargs})


def test_summary_returns_valid_commit_summary() -> None:
    record = _make_record()
    valid_text = "This commit fixed an authentication session leak affecting all users. "
    valid_text += "Key changes were made to src/auth.py where the session manager "
    valid_text += "was not properly closing tokens on logout. The fix ensures all "
    valid_text += "JWT tokens are invalidated server-side on session termination."

    with patch("lemoncrow.infra.code_intel.git_history.summarizer.chat", return_value=valid_text):
        result = summarize_commit(record, diff_text="--- a/src/auth.py\n+++ b/src/auth.py")

    assert isinstance(result, CommitSummary)
    assert result.sha == record.sha
    assert result.prompt_version == "v1"
    assert result.summary == valid_text.strip()
    assert result.author_date == record.author_date


def test_summary_prompt_version_and_model_fields() -> None:
    record = _make_record()
    captured: dict[str, Any] = {}

    def fake_chat(messages: list[Any], *, model: str = "") -> str:
        captured["model"] = model
        captured["messages"] = messages
        return "A valid summary with enough words to be considered acceptable by the summariser."

    with patch("lemoncrow.infra.code_intel.git_history.summarizer.chat", fake_chat):
        result = summarize_commit(record)

    assert result.prompt_version == _CURRENT_PROMPT_VERSION
    assert result.summary_model == captured["model"]
    assert isinstance(result.summary_model, str)
    assert len(result.summary_model) > 0


def test_summarizer_error_on_empty_response() -> None:
    record = _make_record()
    with (
        patch("lemoncrow.infra.code_intel.git_history.summarizer.chat", return_value=""),
        pytest.raises(SummarizerError),
    ):
        summarize_commit(record)


def test_summarizer_error_on_llm_exception() -> None:
    record = _make_record()
    with (
        patch(
            "lemoncrow.infra.code_intel.git_history.summarizer.chat",
            side_effect=RuntimeError("model unavailable"),
        ),
        pytest.raises(SummarizerError) as exc_info,
    ):
        summarize_commit(record)
    assert "LLM call failed" in str(exc_info.value)


def test_summarizer_uses_env_model(monkeypatch: pytest.MonkeyPatch) -> None:
    record = _make_record()
    monkeypatch.setenv("LEMONCROW_LINEAGE_MODEL", "test-model-xyz")
    captured: dict[str, str] = {}

    def fake_chat(messages: list[Any], *, model: str = "") -> str:
        captured["model"] = model
        return "A valid summary returned by the test model."

    with patch("lemoncrow.infra.code_intel.git_history.summarizer.chat", fake_chat):
        result = summarize_commit(record)

    assert captured["model"] == "test-model-xyz"
    assert result.summary_model == "test-model-xyz"


@pytest.mark.parametrize(
    ("backend", "env_name", "env_value"),
    [
        ("ollama", "LEMONCROW_OLLAMA_MODEL", "local-ollama-model"),
        ("openai", "LEMONCROW_OPENAI_MODEL", "openai-model"),
        ("openai_compatible", "LEMONCROW_OPENAI_MODEL", "compat-model"),
        ("litellm", "LEMONCROW_LITELLM_MODEL", "litellm-model"),
    ],
)
def test_summarizer_uses_backend_default_model(
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    env_name: str,
    env_value: str,
) -> None:
    record = _make_record()
    monkeypatch.delenv("LEMONCROW_LINEAGE_MODEL", raising=False)
    monkeypatch.setenv("LEMONCROW_LLM_BACKEND", backend)
    monkeypatch.setenv(env_name, env_value)
    captured: dict[str, str] = {}

    def fake_chat(messages: list[Any], *, model: str = "") -> str:
        captured["model"] = model
        return "A valid summary returned by the backend default model."

    with patch("lemoncrow.infra.code_intel.git_history.summarizer.chat", fake_chat):
        result = summarize_commit(record)

    assert captured["model"] == env_value
    assert result.summary_model == env_value
