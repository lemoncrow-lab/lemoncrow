"""The capability probe: what it measures, and everything it refuses to guess.

The load-bearing property is not "the probe works against a good server" -- it
is that a bad, slow, angry or absent server produces *unknown* rather than a
plausible-looking negative. ``supports_tool_use=False`` is written into
``providers.json`` and permanently demotes a model in routing, so the tests
below spend most of their effort on the paths where a lesser probe would
happily invent that value: HTTP 500, a body that is not JSON, an endpoint that
answers 200 without calling the tool, a socket that never answers at all.

Nothing here touches an external network. Every test drives a
``ThreadingHTTPServer`` bound to ``127.0.0.1:0`` whose replies are scripted per
test, so the probe's own HTTP layer (urllib, timeouts, error-body reading) is
exercised for real rather than mocked away.
"""

from __future__ import annotations

import base64
import json
import socket
import struct
import threading
import time
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from lemoncrow.core.capabilities.providers.config import providers_config_path
from lemoncrow.pro.capabilities.model_setup import endpoints as endpoints_mod
from lemoncrow.pro.capabilities.model_setup.endpoints import add_endpoint, get_endpoint, record_probe
from lemoncrow.pro.capabilities.model_setup.models import ProbeCheck, ProbeResult
from lemoncrow.pro.capabilities.model_setup.probe import probe_model

_MODEL = "Qwen3-Coder-30B"

# Reply shapes an OpenAI-compatible server produces. A "reply" is
# (status, body) where body is either a JSON-serialisable object or raw text.
_OK_COMPLETION: dict[str, Any] = {
    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 11, "completion_tokens": 8},
}
_OK_TOOL_CALL: dict[str, Any] = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "get_time", "arguments": "{}"}}],
            }
        }
    ]
}
_OK_JSON: dict[str, Any] = {"choices": [{"message": {"role": "assistant", "content": '{"answer": "ok"}'}}]}


def _reply(content: str) -> dict[str, Any]:
    """A completion body carrying exactly *content* -- what the vision probe reads."""

    return {"choices": [{"message": {"role": "assistant", "content": content}}]}


def _classify(payload: dict[str, Any]) -> str:
    """Which capability question this request is asking."""

    if payload.get("tools"):
        return "tools"
    if payload.get("response_format"):
        return "structured"
    content = (payload.get("messages") or [{}])[0].get("content")
    if isinstance(content, list):
        return "vision"
    return "chat"


class _QuietServer(ThreadingHTTPServer):
    """The timeout tests hang up mid-response on purpose; that is not an error.

    ``block_on_close`` is off so teardown does not wait out a handler that is
    still deliberately sleeping -- the threads are daemons.
    """

    block_on_close = False

    def handle_error(self, request: Any, client_address: Any) -> None:
        return None


class _Endpoint:
    """A scripted OpenAI-compatible server. ``log`` records what was actually asked."""

    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan = plan
        self.log: list[str] = []
        self._server = _QuietServer(("127.0.0.1", 0), self._handler())
        self.base_url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"
        # A short poll interval: the default 0.5s is spent in every teardown.
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()

    def _reply(self, kind: str, default: tuple[int, Any]) -> tuple[int, Any]:
        self.log.append(kind)
        delay = self.plan.get("delay")
        if isinstance(delay, (int, float)) and delay > 0:
            time.sleep(float(delay))
        reply = self.plan.get(kind, default)
        return reply if isinstance(reply, tuple) else (200, reply)

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        endpoint = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self) -> None:
                status, body = endpoint._reply("models", (200, {"data": [{"id": _MODEL}]}))
                self._send(status, body)

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                payload = json.loads(self.rfile.read(length) or b"{}")
                endpoint.plan.setdefault("_auth", []).append(self.headers.get("Authorization"))
                kind = _classify(payload)
                status, body = endpoint._reply(kind, _DEFAULT_REPLIES[kind])
                self._send(status, body)

            def _send(self, status: int, body: Any) -> None:
                raw = (body if isinstance(body, str) else json.dumps(body)).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *_args: Any) -> None:
                pass

        return _Handler


_DEFAULT_REPLIES: dict[str, tuple[int, Any]] = {
    "chat": (200, _OK_COMPLETION),
    "tools": (200, _OK_TOOL_CALL),
    "structured": (200, _OK_JSON),
    "vision": (200, _OK_COMPLETION),
    "models": (200, {"data": [{"id": _MODEL}]}),
}


