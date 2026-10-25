"""A provider that refuses the key, the credit or the rate reads as one sentence on every route, and LiteLLM is quiet.

The audit: a bad key through LiteLLM printed its "Give Feedback / Get Help" banner four times in red (whatever ``NO_COLOR``
said) and a truncated JSON error. LiteLLM prints that banner itself unless ``litellm.suppress_debug_info`` is set; the
errors are mapped to the sentence the SDK-free route writes (``describe_refusal``).
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from lemoncrow.infra.internal_llm import litellm_client
from lemoncrow.infra.internal_llm.exceptions import LiteLLMUnavailable, refusal_kind


class AuthenticationError(Exception):
    status_code = 401
    llm_provider = "anthropic"
    message = "invalid x-api-key"


class OutOfCredit(Exception):
    status_code = 400
    llm_provider = "anthropic"
    message = "Your credit balance is too low to access the Anthropic API."


class Outage(Exception):
    status_code = 503
    llm_provider = "anthropic"
    message = "overloaded"


@pytest.fixture
def fake_litellm(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    module = types.ModuleType("litellm")
    module.suppress_debug_info = False  # type: ignore[attr-defined]
    module.raised = None  # type: ignore[attr-defined]

    def completion(**_kwargs: Any) -> Any:
        raise module.raised  # type: ignore[attr-defined]

    module.completion = completion  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "litellm", module)
    return module


def test_importing_litellm_for_a_call_turns_its_banner_off(fake_litellm: types.ModuleType) -> None:
    assert fake_litellm.suppress_debug_info is False  # type: ignore[attr-defined]
    litellm_client._litellm_module()
    assert fake_litellm.suppress_debug_info is True  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("error", "kind", "start"),
    [
        (AuthenticationError(), "key", "Anthropic rejected the API key (HTTP 401): check ANTHROPIC_API_KEY."),
        (OutOfCredit(), "credit", "Anthropic says your account has no credit left (HTTP 400): add credit at"),
    ],
)
def test_a_refusal_through_litellm_is_the_same_sentence_as_the_direct_route(
    fake_litellm: types.ModuleType, error: Exception, kind: str, start: str
) -> None:
    fake_litellm.raised = error  # type: ignore[attr-defined]
    with pytest.raises(LiteLLMUnavailable) as raised:
        litellm_client.chat_with_result([{"role": "user", "content": "hi"}], model="anthropic/claude-sonnet-4-6")
    assert str(raised.value).startswith(start) and refusal_kind(str(raised.value)) == kind
    assert "litellm" not in str(raised.value).lower() and "AuthenticationError" not in str(raised.value)


def test_any_other_failure_keeps_its_own_words(fake_litellm: types.ModuleType) -> None:
    fake_litellm.raised = Outage("overloaded")  # type: ignore[attr-defined]
    with pytest.raises(LiteLLMUnavailable, match="LiteLLM completion unavailable: overloaded"):
        litellm_client.chat_with_result([{"role": "user", "content": "hi"}], model="anthropic/claude-sonnet-4-6")
