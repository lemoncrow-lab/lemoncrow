"""Public aggregate savings rollup publisher.

Sends only the anonymous aggregate fields used by the public landing-page
counters (saved_usd, tokens_saved, calls_avoided, turn_count).  Always on—
no opt-out.  Install IDs and session IDs are SHA-256 hashed before leaving
the process.  Never raises into hooks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from atelier.core.foundation.identity import get_anon_id

logger = logging.getLogger("atelier.product.telemetry.public_rollup")


def _hash_hex(value: str) -> str:
    """One-way hash so only an opaque key ever leaves the machine.

    Must match the formula the server previously applied to raw ids
    (functions/api/telemetry/rollup.ts), so already-stored session/install
    keys stay stable across the cutover.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


DEFAULT_PUBLIC_ROLLUP_ENDPOINT = "https://atelier.ws/api/telemetry/rollup"
DEFAULT_TIMEOUT_SECONDS = 0.75


def publish_public_savings_rollup(
    *,
    session_id: str,
    saved_usd: float,
    tokens_saved: int,
    calls_avoided: int,
    turn_count: int,
    source: str,
    occurred_at: datetime | None = None,
    carry_usd: float = 0.0,
    carry_tokens: int = 0,
    est_cost_usd: float = 0.0,
    time_saved_seconds: float = 0.0,
    output_saved_tokens: int = 0,
    output_saved_usd: float = 0.0,
) -> bool:
    """Publish one sanitized savings rollup (a single session, or one daily aggregate).

    Returns whether the request was accepted. All failures are swallowed so
    telemetry cannot break a user session or hook.
    """

    try:
        endpoint = public_rollup_endpoint()
        if not endpoint:
            return False

        payload = _payload(
            session_id=session_id,
            saved_usd=saved_usd,
            tokens_saved=tokens_saved,
            calls_avoided=calls_avoided,
            turn_count=turn_count,
            source=source,
            occurred_at=occurred_at,
            carry_usd=carry_usd,
            carry_tokens=carry_tokens,
            est_cost_usd=est_cost_usd,
            time_saved_seconds=time_saved_seconds,
            output_saved_tokens=output_saved_tokens,
            output_saved_usd=output_saved_usd,
        )
        if payload is None:
            return False
        return _post_json(endpoint, payload, timeout_s=public_rollup_timeout_seconds())
    except Exception as exc:  # noqa: BLE001
        logger.debug("public_rollup.publish_failed", extra={"error": str(exc)})
        return False


def public_rollup_endpoint() -> str:
    """Return the rollup endpoint URL.

    Override via ATELIER_PUBLIC_TELEMETRY_ENDPOINT (useful for self-hosting
    or local dev).  Falls back to the production endpoint.
    """
    raw = os.environ.get("ATELIER_PUBLIC_TELEMETRY_ENDPOINT", DEFAULT_PUBLIC_ROLLUP_ENDPOINT).strip()
    return raw if raw else DEFAULT_PUBLIC_ROLLUP_ENDPOINT


def public_rollup_timeout_seconds() -> float:
    raw = os.environ.get("ATELIER_PUBLIC_TELEMETRY_TIMEOUT_MS", "")
    try:
        ms = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    if ms <= 0:
        return DEFAULT_TIMEOUT_SECONDS
    return max(0.1, min(ms / 1000.0, 5.0))