@pytest.fixture
def serve() -> Iterator[Callable[..., _Endpoint]]:
    """Start scripted endpoints and tear every one of them down."""

    running: list[_Endpoint] = []

    def _start(**plan: Any) -> _Endpoint:
        endpoint = _Endpoint(plan)
        running.append(endpoint)
        return endpoint

    yield _start
    for endpoint in running:
        endpoint.close()


def _probe(endpoint: _Endpoint, **kwargs: Any) -> ProbeResult:
    kwargs.setdefault("timeout", 5.0)
    return probe_model(endpoint.base_url, _MODEL, endpoint_name="box", **kwargs)


def _check(result: ProbeResult, name: str) -> ProbeCheck:
    return next(check for check in result.checks if check.name == name)


def _closed_port() -> int:
    """A port nothing is listening on: bind it, read it, release it."""

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_probe_ok_on_minimal_server(serve: Callable[..., _Endpoint]) -> None:
    result = _probe(serve())

    assert result.ok is True
    assert _check(result, "chat_completion").status == "ok"
    assert result.model_id == f"custom/box/{_MODEL}"
    assert result.schema_version == 1
    assert result.probed_at


def test_probe_result_json_round_trips(serve: Callable[..., _Endpoint]) -> None:
    payload = json.loads(json.dumps(_probe(serve()).to_dict()))

    assert payload["schema_version"] == 1
    assert {check["name"] for check in payload["checks"]} == {
        "chat_completion",
        "tool_calling",
        "structured_output",
        "context_window",
        "throughput",
        "vision",
    }


def test_probe_sends_the_bearer_key(serve: Callable[..., _Endpoint]) -> None:
    endpoint = serve()

    _probe(endpoint, api_key="sk-endpoint")

    assert endpoint.plan["_auth"][0] == "Bearer sk-endpoint"


def test_probe_sends_a_placeholder_when_there_is_no_key(serve: Callable[..., _Endpoint]) -> None:
    """A local vLLM has no key; litellm still needs the header to be present."""

    endpoint = serve()

    _probe(endpoint)

    assert endpoint.plan["_auth"][0] == "Bearer no-key"


# ---------------------------------------------------------------------------
# Tool calling -- ok / unsupported / unknown
# ---------------------------------------------------------------------------


def test_probe_tool_calling_ok(serve: Callable[..., _Endpoint]) -> None:
    result = _probe(serve())

    assert result.supports_tool_use is True
    assert _check(result, "tool_calling").status == "ok"


def test_probe_tool_calling_unsupported_on_400(serve: Callable[..., _Endpoint]) -> None:
    result = _probe(serve(tools=(400, {"error": {"message": "tools not supported by this model"}})))

    assert result.supports_tool_use is False
    assert _check(result, "tool_calling").status == "unsupported"


def test_probe_tool_calling_unknown_on_500(serve: Callable[..., _Endpoint]) -> None:
    """A 500 says nothing about tools. Guards "unknown stays unknown"."""

    result = _probe(serve(tools=(500, {"error": "internal"})))

    assert result.supports_tool_use is None
    assert _check(result, "tool_calling").status == "unknown"


def test_probe_tool_calling_unknown_on_an_unrelated_400(serve: Callable[..., _Endpoint]) -> None:
    """A 400 that never mentions tools is a rejected request, not a missing feature."""

    result = _probe(serve(tools=(400, {"error": {"message": "context length exceeded"}})))

    assert result.supports_tool_use is None
    assert _check(result, "tool_calling").status == "unknown"


def test_probe_tool_calling_unknown_when_the_model_declines_to_call(serve: Callable[..., _Endpoint]) -> None:
    """``tool_choice="auto"`` means a plain answer is ambiguous, not a negative."""

    result = _probe(serve(tools=(200, {"choices": [{"message": {"content": "It is noon."}}]})))

    assert result.supports_tool_use is None
    assert _check(result, "tool_calling").status == "unknown"


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


def test_probe_structured_output_ok(serve: Callable[..., _Endpoint]) -> None:
    result = _probe(serve())

    assert result.supports_structured_output is True


def test_probe_structured_output_unsupported_on_400(serve: Callable[..., _Endpoint]) -> None:
    result = _probe(serve(structured=(400, {"error": "response_format is not supported"})))

    assert result.supports_structured_output is False
    assert _check(result, "structured_output").status == "unsupported"


