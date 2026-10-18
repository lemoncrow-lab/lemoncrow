"""Bounded polling shared by the isolated Review and combined Runtime clients."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from .errors import AgentAction, ClientError, ErrorCode

_CAPTURE_JOB_DEADLINE_S = 1800.0


def await_capture(
    transport: Any,
    auth: Callable[[], Mapping[str, str]],
    request_timeout_s: float,
    path: str,
    answer: Mapping[str, Any],
    progress: Callable[[str], None] | None = None,
) -> Mapping[str, Any]:
    """Poll an async capture; older servers' synchronous answers pass through."""
    job_id = answer.get("job_id")
    if answer.get("state") != "running" or not isinstance(job_id, str):
        return answer
    job_path = f"{path}/jobs/{quote(job_id, safe='')}"
    deadline = time.monotonic() + _CAPTURE_JOB_DEADLINE_S
    failures = 0
    while True:
        if time.monotonic() > deadline:
            raise ClientError(
                ErrorCode.SERVER_UNREACHABLE,
                f"the Review capture is still running after {_CAPTURE_JOB_DEADLINE_S:.0f}s",
                details={"timed_out": True},
                action=AgentAction.RETRY_LATER,
            )
        time.sleep(min(max(float(answer.get("poll_after_s") or 1.0), 0.25), 5.0))
        try:
            response = transport.get(job_path, headers=auth(), timeout_s=request_timeout_s)
            # A completed async failure is itself an HTTP 200 job-status
            # envelope with an embedded typed HTTP error. It is not an HTTP
            # refusal; parse its inner error below. Calling require() here
            # would misread that envelope as an error with no code/message.
            answer = (
                response.payload
                if response.status < 400 and response.payload.get("state") == "failed"
                else response.require()
            )
        except ClientError as exc:
            if exc.code is ErrorCode.REVIEW_UNKNOWN:
                raise ClientError(
                    ErrorCode.SERVER_SESSION_UNAVAILABLE,
                    "the server lost the Review capture (restarted or expired); run the review again",
                    action=AgentAction.RETRY_LATER,
                ) from exc
            failures += 1
            if exc.code not in {ErrorCode.SERVER_UNREACHABLE, ErrorCode.SERVER_SESSION_UNAVAILABLE} or failures > 5:
                raise
            continue
        failures = 0
        feedback = answer.get("progress")
        if progress is not None and isinstance(feedback, Mapping) and isinstance(feedback.get("detail"), str):
            progress(feedback["detail"])
        state = answer.get("state")
        if state == "done":
            result = answer.get("result")
            return result if isinstance(result, Mapping) else {}
        if state == "failed":
            raise ClientError.from_wire(
                answer.get("error") if isinstance(answer.get("error"), Mapping) else {},
                status=int(answer.get("status") or 500),
            )
