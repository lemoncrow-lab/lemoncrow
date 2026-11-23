"""The one request behind anonymous feature counts (:mod:`.usage`): a JSON POST that follows no redirect."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def post_json(url: str, payload: dict[str, Any], timeout_s: float) -> int:
    """POST *payload*; the HTTP status, or 0 when the request did not complete."""

    request = urllib.request.Request(
        url,
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        # The production endpoint's bot filter refuses urllib's default agent.
        headers={"Content-Type": "application/json", "User-Agent": f"lemoncrow/{payload['lemoncrow_version']}"},
        method="POST",
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout_s) as response:
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return int(exc.code)
    except (OSError, ValueError):
        return 0