def test_probe_structured_output_unknown_when_the_schema_is_ignored(serve: Callable[..., _Endpoint]) -> None:
    """Accepting the parameter is not honouring it."""

    result = _probe(serve(structured=(200, {"choices": [{"message": {"content": "sure thing"}}]})))

    assert result.supports_structured_output is None
    assert _check(result, "structured_output").status == "unknown"


# ---------------------------------------------------------------------------
# Context window -- read, never inferred
# ---------------------------------------------------------------------------


def test_probe_context_window_from_models_body(serve: Callable[..., _Endpoint]) -> None:
    result = _probe(serve(models=(200, {"data": [{"id": _MODEL, "max_model_len": 131072}]})))

    assert result.context_window == 131072
    assert _check(result, "context_window").status == "ok"


def test_probe_context_window_unknown_when_absent(serve: Callable[..., _Endpoint]) -> None:
    result = _probe(serve())

    assert result.context_window is None
    assert _check(result, "context_window").status == "unknown"


def test_probe_context_window_unknown_for_a_model_the_endpoint_does_not_list(
    serve: Callable[..., _Endpoint],
) -> None:
    result = _probe(serve(models=(200, {"data": [{"id": "some-other-model", "max_model_len": 8}]})))

    assert result.context_window is None


def test_probe_context_window_override_wins_without_asking(serve: Callable[..., _Endpoint]) -> None:
    endpoint = serve(models=(200, {"data": [{"id": _MODEL, "max_model_len": 131072}]}))

    result = _probe(endpoint, context_window=32000)

    assert result.context_window == 32000
    assert "models" not in endpoint.log


# ---------------------------------------------------------------------------
# Throughput and vision
# ---------------------------------------------------------------------------


def test_probe_throughput_observed(serve: Callable[..., _Endpoint]) -> None:
    result = _probe(serve())

    assert result.observed_tokens_per_second is not None
    assert result.observed_tokens_per_second > 0


def test_probe_throughput_unknown_without_a_usage_block(serve: Callable[..., _Endpoint]) -> None:
    result = _probe(serve(chat=(200, {"choices": [{"message": {"content": "ok"}}]})))

    assert result.observed_tokens_per_second is None
    assert _check(result, "throughput").status == "unknown"


def test_probe_reports_round_trip_but_not_first_token_latency(serve: Callable[..., _Endpoint]) -> None:
    """A non-streaming probe cannot see the first token, so it does not claim to."""

    result = _probe(serve())

    assert result.latency_first_token_ms is None
    assert isinstance(_check(result, "chat_completion").value, float)


def test_probe_vision_not_attempted_by_default(serve: Callable[..., _Endpoint]) -> None:
    endpoint = serve()

    result = _probe(endpoint)

    assert result.supports_vision is None
    check = _check(result, "vision")
    assert check.status == "unknown"
    assert check.detail == "not probed"
    assert "vision" not in endpoint.log


def test_probe_vision_unknown_when_the_reply_never_saw_the_image(serve: Callable[..., _Endpoint]) -> None:
    """The reported defect: a text-only server was recorded as vision-capable.

    ``_DEFAULT_REPLIES["vision"]`` is a plain 200 saying "ok" -- exactly what a
    text-only endpoint does with a multipart message: drop the image part it
    cannot parse and answer the text. Accepting that as proof wrote
    ``vision=yes`` into ``lc model list`` for two models on a server that has no
    vision at all. Answering ``unknown`` is the honest reading, and it is the
    one the ``?`` marker in the listing already exists for.
    """

    endpoint = serve()

    result = _probe(endpoint, probe_vision=True)

    assert result.supports_vision is None
    assert "vision" in endpoint.log, "the image was still sent -- only the verdict changed"
    check = _check(result, "vision")
    assert check.status == "unknown"
    assert "did not name the image's colour" in check.detail


def test_probe_vision_ok_when_the_reply_names_what_the_image_showed(serve: Callable[..., _Endpoint]) -> None:
    """``yes`` needs an answer only a model that decoded the image could give."""

    endpoint = serve(vision=(200, _reply("green")))

    result = _probe(endpoint, probe_vision=True)

    assert result.supports_vision is True
    assert _check(result, "vision").detail == "named the test image's colour (green)"


def test_probe_vision_unknown_when_the_reply_hedges_across_colours(serve: Callable[..., _Endpoint]) -> None:
    """A model listing the palette has not seen the image; one lucky word is not evidence."""

    result = _probe(serve(vision=(200, _reply("maybe red, green or blue"))), probe_vision=True)

    assert result.supports_vision is None
    assert _check(result, "vision").status == "unknown"