def _payload(
    *,
    session_id: str,
    saved_usd: float,
    tokens_saved: int,
    calls_avoided: int,
    turn_count: int,
    source: str,
    occurred_at: datetime | None,
    carry_usd: float = 0.0,
    carry_tokens: int = 0,
    est_cost_usd: float = 0.0,
    time_saved_seconds: float = 0.0,
    output_saved_tokens: int = 0,
    output_saved_usd: float = 0.0,
) -> dict[str, Any] | None:
    session = str(session_id or "").strip()
    if not session:
        return None
    anon_id = get_anon_id()
    install_key = _hash_hex(anon_id)
    session_key = _hash_hex(f"{anon_id}:{session}")
    # Signed: real benchmarks can be net-negative. cost/turns/carry stay >= 0.
    saved = float(saved_usd or 0.0)
    tokens = int(tokens_saved or 0)
    calls = int(calls_avoided or 0)
    turns = max(0, int(turn_count or 0))
    carry_s = max(0.0, float(carry_usd or 0.0))
    carry_t = max(0, int(carry_tokens or 0))
    cost = max(0.0, float(est_cost_usd or 0.0))
    time_s = float(time_saved_seconds or 0.0)
    out_tok = max(0, int(output_saved_tokens or 0))
    out_usd = max(0.0, float(output_saved_usd or 0.0))
    # Skip only a wholly empty rollup; a negative-but-nonzero one is real signal.
    if not (saved or tokens or calls or turns or carry_s or carry_t or cost or time_s or out_tok or out_usd):
        return None
    at = occurred_at or datetime.now(UTC)
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return {
        "install_key": install_key,
        "session_key": session_key,
        "atelier_version": _service_version(),
        "source": _label(source, fallback="atelier", max_length=40),
        "saved_usd": round(saved, 6),
        "tokens_saved": tokens,
        "calls_avoided": calls,
        "carry_usd": round(carry_s, 6),
        "carry_tokens": carry_t,
        "turn_count": turns,
        "est_cost_usd": round(cost, 6),
        "time_saved_seconds": round(time_s, 3),
        "output_saved_tokens": out_tok,
        "output_saved_usd": round(out_usd, 6),
        "occurred_at": at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


def _post_json(endpoint: str, payload: dict[str, Any], *, timeout_s: float) -> bool:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"atelier/{payload.get('atelier_version', 'unknown')}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        logger.debug("public_rollup.post_failed", extra={"error": str(exc)})
        return False


def _service_version() -> str:
    try:
        return version("atelier")
    except PackageNotFoundError:
        return "0.1.0"


def _label(value: str, *, fallback: str, max_length: int) -> str:
    cleaned = "".join(ch for ch in str(value or fallback) if ch.isalnum() or ch in "_.:+/-")[:max_length]
    return cleaned or fallback


def flush_daily_public_rollup(root: str | Path, *, checkpoint_day: str | None) -> tuple[dict[str, Any], str | None]:
    """Publish at most one aggregated rollup for every UTC day fully elapsed
    since ``checkpoint_day``, computed directly from the canonical per-session
    savings ledger (:func:`atelier.core.capabilities.savings_summary.aggregate_savings_since_day`)
    -- no separate queue file to maintain.

    Returns ``(result, new_checkpoint_day)``; callers persist the returned
    checkpoint so each calendar day is reported exactly once, however often
    (or rarely) this is actually called.

    ``checkpoint_day is None`` means "never flushed before". Rather than
    resending a user's entire local history in one lump (which would double
    report every session the old always-on Stop-hook push already sent
    before this daily batching existed), the first call only establishes
    today as the baseline and reports nothing.
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if checkpoint_day is None:
        return {"flushed": False, "reason": "baseline"}, today

    from atelier.core.capabilities.savings_summary import (
        aggregate_savings_since_day,
        estimate_time_saved_seconds,
    )

    totals, last_day = aggregate_savings_since_day(root, since_day=checkpoint_day, today=today)
    if last_day is None:
        return {"flushed": False, "reason": "no_new_days"}, checkpoint_day

    ok = publish_public_savings_rollup(
        session_id=f"daily-rollup-{last_day}",
        saved_usd=float(totals["saved_usd"]),
        tokens_saved=int(totals["tokens_saved"]),
        calls_avoided=int(totals["calls_avoided"]),
        turn_count=int(totals["turn_count"]),
        source="claude",
        carry_usd=float(totals["carry_usd"]),
        est_cost_usd=float(totals["est_cost_usd"]),
        time_saved_seconds=estimate_time_saved_seconds(calls_avoided=int(totals["calls_avoided"])),
        output_saved_tokens=int(totals.get("output_saved_tokens", 0) or 0),
        output_saved_usd=float(totals.get("output_saved_usd", 0.0) or 0.0),
    )
    return {"flushed": ok, "through_day": last_day}, last_day
