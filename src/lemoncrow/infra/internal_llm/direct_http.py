"""Direct HTTPS calls to the Anthropic and OpenAI chat APIs, for an install that has neither vendor SDK.

The installed Review engine ships no model SDK: ``litellm`` is about 75 MB with its dependencies and ``openai``
adds an HTTP stack, for a person who only wants ``export ANTHROPIC_API_KEY=...; lc review --ai``. When the SDK that
the ``litellm`` or ``openai`` driver would import is missing, the driver calls here instead. Standard library only.

This is a fallback, not a second driver: the driver name, the model identity, the budgets, the kill switch and the
governance records are the ones the SDK path produces. What differs, and is said: no cost is reported (a provider
that does not say what a call cost does not get a made-up figure), and streaming is not offered.

Environment: ``ANTHROPIC_API_KEY`` and ``ANTHROPIC_BASE_URL`` (the SDKs' own names), ``LEMONCROW_OPENAI_API_KEY`` /
``OPENAI_API_KEY`` and ``LEMONCROW_OPENAI_BASE_URL``.

Workload Identity Federation, with no stored key: ``ANTHROPIC_FEDERATION_RULE_ID`` and ``ANTHROPIC_ORGANIZATION_ID``
(``ANTHROPIC_SERVICE_ACCOUNT_ID`` and ``ANTHROPIC_WORKSPACE_ID`` when the rule needs them), and an identity token: the
file ``ANTHROPIC_IDENTITY_TOKEN_FILE`` names, re-read for every exchange, else a fresh GitHub Actions OIDC token
fetched from ``ACTIONS_ID_TOKEN_REQUEST_URL`` for every exchange (a GitHub token lives about five minutes and its
``jti`` is single-use). The token is exchanged at ``/v1/oauth/token`` for a short-lived access token, which is kept
in memory only, sent as ``Authorization: Bearer`` and exchanged again 120 s before it expires. ``ANTHROPIC_API_KEY``,
when set, wins, as in the vendor SDKs.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from lemoncrow.infra.internal_llm.exceptions import LiteLLMUnavailable, OpenAIClientUnavailable, describe_refusal
from lemoncrow.infra.internal_llm.result import InternalLLMChatResult
from lemoncrow.infra.internal_llm.usage import note_response

ANTHROPIC_VERSION: Final[str] = "2023-06-01"
DEFAULT_ANTHROPIC_URL: Final[str] = "https://api.anthropic.com"
DEFAULT_OPENAI_URL: Final[str] = "https://api.openai.com/v1"
#: Anthropic needs an output ceiling on every call; the review's answers are short JSON objects.
ANTHROPIC_MAX_TOKENS: Final[int] = 8192
_ANTHROPIC_PREFIX: Final[str] = "anthropic/"
_JSON_ONLY: Final[str] = (
    "Reply with a single JSON object that answers the request: no prose before or after it, no Markdown, no code fences."
)
_MAX_ERROR_BYTES: Final[int] = 4096


def anthropic_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "").strip()


def handles_anthropic(model: str) -> bool:
    """Whether *model* is an ``anthropic/...`` model with a credential to call it: an API key or a federation."""

    return model.startswith(_ANTHROPIC_PREFIX) and (bool(anthropic_key()) or federation_config() is not None)


def uses_federation(model: str) -> bool:
    """Whether *model* is called through Workload Identity Federation: an ``anthropic/`` model, a federation, no key.

    LiteLLM knows nothing of a federated token, so this route never goes through it, installed or not.
    """

    return model.startswith(_ANTHROPIC_PREFIX) and not anthropic_key() and federation_config() is not None


# --------------------------------------------------------------------------- #
# Workload Identity Federation: an identity token exchanged for a short-lived access token
# --------------------------------------------------------------------------- #

GITHUB_AUDIENCE: Final[str] = "https://api.anthropic.com"
_JWT_BEARER: Final[str] = "urn:ietf:params:oauth:grant-type:jwt-bearer"
#: Exchange again this long before the access token expires (the vendor SDKs' advisory refresh).
_REFRESH_MARGIN_S: Final[float] = 120.0
#: The one fix for an opaque refusal: Anthropic names the reason only in the Console.
FEDERATION_DENIED: Final[str] = (
    'Anthropic refused the workload identity token exchange (HTTP 401 "Authentication failed"). The reason is '
    "shown only in the Claude Console: Settings > Workload identity > Authentication history "
    "(platform.claude.com/settings/workload-identity-federation?tab=history). The usual cause is a federation rule "
    "that does not match the token's `sub` claim: a pull_request run's is `repo:<owner>/<repo>:pull_request`, not "
    "`repo:<owner>/<repo>:ref:refs/heads/<branch>`. Check also the rule, organization, service account and workspace "
    "IDs passed to the Action."
)


@dataclass(frozen=True)
class FederationConfig:
    """The non-secret IDs of a federation, and where its identity token comes from."""

    rule_id: str
    organization_id: str
    service_account_id: str
    workspace_id: str
    token_file: str
    github_request_url: str


def federation_config(env: Mapping[str, str] | None = None) -> FederationConfig | None:
    """The federation *env* configures (default: the process environment), or ``None`` without a rule, an
    organization or a source of identity tokens."""

    source = os.environ if env is None else env

    def get(name: str) -> str:
        return str(source.get(name) or "").strip()

    rule, organization = get("ANTHROPIC_FEDERATION_RULE_ID"), get("ANTHROPIC_ORGANIZATION_ID")
    token_file = get("ANTHROPIC_IDENTITY_TOKEN_FILE")
    github = get("ACTIONS_ID_TOKEN_REQUEST_URL") if get("ACTIONS_ID_TOKEN_REQUEST_TOKEN") else ""
    if not (rule and organization and (token_file or github)):
        return None
    return FederationConfig(
        rule, organization, get("ANTHROPIC_SERVICE_ACCOUNT_ID"), get("ANTHROPIC_WORKSPACE_ID"), token_file, github
    )


class _AccessToken:
    """The current access token, in memory only, and when to exchange for the next one."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value = ""
        self.refresh_at = 0.0
        self.config: FederationConfig | None = None