def test_probe_vision_sends_a_real_inline_image() -> None:
    """The payload has to *be* an image with content, or there is nothing to name.

    The previous payload was a 1x1 transparent pixel: no correct answer exists
    for it, which is why any reply at all had to be accepted.
    """

    from lemoncrow.pro.capabilities.model_setup.probe import _VISION_IMAGE

    prefix, _, encoded = _VISION_IMAGE.partition(",")
    assert prefix == "data:image/png;base64"
    raw = base64.b64decode(encoded)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n"
    width, height, _depth, colour_type = struct.unpack(">IIBB", raw[16:26])
    assert (width, height) == (32, 32), "a 1x1 image is below several encoders' minimum tile"
    assert colour_type == 2, "truecolour, no alpha: the image must have a colour to name"


def test_probe_vision_unsupported_on_400(serve: Callable[..., _Endpoint]) -> None:
    result = _probe(serve(vision=(400, {"error": "image input is not supported"})), probe_vision=True)

    assert result.supports_vision is False


# ---------------------------------------------------------------------------
# Degradation: nothing raises, nothing hangs
# ---------------------------------------------------------------------------


def test_probe_never_raises_on_connection_refused() -> None:
    result = probe_model(
        f"http://127.0.0.1:{_closed_port()}/v1",
        _MODEL,
        endpoint_name="down",
        timeout=2.0,
    )

    assert result.ok is False
    assert result.supports_tool_use is None
    assert result.supports_structured_output is None
    assert result.context_window is None
    assert result.observed_tokens_per_second is None
    # Every check that needed the endpoint failed; vision was never attempted.
    assert {check.status for check in result.checks if check.name != "vision"} == {"error"}
    assert _check(result, "vision").detail == "not probed"


def test_probe_timeout_yields_unknown_and_does_not_hang(serve: Callable[..., _Endpoint]) -> None:
    """One slow endpoint costs one timeout, not one timeout per check."""

    endpoint = serve(delay=2.0)

    result = _probe(endpoint, timeout=0.25)

    assert result.ok is False
    assert result.supports_tool_use is None
    assert result.supports_structured_output is None
    assert result.context_window is None
    # The chat probe timed out, so the remaining live checks were skipped
    # rather than each buying another full timeout.
    assert endpoint.log == ["chat"]
    assert "not attempted" in _check(result, "tool_calling").detail


def test_probe_survives_a_body_that_is_not_json(serve: Callable[..., _Endpoint]) -> None:
    """A proxy error page in front of the endpoint teaches us nothing about it."""

    html = (200, "<html>502 Bad Gateway</html>")
    result = _probe(serve(chat=html, tools=html, structured=html, models=html))

    assert result.ok is False
    assert _check(result, "chat_completion").status == "error"
    assert result.supports_tool_use is None
    assert result.supports_structured_output is None
    assert result.context_window is None


def test_probe_error_detail_is_bounded(serve: Callable[..., _Endpoint]) -> None:
    """A hostile error page must not become a 4 MB string inside providers.json."""

    result = _probe(serve(chat=(500, "x" * 50_000)))

    assert len(_check(result, "chat_completion").detail) <= 200


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


