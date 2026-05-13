"""HTTP client for the Atelier service API (remote MCP mode).

Uses the stdlib ``urllib.request`` only — no extra runtime deps.
All network I/O is synchronous and bounded by *timeout* (default 30s).

Security notes:
- API key is NEVER logged.
- Response bodies are size-capped to prevent memory exhaustion.
- Uses ``ssl.create_default_context()`` for TLS validation.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Maximum response body size accepted (4 MB).
_MAX_BODY_BYTES = 4 * 1024 * 1024

_DEFAULT_TIMEOUT = 30


class RemoteClient:
    """Thin HTTP client for the Atelier service API.

    Args:
        base_url: Base URL of the service, e.g. ``http://localhost:8787``.
        api_key:  Bearer token.  If *None*, read from ``ATELIER_API_KEY``.
        timeout:  Request timeout in seconds.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        base = base_url or os.environ.get("ATELIER_SERVICE_URL") or "http://localhost:8787"
        self._base = base.rstrip("/")
        # Never log or expose the key.
        self._api_key: str | None = api_key or os.environ.get("ATELIER_API_KEY") or None
        self._timeout = timeout
        self._ssl_ctx = ssl.create_default_context()

    # ------------------------------------------------------------------ #
    # Internal helpers                                                    #
    # ------------------------------------------------------------------ #

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self._base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ssl_ctx) as resp:
                raw = resp.read(_MAX_BODY_BYTES)
                return json.loads(raw)  # type: ignore[no-any-return]
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read(_MAX_BODY_BYTES).decode(errors="replace")
            except Exception:
                err_body = ""
            return {"ok": False, "error": f"HTTP {exc.code}", "detail": err_body}
        except urllib.error.URLError as exc:
            return {"ok": False, "error": "service unavailable", "detail": str(exc.reason)}
        except TimeoutError:
            return {"ok": False, "error": "timeout", "detail": f"exceeded {self._timeout}s"}
        except Exception as exc:
            return {"ok": False, "error": "client error", "detail": str(exc)}

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body)

    def _get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    # ------------------------------------------------------------------ #
    # Service tools (mirror of MCP local tools)                          #
    # ------------------------------------------------------------------ #

    def get_task_context(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/reasoning/context", args)

    def rescue_failure(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/reasoning/rescue", args)

    def run_rubric_gate(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/rubrics/run", args)

    def record_trace(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/traces", args)

    def memory(self, args: dict[str, Any]) -> dict[str, Any]:
        op = str(args.get("op") or "")
        if op == "block_upsert":
            return self._post("/v1/memory/blocks", args)
        if op == "block_get":
            query = urllib.parse.urlencode(
                {
                    "agent_id": str(args.get("agent_id") or ""),
                    "label": str(args.get("label") or ""),
                }
            )
            return self._get(f"/v1/memory/blocks?{query}")
        if op == "archive":
            return self._post("/v1/memory/archive", args)
        if op == "recall":
            return self._post("/v1/memory/recall", args)
        raise ValueError(f"memory op not supported in remote mode: {op}")

    def lesson_inbox(self, args: dict[str, Any]) -> dict[str, Any]:
        domain = args.get("domain")
        limit = args.get("limit")
        query: list[str] = []
        if domain:
            query.append(f"domain={domain}")
        if limit is not None:
            query.append(f"limit={int(limit)}")
        suffix = f"?{'&'.join(query)}" if query else ""
        return self._get(f"/v1/lessons/inbox{suffix}")

    def lesson_decide(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/lessons/decide", args)