_ACCESS = _AccessToken()


def _mask(value: str) -> None:
    """Tell the GitHub runner to hide *value* in every log line, before anything could print it."""

    if os.environ.get("GITHUB_ACTIONS") == "true" and value:
        try:
            # The step's own standard output, which the runner reads for workflow commands, whatever sys.stdout is now.
            os.write(1, f"::add-mask::{value}\n".encode())
        except OSError:
            pass


def _identity_token(config: FederationConfig, timeout: float) -> str:
    """A fresh identity token: the file re-read, else a new GitHub OIDC token (one per exchange: ``jti`` is single-use)."""

    if config.token_file:
        try:
            token = Path(config.token_file).read_text(encoding="utf-8").strip()
        except OSError as error:
            raise AnthropicRequestFailed(
                f"could not read the identity token file ANTHROPIC_IDENTITY_TOKEN_FILE ({error.strerror})"
            ) from error
        if not token:
            raise AnthropicRequestFailed("the identity token file ANTHROPIC_IDENTITY_TOKEN_FILE is empty")
        return token
    separator = "&" if "?" in config.github_request_url else "?"
    request = urllib.request.Request(
        f"{config.github_request_url}{separator}audience={urllib.parse.quote(GITHUB_AUDIENCE, safe='')}",
        headers={
            "Authorization": f"Bearer {os.environ.get('ACTIONS_ID_TOKEN_REQUEST_TOKEN', '').strip()}",
            "Accept": "application/json",
            "User-Agent": "lemoncrow-review",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            token = str((json.loads(response.read().decode("utf-8")) or {}).get("value") or "").strip()
    except urllib.error.HTTPError as error:
        raise AnthropicRequestFailed(
            f"GitHub refused the OIDC token request (HTTP {error.code}): the workflow needs `permissions: id-token: write`"
        ) from error
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, AttributeError) as error:
        raise AnthropicRequestFailed(f"could not get a GitHub OIDC token ({type(error).__name__})") from error
    if not token:
        raise AnthropicRequestFailed("GitHub's OIDC token endpoint sent no token")
    _mask(token)
    return token


def _exchange(config: FederationConfig, base: str, timeout: float) -> tuple[str, float]:
    """One token exchange: ``(access token, seconds it lives)``. No error message ever holds either token."""

    body: dict[str, str] = {
        "grant_type": _JWT_BEARER,
        "assertion": _identity_token(config, timeout),
        "federation_rule_id": config.rule_id,
        "organization_id": config.organization_id,
    }
    if config.service_account_id:
        body["service_account_id"] = config.service_account_id
    if config.workspace_id:
        body["workspace_id"] = config.workspace_id
    request = urllib.request.Request(
        f"{base}/v1/oauth/token",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "lemoncrow-review"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code in (400, 401, 403):
            raise AnthropicRequestFailed(FEDERATION_DENIED) from None
        raise AnthropicRequestFailed(f"Anthropic's token exchange answered HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise AnthropicRequestFailed(
            f"could not reach Anthropic's token exchange ({getattr(error, 'reason', type(error).__name__)})"
        ) from None
    except ValueError:
        raise AnthropicRequestFailed("Anthropic's token exchange sent a reply that is not JSON") from None
    token = str(reply.get("access_token") or "").strip() if isinstance(reply, Mapping) else ""
    if not token:
        raise AnthropicRequestFailed("Anthropic's token exchange sent no access_token")
    _mask(token)
    lifetime = reply.get("expires_in")
    seconds = float(lifetime) if isinstance(lifetime, int | float) and not isinstance(lifetime, bool) else 60.0
    return token, max(seconds, 1.0)


def federated_access_token(*, base: str = "", timeout: float | None = None) -> str:
    """The access token for the configured federation: the cached one, or a new exchange 120 s before it expires."""

    config = federation_config()
    if config is None:
        raise AnthropicRequestFailed("no Anthropic credential: set ANTHROPIC_API_KEY or a workload identity federation")
    base = base or os.environ.get("ANTHROPIC_BASE_URL", "").strip().rstrip("/") or DEFAULT_ANTHROPIC_URL
    with _ACCESS.lock:
        now = time.monotonic()
        if _ACCESS.value and _ACCESS.config == config and now < _ACCESS.refresh_at:
            return _ACCESS.value
        token, lifetime = _exchange(config, base, _timeout(timeout))
        # A token shorter-lived than twice the margin is refreshed at half its life instead of never being reused.
        margin = _REFRESH_MARGIN_S if lifetime > 2 * _REFRESH_MARGIN_S else lifetime / 2
        _ACCESS.value, _ACCESS.refresh_at, _ACCESS.config = token, now + lifetime - margin, config
        return token


def _anthropic_auth(base: str, timeout: float | None) -> dict[str, str]:
    """The credential header of a Messages call: the API key when there is one (it wins), else a federated token."""

    key = anthropic_key()
    if key:
        return {"x-api-key": key}
    return {"Authorization": f"Bearer {federated_access_token(base=base, timeout=timeout)}"}


def _timeout(timeout: float | None) -> float:
    """The call's timeout: the caller's, else ``REQUEST_TIMEOUT`` (the variable the review already sets), else 120 s."""

    if timeout:
        return timeout
    try:
        return float(os.environ.get("REQUEST_TIMEOUT", "") or 120.0)
    except ValueError:
        return 120.0


def _post(url: str, headers: Mapping[str, str], body: Mapping[str, Any], *, timeout: float, provider: str) -> Any:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "lemoncrow-review", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise _failure(provider, _http_error_text(provider, error)) from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise _failure(provider, f"could not reach {provider} ({getattr(error, 'reason', error)})") from error
    except ValueError as error:
        raise _failure(provider, f"{provider} sent a reply that is not JSON") from error


def _http_error_text(provider: str, error: urllib.error.HTTPError) -> str:
    """What a refusal means to the person, not just its status: a bad key and a spent quota are different fixes."""

    detail = error.read(_MAX_ERROR_BYTES).decode("utf-8", "replace")
    try:
        message = (json.loads(detail).get("error") or {}).get("message") or detail
    except (ValueError, AttributeError):
        message = detail
    refusal = describe_refusal(provider, error.code, message)
    if refusal:
        return refusal
    return f"{provider} answered HTTP {error.code}: {message}"


class AnthropicRequestFailed(LiteLLMUnavailable):
    """A direct Anthropic call failed. Named for what ran: the review prints the class, and LiteLLM is not involved."""


class OpenAIRequestFailed(OpenAIClientUnavailable):
    """A direct OpenAI-compatible call failed."""


def _failure(provider: str, message: str) -> Exception:
    return AnthropicRequestFailed(message) if provider == "Anthropic" else OpenAIRequestFailed(message)


def _int(source: Any, name: str) -> int:
    value = source.get(name) if isinstance(source, Mapping) else None
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


# --------------------------------------------------------------------------- #
# Anthropic Messages API
# --------------------------------------------------------------------------- #


def _anthropic_request(messages: Sequence[Mapping[str, Any]], *, model: str, json_mode: bool) -> dict[str, Any]:
    system: list[Any] = []
    turns: list[dict[str, Any]] = []
    for message in messages:
        content = message.get("content")
        if message.get("role") == "system":
            system.extend(content if isinstance(content, list) else [{"type": "text", "text": str(content or "")}])
        else:
            turns.append({"role": "assistant" if message.get("role") == "assistant" else "user", "content": content})
    if json_mode:
        system.append({"type": "text", "text": _JSON_ONLY})
    body: dict[str, Any] = {
        "model": model.removeprefix(_ANTHROPIC_PREFIX),
        "max_tokens": ANTHROPIC_MAX_TOKENS,
        "messages": turns or [{"role": "user", "content": "Go ahead."}],
    }
    if system:
        body["system"] = system
    return body


def anthropic_chat_with_result(
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str,
    json_schema: Mapping[str, Any] | None,
    cache_metadata: Mapping[str, Any] | None = None,
    timeout: float | None = None,
) -> InternalLLMChatResult:
    """One Anthropic Messages call (a retry once, saying so, when a reply that had to be JSON held none)."""

    from lemoncrow.infra.internal_llm.litellm_client import _JSON_ONLY as retry_instruction
    from lemoncrow.infra.internal_llm.litellm_client import _parse_json

    base = os.environ.get("ANTHROPIC_BASE_URL", "").strip().rstrip("/") or DEFAULT_ANTHROPIC_URL
    headers = {**_anthropic_auth(base, timeout), "anthropic-version": ANTHROPIC_VERSION}
    request = _anthropic_request(messages, model=model, json_mode=json_schema is not None)
    replies: list[Any] = []

    def call(body: Mapping[str, Any]) -> str:
        reply = _post(f"{base}/v1/messages", headers, body, timeout=_timeout(timeout), provider="Anthropic")
        replies.append(reply)
        note_response(model, reply.get("usage") if isinstance(reply, Mapping) else None)
        blocks = reply.get("content") if isinstance(reply, Mapping) else None
        return "".join(
            str(block.get("text") or "")
            for block in (blocks if isinstance(blocks, list) else [])
            if isinstance(block, Mapping) and block.get("type") == "text"
        )

    content = call(request)
    parsed_json: dict[str, Any] | None = None
    if json_schema is not None:
        parsed, why = _parse_json(content)
        if why:
            retry = {**request, "messages": [*request["messages"], {"role": "user", "content": retry_instruction}]}
            content = call(retry)
            parsed, why = _parse_json(content)
            if why:
                raise LiteLLMUnavailable(f"Anthropic returned invalid JSON, also after one retry: {why}")
        parsed_json = parsed if isinstance(parsed, dict) else {"value": parsed}
    last = replies[-1] if isinstance(replies[-1], Mapping) else {}
    usages = [reply.get("usage") for reply in replies if isinstance(reply, Mapping)]
    return InternalLLMChatResult(
        content=content,
        parsed_json=parsed_json,
        model=model,
        reported_model=str(last.get("model") or ""),
        request_id=str(last.get("id") or ""),
        # Every call that was made is paid for, so a retry adds to the tokens of the answer it produced.
        input_tokens=sum(_int(usage, "input_tokens") for usage in usages),
        output_tokens=sum(_int(usage, "output_tokens") for usage in usages),
        cache_read_input_tokens=sum(_int(usage, "cache_read_input_tokens") for usage in usages),
        cache_write_input_tokens=sum(_int(usage, "cache_creation_input_tokens") for usage in usages),
        cache_capability="explicit" if cache_metadata else "none",
        request_metadata=dict(cache_metadata or {}),
        provider_cost=None,
    )


# --------------------------------------------------------------------------- #
# OpenAI-compatible Chat Completions
# --------------------------------------------------------------------------- #


def openai_credentials() -> tuple[str, str]:
    """``(base URL, key)`` of the OpenAI-compatible route; raises when there is neither a key nor a custom server."""

    base = os.environ.get("LEMONCROW_OPENAI_BASE_URL", "").strip().rstrip("/")
    key = (os.environ.get("LEMONCROW_OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        if not base:
            raise OpenAIClientUnavailable(
                "no OpenAI API key found; set LEMONCROW_OPENAI_API_KEY or OPENAI_API_KEY "
                "(or LEMONCROW_OPENAI_BASE_URL for a keyless local server)"
            )
        key = "no-key"  # a keyless local server accepts any placeholder
    return base or DEFAULT_OPENAI_URL, key


def openai_chat_with_result(
    messages: Sequence[Mapping[str, Any]],
    *,
    model: str,
    json_schema: Mapping[str, Any] | None,
    cache_metadata: Mapping[str, Any] | None = None,
    max_tokens: int | None = None,
    timeout: float | None = None,
) -> InternalLLMChatResult:
    """One Chat Completions call, as the SDK path makes it (JSON mode through ``response_format``)."""

    base, key = openai_credentials()
    body: dict[str, Any] = {"model": model, "messages": list(messages)}
    if json_schema is not None:
        body["response_format"] = {"type": "json_object"}
    if max_tokens is not None:
        body["max_tokens"] = max_tokens
    cache_key = str((cache_metadata or {}).get("prompt_cache_key") or "").strip()
    if cache_key:
        body["prompt_cache_key"] = cache_key
    reply = _post(
        f"{base}/chat/completions",
        {"Authorization": f"Bearer {key}"},
        body,
        timeout=_timeout(timeout),
        provider="OpenAI",
    )
    note_response(model, reply.get("usage") if isinstance(reply, Mapping) else None)
    try:
        content = str(reply["choices"][0]["message"]["content"] or "")
    except (KeyError, IndexError, TypeError) as error:
        raise OpenAIClientUnavailable(f"OpenAI-compatible API sent an unexpected reply: {error}") from error
    parsed_json: dict[str, Any] | None = None
    if json_schema is not None:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as error:
            raise OpenAIClientUnavailable(f"API returned invalid JSON: {error}") from error
        parsed_json = parsed if isinstance(parsed, dict) else {"value": parsed}
    usage = reply.get("usage") if isinstance(reply, Mapping) else None
    details = usage.get("prompt_tokens_details") if isinstance(usage, Mapping) else None
    return InternalLLMChatResult(
        content=content,
        parsed_json=parsed_json,
        model=model,
        reported_model=str(reply.get("model") or "") if isinstance(reply, Mapping) else "",
        request_id=str(reply.get("id") or "") if isinstance(reply, Mapping) else "",
        input_tokens=_int(usage, "prompt_tokens"),
        output_tokens=_int(usage, "completion_tokens"),
        cache_read_input_tokens=_int(details, "cached_tokens"),
        cache_capability="explicit" if cache_key else ("hint_only" if cache_metadata else "none"),
        request_metadata={"prompt_cache_key": cache_key} if cache_key else dict(cache_metadata or {}),
    )


__all__ = [
    "FEDERATION_DENIED",
    "AnthropicRequestFailed",
    "FederationConfig",
    "OpenAIRequestFailed",
    "anthropic_chat_with_result",
    "anthropic_key",
    "federated_access_token",
    "federation_config",
    "handles_anthropic",
    "openai_chat_with_result",
    "openai_credentials",
    "uses_federation",
]
