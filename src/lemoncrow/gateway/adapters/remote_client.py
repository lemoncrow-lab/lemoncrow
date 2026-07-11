"""HTTP client for the LemonCrow service API (remote MCP mode).

Uses the stdlib ``urllib.request`` only — no extra runtime deps.
All network I/O is synchronous and bounded by *timeout* (default 30s).

Security notes:
- API key is NEVER logged.
- Response bodies are size-capped to prevent memory exhaustion.
- Uses ``ssl.create_default_context()`` for TLS validation.
"""

from __future__ import annotations

import json
import logging
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
    """Thin HTTP client for the LemonCrow service API.

    Args:
        base_url: Base URL of the service, e.g. ``http://localhost:8787``.
        api_key:  Bearer token.  If *None*, read from ``LEMONCROW_API_KEY``.
        timeout:  Request timeout in seconds.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        base = base_url or os.environ.get("LEMONCROW_SERVICE_URL") or "http://localhost:8787"
        self._base = base.rstrip("/")
        # Never log or expose the key.
        self._api_key: str | None = api_key or os.environ.get("LEMONCROW_API_KEY") or None
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
    ) -> Any:
        url = f"{self._base}{path}"
        data = json.dumps(body).encode() if body is not None else None
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout, context=self._ssl_ctx) as resp:
                raw = resp.read(_MAX_BODY_BYTES)
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            try:
                err_body = exc.read(_MAX_BODY_BYTES).decode(errors="replace")
            except Exception:
                logging.exception("Recovered from broad exception handler")
                err_body = ""
            return {"ok": False, "error": f"HTTP {exc.code}", "detail": err_body}
        except urllib.error.URLError as exc:
            return {"ok": False, "error": "service unavailable", "detail": str(exc.reason)}
        except TimeoutError:
            return {"ok": False, "error": "timeout", "detail": f"exceeded {self._timeout}s"}
        except Exception as exc:
            logging.exception("Recovered from broad exception handler")
            return {"ok": False, "error": "client error", "detail": str(exc)}

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", path, body)

    def _get(self, path: str) -> Any:
        return self._request("GET", path)

    # ------------------------------------------------------------------ #
    # Service tools (mirror of MCP local tools)                          #
    # ------------------------------------------------------------------ #

    def get_context(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/reasoning/context", args)

    def rescue_failure(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/reasoning/rescue", args)

    def run_rubric_gate(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/rubrics/run", args)

    def record_trace(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/traces", args)

    def memory(self, args: dict[str, Any]) -> dict[str, Any]:
        import hashlib

        op = str(args.get("op") or "")
        if op == "block_upsert":
            return self._post("/v1/memory/blocks", args)
        if op == "block_get":
            query_params = {"label": str(args.get("label") or "")}
            if args.get("agent_id"):
                query_params["agent_id"] = str(args.get("agent_id"))
            query = urllib.parse.urlencode(query_params)
            return self._get(f"/v1/memory/blocks?{query}")
        if op == "archive":
            return self._post("/v1/memory/archive", args)
        if op == "recall":
            return self._post("/v1/memory/recall", args)
        if op == "store_fact":
            scope = str(args.get("scope") or "").strip().lower()
            if scope not in {"repository", "user"}:
                raise ValueError("scope must be one of: repository, user")
            subject = str(args.get("subject") or "").strip()
            fact = str(args.get("fact") or "").strip()
            citations = str(args.get("citations") or "").strip()
            reason = str(args.get("reason") or "").strip()
            if not subject or not fact or not citations or not reason:
                raise ValueError("store_fact requires subject, fact, citations, and reason")
            digest = hashlib.sha256(f"{scope}:{subject}:{fact}".encode()).hexdigest()[:12]
            subject_slug = "".join(ch if ch.isalnum() else "-" for ch in subject.lower()).strip("-") or "memory"
            payload = {
                "agent_id": str(args.get("agent_id") or "shared"),
                "label": f"memory-fact/{scope}/{subject_slug}/{digest}",
                "value": fact,
                "pinned": True,
                "metadata": {
                    "kind": "memory_fact",
                    "subject": subject,
                    "fact": fact,
                    "citations": citations,
                    "reason": reason,
                    "scope": scope,
                    "votes": {"upvote": 0, "downvote": 0},
                    "vote_history": [],
                },
            }
            return self._post("/v1/memory/blocks", payload)
        if op == "vote_fact":
            agent_id = str(args.get("agent_id") or "shared")
            fact = str(args.get("fact") or "").strip()
            direction = str(args.get("direction") or "").strip().lower()
            vote_reason = str(args.get("reason") or "").strip()
            scope = str(args.get("scope") or "").strip().lower()
            if not fact or not vote_reason:
                raise ValueError("vote_fact requires fact and reason")
            if direction not in {"upvote", "downvote"}:
                raise ValueError("direction must be one of: upvote, downvote")
            list_limit = 500
            # Retry on a 409 version conflict: a concurrent vote bumped the
            # block's version, so re-read and re-apply the increment instead of
            # dropping this vote.
            result: dict[str, Any] = {"ok": False, "error": "vote_fact: no attempt made"}
            for _attempt in range(3):
                blocks = self._get(f"/v1/memory/blocks?agent_id={urllib.parse.quote(agent_id)}&limit={list_limit}")
                if not isinstance(blocks, list):
                    raise ValueError("unable to list memory blocks for vote_fact")
                target = None
                for block in blocks:
                    metadata = block.get("metadata") or {}
                    if metadata.get("kind") != "memory_fact":
                        continue
                    if str(metadata.get("fact", "")) != fact:
                        continue
                    if scope and str(metadata.get("scope", "")) != scope:
                        continue
                    target = block
                    break
                if target is None:
                    if len(blocks) >= list_limit:
                        raise ValueError(
                            f"no matching stored fact found for vote_fact within the first {list_limit} blocks; "
                            "the fact may exist beyond the listing limit"
                        )
                    raise ValueError("no matching stored fact found for vote_fact")
                metadata = dict(target.get("metadata") or {})
                votes = dict(metadata.get("votes") or {})
                up = int(votes.get("upvote", 0) or 0)
                down = int(votes.get("downvote", 0) or 0)
                if direction == "upvote":
                    up += 1
                else:
                    down += 1
                history = list(metadata.get("vote_history") or [])
                history.append({"direction": direction, "reason": vote_reason})
                metadata["votes"] = {"upvote": up, "downvote": down}
                metadata["vote_history"] = history[-20:]
                payload = {
                    "agent_id": agent_id,
                    "label": str(target.get("label") or ""),
                    "value": str(target.get("value") or ""),
                    "metadata": metadata,
                    "expected_version": int(target.get("version") or 1),
                }
                result = self._post("/v1/memory/blocks", payload)
                if isinstance(result, dict) and result.get("error") == "HTTP 409":
                    continue
                return result
            return result
        raise ValueError(f"memory op not supported in remote mode: {op}")

    def lesson_inbox(self, args: dict[str, Any]) -> dict[str, Any]:
        domain = args.get("domain")
        limit = args.get("limit")
        query: list[str] = []
        if domain:
            query.append(f"domain={urllib.parse.quote(str(domain))}")
        if limit is not None:
            query.append(f"limit={int(limit)}")
        suffix = f"?{'&'.join(query)}" if query else ""
        return self._get(f"/v1/lessons/inbox{suffix}")

    def lesson_decide(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._post("/v1/lessons/decide", args)