@pytest.fixture
def registered(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A root with one endpoint registered, without touching the network."""

    monkeypatch.setattr(endpoints_mod, "discover_endpoint_models", lambda *a, **k: [_MODEL])
    add_endpoint(tmp_path, "http://localhost:8000/v1", name="box")
    return tmp_path


def _result(**overrides: Any) -> ProbeResult:
    fields: dict[str, Any] = {
        "schema_version": 1,
        "endpoint": "http://localhost:8000/v1",
        "endpoint_name": "box",
        "raw_model_id": _MODEL,
        "model_id": f"custom/box/{_MODEL}",
        "checks": (),
        "context_window": 131072,
        "supports_tool_use": True,
        "supports_structured_output": True,
        "observed_tokens_per_second": 41.2,
        "probed_at": "2026-09-08T10:11:12+00:00",
        "ok": True,
    }
    fields.update(overrides)
    return ProbeResult(**fields)


def test_record_probe_persists_capabilities(registered: Path) -> None:
    assert record_probe(registered, _result()) is True

    stored = json.loads(providers_config_path(registered).read_text(encoding="utf-8"))
    entry = stored["custom"]["endpoints"]["box"]["models"][_MODEL]
    assert entry["supports_tool_use"] is True
    assert entry["context_window"] == 131072
    assert entry["observed_tokens_per_second"] == 41.2
    assert entry["probed_at"] == "2026-09-08T10:11:12+00:00"
    assert entry["tier"] == "cheap"


def test_record_probe_keeps_the_file_at_mode_600(registered: Path) -> None:
    record_probe(registered, _result())

    assert providers_config_path(registered).stat().st_mode & 0o777 == 0o600


def test_record_probe_does_not_erase_a_previous_measurement(registered: Path) -> None:
    """An endpoint that was down during a re-probe must not lose what we knew."""

    record_probe(registered, _result())
    record_probe(
        registered,
        _result(supports_tool_use=None, context_window=None, observed_tokens_per_second=None, ok=False),
    )

    endpoint = get_endpoint(registered, "box")
    assert endpoint is not None
    assert endpoint.models[0].supports_tool_use is True
    assert endpoint.models[0].context_window == 131072


def test_record_probe_overwrites_with_a_measured_negative(registered: Path) -> None:
    record_probe(registered, _result())
    record_probe(registered, _result(supports_tool_use=False))

    endpoint = get_endpoint(registered, "box")
    assert endpoint is not None
    assert endpoint.models[0].supports_tool_use is False


def test_record_probe_returns_false_for_an_unregistered_endpoint(registered: Path) -> None:
    assert record_probe(registered, _result(endpoint_name="ghost")) is False


def test_record_probe_registers_a_model_the_endpoint_gained(registered: Path) -> None:
    assert record_probe(registered, _result(raw_model_id="new-model")) is True

    endpoint = get_endpoint(registered, "box")
    assert endpoint is not None
    assert {model.raw_id for model in endpoint.models} == {_MODEL, "new-model"}


# ---------------------------------------------------------------------------
# Routing eligibility
# ---------------------------------------------------------------------------


def test_user_candidate_defaults_tool_use_false_when_unknown(registered: Path) -> None:
    """Unknown must not be routed to optimistically: supports_turn would accept it."""

    candidates = endpoints_mod.user_candidate_models(registered)

    assert [candidate.model_id for candidate in candidates] == [f"custom/box/{_MODEL}"]
    assert candidates[0].supports_tool_use is False
    assert candidates[0].vendor == "custom"
    assert candidates[0].tier == "cheap"


def test_user_candidate_uses_the_probed_capabilities(registered: Path) -> None:
    record_probe(registered, _result())

    candidate = endpoints_mod.user_candidate_models(registered)[0]

    assert candidate.supports_tool_use is True
    assert candidate.context_window == 131072


def test_user_candidate_context_window_falls_back_when_unmeasured(registered: Path) -> None:
    candidate = endpoints_mod.user_candidate_models(registered)[0]

    assert candidate.context_window == 8192


def test_user_candidate_is_priced_at_zero_not_at_a_vendor_rate(registered: Path) -> None:
    """A box the user already owns must not be priced like a hosted model."""

    candidate = endpoints_mod.user_candidate_models(registered)[0]

    assert candidate.pricing.input == 0.0
    assert candidate.pricing.output == 0.0


def test_user_candidates_are_empty_on_a_broken_providers_json(tmp_path: Path) -> None:
    path = providers_config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    assert endpoints_mod.user_candidate_models(tmp_path) == []


def test_user_candidates_appear_in_build_candidates(registered: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from lemoncrow.pro.capabilities.counterfactual import pricing as pricing_mod

    record_probe(registered, _result())
    # _build_candidates() calls user_candidate_models() with no root, so the
    # default store root is what routing actually reads.
    monkeypatch.setenv("LEMONCROW_ROOT", str(registered))

    ids = {candidate.model_id for candidate in pricing_mod._build_candidates()}

    assert f"custom/box/{_MODEL}" in ids


def test_build_candidates_survives_a_broken_providers_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Routing must never go down because a hand-edited config lost a brace."""

    from lemoncrow.pro.capabilities.counterfactual import pricing as pricing_mod

    path = providers_config_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")
    monkeypatch.setenv("LEMONCROW_ROOT", str(tmp_path))

    assert len(pricing_mod._build_candidates()) > 0


def test_build_candidates_survives_a_raising_endpoint_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    """The guard is around the whole call, not just around JSON parsing."""

    from lemoncrow.pro.capabilities.counterfactual import pricing as pricing_mod

    def _boom(root: Path | None = None) -> list[Any]:
        raise RuntimeError("providers.json is on fire")

    monkeypatch.setattr(endpoints_mod, "user_candidate_models", _boom)

    assert len(pricing_mod._build_candidates()) > 0
