"""Ask a self-hosted endpoint what it can actually do, and believe only the answer.

Why this module exists: ``providers.json`` records capabilities that decide
routing (``supports_tool_use`` gates every edit turn), and nothing else in the
repo can supply them for a user-registered endpoint. A model catalogue cannot:
the same ``Qwen3-Coder-30B`` weights behind vLLM, llama.cpp and Ollama expose
different tool-calling and JSON-schema support depending on the *server*, its
version and its launch flags. The only truthful source is the endpoint itself.

Three rules shape everything here.

* **Unknown is an answer.** ``"unsupported"`` means the server rejected the
  feature; ``"unknown"`` means we failed to learn anything. They are never
  merged. A model that could not be reached must not be recorded as a model
  without tools -- that reads as a measurement, and it would silently demote
  the endpoint forever.
* **Nothing raises.** Every exchange is wrapped; a refused connection, a TLS
  error, a proxy returning HTML, a body that is not JSON -- all become a
  ``ProbeCheck`` with ``status="error"``. ``probe_model`` has no failure mode
  other than returning a result.
* **Nothing hangs.** Every request is bounded by the caller's ``timeout``, and
  the first *transport* failure (as opposed to an HTTP error status, which
  proves the server is alive) skips the remaining live checks. A dead endpoint
  therefore costs one timeout, not one per check.

Only safely measurable things are probed. Context length is *read* from the
``/models`` entry, never binary-searched -- discovering the limit by provoking
failures would cost the user real tokens and, on a shared endpoint, real
capacity. Vision is not probed unless asked for, because sending an image to an
unknown endpoint is a request the user did not authorise -- and when it is
asked for, it answers ``ok`` only if the reply names what the image contained.
A server that accepts the request and answers something plausible has proved
nothing: text-only endpoints drop the image part and reply anyway.

Transport convention (urllib, broad ``except``, ``logger.debug``) is copied from
``core/capabilities/providers/discovery.py``: this runs against boxes that are
half-configured by definition, so noise on stderr would be constant.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .models import (
    PROBE_SCHEMA_VERSION,
    ProbeCheck,
    ProbeCheckName,
    ProbeResult,
    custom_model_id,
)
from .transport import NO_KEY_PLACEHOLDER

logger = logging.getLogger(__name__)

_PROMPT = "Reply with the single word: ok"
_TOOL_PROMPT = "What time is it? Use the tool."
_JSON_PROMPT = "Reply with a JSON object holding one key, answer, set to ok."
_VISION_PROMPT = "What colour is this image? Reply with the single colour word and nothing else."

_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Return the current time",
            "parameters": {"type": "object", "properties": {}},
        },
    },
)

_RESPONSE_FORMAT: dict[str, Any] = {
    "type": "json_schema",
    "json_schema": {
        "name": "r",
        "schema": {
            "type": "object",
            "properties": {"answer": {"type": "string"}},
            "required": ["answer"],
        },
    },
}

# A 32x32 solid #00FF00 PNG (95 bytes), inline so nothing is fetched. The
# previous payload was a 1x1 *transparent* pixel, which is unusable as
# evidence: there is no answer to it, so any reply at all had to be accepted
# and a text-only server replying "ok" was recorded as vision-capable. An image
# with a colour has one correct answer that a server which never decoded it
# cannot produce. 32x32 clears every encoder's minimum tile size, costs a
# handful of tokens, and carries no content an operator could object to.
_VISION_IMAGE = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAJklEQVR42u3NsQkAAAjAsP7/tF7hIASy"
    "p6ZbAoFAIBAIBAKB4EuwNof8Lqbpz1cAAAAASUVORK5CYII="
)
# The replies that prove the image was decoded. "lime" is a truthful name for
# #00FF00, so it counts; every other colour word is a miss, which stops a model
# that hedges by listing colours from passing on a lucky substring.
_VISION_EXPECTED = frozenset({"green", "lime"})
_VISION_COLOUR_WORDS = frozenset(
    {
        "amber",
        "beige",
        "black",
        "blue",
        "brown",
        "cyan",
        "gold",
        "gray",
        "green",
        "grey",
        "indigo",
        "lime",
        "magenta",
        "maroon",
        "navy",
        "olive",
        "orange",
        "pink",
        "purple",
        "red",
        "silver",
        "teal",
        "transparent",
        "turquoise",
        "violet",
        "white",
        "yellow",
    }
)

# The keys vLLM, LM Studio and llama.cpp use on a `/models` item to report the
# context limit. Read, never inferred.
_CONTEXT_KEYS: tuple[str, ...] = ("max_model_len", "max_input_tokens", "context_length")

# Bodies larger than this are a proxy error page or a misrouted stream, not an
# OpenAI response. Read a bounded prefix so a hostile endpoint cannot fill RAM.
_MAX_BODY_BYTES = 1_000_000
_DETAIL_CHARS = 200


@dataclass(frozen=True)
class _HttpOutcome:
    """One HTTP exchange. ``error`` is set only when no response arrived at all.

    An HTTP 400 is *information* (the server is alive and rejected something);
    a refused connection is the absence of information. Keeping the two apart in
    one place is what lets every check map statuses honestly.
    """

    status: int
    body: dict[str, Any]
    text: str
    elapsed_ms: float
    error: str = ""

    @property
    def responded(self) -> bool:
        return not self.error


def probe_model(
    base_url: str,
    raw_model_id: str,
    *,
    endpoint_name: str,
    api_key: str = "",
    probe_vision: bool = False,
    timeout: float = 30.0,
    context_window: int | None = None,
) -> ProbeResult:
    """Measure one model on one OpenAI-compatible endpoint. Never raises.

    ``context_window`` is the user's ``--context-window`` override: when given
    it is recorded as-is and ``/models`` is not consulted, because the operator
    knows their deployment's real limit better than a server that often reports
    none at all.
    """

    base = base_url.strip().rstrip("/")
    chat_url = f"{base}/chat/completions"
    bounded = max(0.1, float(timeout))

    chat_check, chat = _probe_chat(chat_url, raw_model_id, api_key, bounded)
    # The endpoint answered *something* (even a 500) => keep asking. It did not
    # answer at all => every further request would cost another full timeout.
    stalled = chat.error

    throughput_check = _throughput_check(chat, stalled)

    tool_check, outcome = _probe_tool_calling(chat_url, raw_model_id, api_key, bounded, stalled)
    stalled = stalled or outcome.error
    structured_check, outcome = _probe_structured_output(chat_url, raw_model_id, api_key, bounded, stalled)
    stalled = stalled or outcome.error
    context_check, outcome = _probe_context_window(base, raw_model_id, api_key, bounded, stalled, context_window)
    stalled = stalled or outcome.error
    vision_check, _ = _probe_vision(chat_url, raw_model_id, api_key, bounded, stalled, probe_vision)

    checks = (chat_check, tool_check, structured_check, context_check, throughput_check, vision_check)
    return ProbeResult(
        schema_version=PROBE_SCHEMA_VERSION,
        endpoint=base,
        endpoint_name=endpoint_name,
        raw_model_id=raw_model_id,
        model_id=custom_model_id(endpoint_name, raw_model_id),
        checks=checks,
        context_window=_int_value(context_check),
        supports_tool_use=_capability(tool_check),
        supports_structured_output=_capability(structured_check),
        supports_vision=_capability(vision_check),
        observed_tokens_per_second=_float_value(throughput_check),
        # A non-streaming probe cannot see the first token, and a round-trip is
        # not a time-to-first-token. The honest value is None; the measured
        # round-trip is reported as the chat_completion check's own value.
        latency_first_token_ms=None,
        probed_at=datetime.now(UTC).isoformat(),
        ok=chat_check.status == "ok",
    )


def _probe_chat(url: str, model: str, api_key: str, timeout: float) -> tuple[ProbeCheck, _HttpOutcome]:
    """The one check with no ``unknown``: either the endpoint completes, or it does not."""

    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _PROMPT}],
        "max_tokens": 16,
    }
    outcome = _request(url, api_key, timeout, payload=payload)
    if not outcome.responded:
        return ProbeCheck(name="chat_completion", status="error", detail=outcome.error), outcome
    round_trip = round(outcome.elapsed_ms, 1)
    if outcome.status != 200:
        return (
            ProbeCheck(name="chat_completion", status="error", detail=_http_detail(outcome), value=round_trip),
            outcome,
        )
    content = _message_content(outcome.body)
    if not content:
        return (
            ProbeCheck(
                name="chat_completion",
                status="error",
                detail="HTTP 200 but choices[0].message.content was empty",
                value=round_trip,
            ),
            outcome,
        )
    detail = f"HTTP 200 in {outcome.elapsed_ms:.0f} ms round-trip, replied {content[:40]!r}"
    return ProbeCheck(name="chat_completion", status="ok", detail=detail, value=round_trip), outcome


def _throughput_check(chat: _HttpOutcome, stalled: str) -> ProbeCheck:
    """Wall-clock tokens/second over the completion probe -- a floor, not a rate card.

    The elapsed time includes prompt processing and connection setup, so the
    number under-reports a warm server. It is reported as observed rather than
    corrected, because the correction would be a guess.
    """

    if stalled:
        return _skipped("throughput", stalled)
    if chat.status != 200:
        return ProbeCheck(name="throughput", status="unknown", detail="the completion probe did not return a body")
    usage = chat.body.get("usage")
    tokens = usage.get("completion_tokens") if isinstance(usage, dict) else None
    if isinstance(tokens, bool) or not isinstance(tokens, (int, float)) or tokens <= 0:
        return ProbeCheck(name="throughput", status="unknown", detail="the response carried no usage.completion_tokens")
    elapsed_s = chat.elapsed_ms / 1000.0
    if elapsed_s <= 0:
        return ProbeCheck(name="throughput", status="unknown", detail="the response arrived too fast to time")
    rate = float(tokens) / elapsed_s
    detail = f"{int(tokens)} completion tokens in {elapsed_s:.2f}s (wall clock, includes prompt processing)"
    return ProbeCheck(name="throughput", status="ok", detail=detail, value=round(rate, 1))


def _probe_tool_calling(
    url: str, model: str, api_key: str, timeout: float, stalled: str
) -> tuple[ProbeCheck, _HttpOutcome]:
    """``tool_choice="auto"`` means a model that simply chose not to call is *unknown*."""

    if stalled:
        return _skipped("tool_calling", stalled), _unattempted()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _TOOL_PROMPT}],
        "max_tokens": 64,
        "tools": [dict(tool) for tool in _TOOLS],
        "tool_choice": "auto",
    }
    outcome = _request(url, api_key, timeout, payload=payload)
    if not outcome.responded:
        return ProbeCheck(name="tool_calling", status="error", detail=outcome.error), outcome
    rejected = _rejection(outcome, ("tool", "function"))
    if rejected is not None:
        return ProbeCheck(name="tool_calling", status="unsupported", detail=rejected), outcome
    if outcome.status != 200:
        return ProbeCheck(name="tool_calling", status="unknown", detail=_http_detail(outcome)), outcome
    calls = _tool_calls(outcome.body)
    if calls:
        names = ", ".join(sorted(_call_names(calls)))
        check = ProbeCheck(name="tool_calling", status="ok", detail=f"returned tool_calls: {names}", value=len(calls))
        return check, outcome
    return (
        ProbeCheck(
            name="tool_calling",
            status="unknown",
            detail="the model answered without calling the offered tool; that is not evidence either way",
        ),
        outcome,
    )


def _probe_structured_output(
    url: str, model: str, api_key: str, timeout: float, stalled: str
) -> tuple[ProbeCheck, _HttpOutcome]:
    """Accepted *and* honoured: a server that ignores the schema is not support."""

    if stalled:
        return _skipped("structured_output", stalled), _unattempted()
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": _JSON_PROMPT}],
        "max_tokens": 64,
        "response_format": dict(_RESPONSE_FORMAT),
    }
    outcome = _request(url, api_key, timeout, payload=payload)
    if not outcome.responded:
        return ProbeCheck(name="structured_output", status="error", detail=outcome.error), outcome
    rejected = _rejection(outcome, ("response_format", "json_schema", "guided"))
    if rejected is not None:
        return ProbeCheck(name="structured_output", status="unsupported", detail=rejected), outcome
    if outcome.status != 200:
        return ProbeCheck(name="structured_output", status="unknown", detail=_http_detail(outcome)), outcome
    content = _message_content(outcome.body)
    parsed = _loads(content)
    if isinstance(parsed, dict) and "answer" in parsed:
        return (
            ProbeCheck(name="structured_output", status="ok", detail="returned JSON matching the requested schema"),
            outcome,
        )
    return (
        ProbeCheck(
            name="structured_output",
            status="unknown",
            detail="the request was accepted but the reply did not match the requested schema",
        ),
        outcome,
    )


def _probe_context_window(
    base: str,
    model: str,
    api_key: str,
    timeout: float,
    stalled: str,
    override: int | None,
) -> tuple[ProbeCheck, _HttpOutcome]:
    """Read the limit off ``GET /models``. Never binary-search for it."""

    if override is not None and override > 0:
        return (
            ProbeCheck(
                name="context_window",
                status="ok",
                detail="set explicitly with --context-window; the endpoint was not asked",
                value=int(override),
            ),
            _unattempted(),
        )
    if stalled:
        return _skipped("context_window", stalled), _unattempted()
    outcome = _request(f"{base}/models", api_key, timeout)
    if not outcome.responded:
        return ProbeCheck(name="context_window", status="error", detail=outcome.error), outcome
    if outcome.status != 200:
        return ProbeCheck(name="context_window", status="unknown", detail=_http_detail(outcome)), outcome
    item = _models_entry(outcome.body, model)
    if item is None:
        return (
            ProbeCheck(name="context_window", status="unknown", detail=f"/models does not list {model!r}"),
            outcome,
        )
    for key in _CONTEXT_KEYS:
        value = item.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > 0:
            return (
                ProbeCheck(name="context_window", status="ok", detail=f"/models reported {key}", value=int(value)),
                outcome,
            )
    return (
        ProbeCheck(
            name="context_window",
            status="unknown",
            detail="the /models entry reports no context length; pass --context-window to record it",
        ),
        outcome,
    )


def _probe_vision(
    url: str, model: str, api_key: str, timeout: float, stalled: str, requested: bool
) -> tuple[ProbeCheck, _HttpOutcome]:
    """Opt-in only, and ``ok`` only on positive evidence that the image was seen.

    ``supports_vision=True`` is written into ``providers.json`` and routes real
    work, so the bar is a reply that could not have been produced without
    decoding the image: the colour of a solid green square. A 200 that answers
    something else -- including a polite "ok" from a text-only server that
    ignored the image part it did not understand -- is ``unknown``, not ``yes``.
    Only the server saying so (a 4xx naming the feature) is ever ``unsupported``.
    """

    if not requested:
        return ProbeCheck(name="vision", status="unknown", detail="not probed"), _unattempted()
    if stalled:
        return _skipped("vision", stalled), _unattempted()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_PROMPT},
                    {"type": "image_url", "image_url": {"url": _VISION_IMAGE}},
                ],
            }
        ],
        "max_tokens": 16,
    }
    outcome = _request(url, api_key, timeout, payload=payload)
    if not outcome.responded:
        return ProbeCheck(name="vision", status="error", detail=outcome.error), outcome
    rejected = _rejection(outcome, ("image", "vision", "multimodal"))
    if rejected is not None:
        return ProbeCheck(name="vision", status="unsupported", detail=rejected), outcome
    if outcome.status != 200:
        return ProbeCheck(name="vision", status="unknown", detail=_http_detail(outcome)), outcome
    reply = _message_content(outcome.body)
    if not reply:
        return (
            ProbeCheck(name="vision", status="unknown", detail="HTTP 200 with an empty reply"),
            outcome,
        )
    seen = _named_the_image(reply)
    if not seen:
        detail = f"HTTP 200, but the reply did not name the image's colour: {reply}"
        return ProbeCheck(name="vision", status="unknown", detail=detail[:_DETAIL_CHARS]), outcome
    return (
        ProbeCheck(name="vision", status="ok", detail=f"named the test image's colour ({seen})"),
        outcome,
    )


def _named_the_image(reply: str) -> str:
    """The matched colour word, or ``""`` when the reply proves nothing.

    A reply that also names a *different* colour is a miss even when "green" is
    somewhere in it: a model hedging across the palette has not seen the image
    either, and one lucky substring is not a measurement.
    """

    words = frozenset(re.findall(r"[a-z]+", reply.lower()))
    matched = words & _VISION_EXPECTED
    if not matched or words & (_VISION_COLOUR_WORDS - _VISION_EXPECTED):
        return ""
    return sorted(matched)[0]


def _request(url: str, api_key: str, timeout: float, *, payload: dict[str, Any] | None = None) -> _HttpOutcome:
    """One bounded HTTP exchange. Transport failures come back as data, not exceptions."""

    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            # Local servers ignore this; corporate gateways require it. The
            # placeholder is never a credential and is never persisted.
            "Authorization": f"Bearer {api_key or NO_KEY_PLACEHOLDER}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST" if data is not None else "GET",
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_BODY_BYTES)
            status = int(getattr(response, "status", 0) or 200)
    except urllib.error.HTTPError as exc:
        raw = _drain(exc)
        status = int(exc.code)
    except Exception as exc:
        elapsed = (time.monotonic() - started) * 1000.0
        logger.debug("model probe %s: %s", url, exc)
        detail = f"{type(exc).__name__}: {exc}".strip()[:_DETAIL_CHARS]
        return _HttpOutcome(status=0, body={}, text="", elapsed_ms=elapsed, error=detail)
    elapsed = (time.monotonic() - started) * 1000.0
    text = raw.decode("utf-8", errors="replace")
    parsed = _loads(text)
    body: dict[str, Any] = parsed if isinstance(parsed, dict) else {}
    return _HttpOutcome(status=status, body=body, text=text, elapsed_ms=elapsed)


def _drain(exc: urllib.error.HTTPError) -> bytes:
    """The error body carries the reason the server refused; losing it costs the user the diagnosis."""

    try:
        return bytes(exc.read(_MAX_BODY_BYTES))
    except Exception:
        return b""


def _unattempted() -> _HttpOutcome:
    """A check that never went to the network. Not a failure -- just no exchange."""

    return _HttpOutcome(status=0, body={}, text="", elapsed_ms=0.0)


def _skipped(name: ProbeCheckName, reason: str) -> ProbeCheck:
    """The endpoint stopped answering; asking again would only buy another timeout."""

    return ProbeCheck(name=name, status="error", detail=f"not attempted, the endpoint did not respond ({reason})")


def _rejection(outcome: _HttpOutcome, markers: tuple[str, ...]) -> str | None:
    """A 4xx naming the feature is the server saying "I do not do that" -- a measurement.

    Any other status is not: a 500 or a 404 says nothing about the feature, and
    treating it as a negative would bake a transient outage into providers.json.
    """

    if outcome.status not in (400, 422):
        return None
    haystack = f"{outcome.text} {json.dumps(outcome.body, default=str)}".lower()
    if not any(marker in haystack for marker in markers):
        return None
    return _http_detail(outcome)


def _http_detail(outcome: _HttpOutcome) -> str:
    body = outcome.text.strip().replace("\n", " ")
    return f"HTTP {outcome.status}: {body}"[:_DETAIL_CHARS] if body else f"HTTP {outcome.status}"


def _loads(text: str) -> Any:
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


def _first_choice(body: dict[str, Any]) -> dict[str, Any]:
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return {}
    first = choices[0]
    return first if isinstance(first, dict) else {}


def _message_content(body: dict[str, Any]) -> str:
    """``choices[0].message.content``, flattening the multipart form some servers use."""

    message = _first_choice(body).get("message")
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [str(part.get("text") or "") for part in content if isinstance(part, dict)]
        return " ".join(part for part in parts if part).strip()
    return ""


def _tool_calls(body: dict[str, Any]) -> list[dict[str, Any]]:
    message = _first_choice(body).get("message")
    if not isinstance(message, dict):
        return []
    calls = message.get("tool_calls")
    if not isinstance(calls, list):
        return []
    return [call for call in calls if isinstance(call, dict)]


def _call_names(calls: list[dict[str, Any]]) -> set[str]:
    names: set[str] = set()
    for call in calls:
        function = call.get("function")
        name = function.get("name") if isinstance(function, dict) else None
        names.add(str(name) if name else "?")
    return names


def _models_entry(body: dict[str, Any], model: str) -> dict[str, Any] | None:
    """The ``/models`` item for exactly this id. A near-match is a different model."""

    items = body.get("data")
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and str(item.get("id") or "") == model:
            return item
    return None


def _capability(check: ProbeCheck) -> bool | None:
    """``ok`` -> True, ``unsupported`` -> False, everything else -> unknown.

    ``error`` and ``unknown`` both map to ``None`` on purpose: the difference
    between them is *why* we do not know, which the check keeps, not *whether*
    we know, which is what the flag means.
    """

    if check.status == "ok":
        return True
    if check.status == "unsupported":
        return False
    return None


def _int_value(check: ProbeCheck) -> int | None:
    value = check.value
    if check.status != "ok" or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _float_value(check: ProbeCheck) -> float | None:
    value = check.value
    if check.status != "ok" or isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


__all__ = ["probe_model"]
