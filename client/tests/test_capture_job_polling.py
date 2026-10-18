from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from lemoncrow_client import session as session_module
from lemoncrow_client.errors import ClientError, ErrorCode
from lemoncrow_client.review_session import ReviewSession
from lemoncrow_client.session import RemoteSession
from lemoncrow_client.transport import Response


class _Resp:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self.payload, self.status = payload, status

    def require(self) -> dict[str, Any]:
        # Match the real transport, including its decision to interpret any
        # top-level "error" key as a refusal, even on HTTP 200.
        return dict(Response(self.status, self.payload).require())


class _Stub(RemoteSession):
    def _auth(self, *, with_revision: bool = True) -> dict[str, str]:
        return {}


def _session(replies: list[_Resp]) -> RemoteSession:
    remote = object.__new__(_Stub)
    remote._config = SimpleNamespace(request_timeout_s=1.0)  # type: ignore[attr-defined]
    remote._transport = SimpleNamespace(get=lambda *_a, **_k: replies.pop(0))  # type: ignore[attr-defined]
    return remote


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(session_module.time, "sleep", lambda _s: None)


RUNNING = {"job_id": "rjob_1", "state": "running", "poll_after_s": 1.0}


def test_synchronous_answer_passes_through() -> None:
    review = {"review": {"id": "r1"}}
    assert _session([])._await_capture("/v1/repos/x/reviews", review) == review


def test_polls_until_done() -> None:
    remote = _session([_Resp(RUNNING), _Resp({"state": "done", "result": {"review": {"id": "r1"}}})])
    assert remote._await_capture("/v1/repos/x/reviews", RUNNING) == {"review": {"id": "r1"}}


def test_file_progress_is_forwarded_on_each_poll_until_done() -> None:
    feedback = {"detail": "Building review targets · 12/2249 files"}
    remote = _session(
        [
            _Resp({**RUNNING, "progress": feedback}),
            _Resp({**RUNNING, "progress": feedback}),
            _Resp({"state": "done", "result": {"review": {"id": "r1"}}}),
        ]
    )
    seen: list[str] = []
    assert remote._await_capture("/v1/repos/x/reviews", RUNNING, progress=seen.append) == {"review": {"id": "r1"}}
    assert seen == [feedback["detail"], feedback["detail"]]


def test_failed_job_raises_the_server_error() -> None:
    error = {"error": {"code": "payload_invalid", "message": "bad packet"}}
    remote = _session([_Resp({"state": "failed", "error": error, "status": 400})])
    with pytest.raises(ClientError, match="bad packet") as failure:
        remote._await_capture("/v1/repos/x/reviews", RUNNING)
    assert failure.value.code is ErrorCode.PAYLOAD_INVALID


def test_failed_job_preserves_the_discarded_review_conflict() -> None:
    error = {
        "error": {
            "code": "review_state_conflict",
            "message": "review is discarded and read-only; run lc review --reopen-review",
            "action": "fix_request",
        }
    }
    response = _Resp({"job_id": "rjob_1", "state": "failed", "error": error, "status": 409})
    with pytest.raises(ClientError, match="discarded and read-only") as raised:
        _session([response])._await_capture("/v1/repos/x/reviews", RUNNING)
    assert raised.value.server_code == "review_state_conflict"
    assert raised.value.message.endswith("--reopen-review")


def test_lost_job_asks_for_a_fresh_run() -> None:
    gone = _Resp({"error": {"code": "review_unknown", "message": "unknown"}}, status=404)
    with pytest.raises(ClientError) as raised:
        _session([gone])._await_capture("/v1/repos/x/reviews", RUNNING)
    assert raised.value.code is ErrorCode.SERVER_SESSION_UNAVAILABLE


def test_review_only_capture_requests_async_and_returns_polled_result() -> None:
    class ReviewStub(ReviewSession):
        def _auth(self, *, with_revision: bool = True) -> dict[str, str]:
            return {}

    remote = object.__new__(ReviewStub)
    remote._config = SimpleNamespace(request_timeout_s=1.0, tool_timeout_s=1.0)
    remote._state = SimpleNamespace(
        repo_id="x",
        view_id="view",
        view_revision=1,
        review_manifest=SimpleNamespace(require_capture=lambda *_a: None),
    )
    requests = []
    review = {"review": {"id": "r1"}}

    def post(path, **kwargs):
        requests.append((path, kwargs["headers"]))
        return _Resp(RUNNING, status=202)

    def get(path, **kwargs):
        assert path == "/v1/repos/x/reviews/jobs/rjob_1"
        return _Resp({"state": "done", "result": review})

    remote._transport = SimpleNamespace(post=post, get=get)
    assert remote.capture_from_views(range_spec={}, source_ref="test") == review
    assert requests == [("/v1/repos/x/reviews", {"Prefer": "respond-async"})]
