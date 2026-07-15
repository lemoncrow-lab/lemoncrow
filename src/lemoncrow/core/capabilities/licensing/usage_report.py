"""Client-side usage reporter — feeds the server-side meter (the cap's source of truth).

Batched + watermarked: reports only the delta since the last successful report,
throttled to :data:`REPORT_INTERVAL_SECONDS` (30 min), and skips entirely when
there is no new data — an idle process posts nothing. The client sends RAW usage
(token counts + its own estimate); the SERVER prices it authoritatively, so the
client can't lower the cap by faking dollars. Fail-open: any error leaves the
watermark unmoved so the delta retries next tick.

Wiring: call :func:`maybe_report_usage` from the background reconciler loop and
the Stop hook. Requires a signed-in account (anonymous/local reports nothing).
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.licensing import store
from lemoncrow.core.capabilities.licensing.entitlements import USER_AGENT

REPORT_INTERVAL_SECONDS = 30 * 60
# ~100 years: large enough that aggregate_window_savings clamps its day cutoff
# to the epoch, summing EVERY day bucket -> a monotonic lifetime total (see
# report_usage_once for why monotonicity matters).
_LIFETIME_DAYS = 36_500

# Returns the parsed JSON response body on success (may be empty), or None on
# failure. A bool is also accepted (legacy/tests): True == success, no body.
_HttpPost = Callable[[str, dict[str, Any], str], "dict[str, Any] | bool | None"]


def _watermark_path(root: str | Path) -> Path:
    return Path(root) / "usage_report_watermark.json"


def _read_watermark(root: str | Path) -> dict[str, Any]:
    p = _watermark_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_watermark(root: str | Path, data: dict[str, Any]) -> None:
    p = _watermark_path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass


def _default_post(url: str, payload: dict[str, Any], token: str) -> dict[str, Any] | None:
    """POST the payload; return the parsed JSON response, or None on failure.

    The response carries the freshly signed ``capVerdictToken`` (server-computed
    from the accumulated meter); the caller persists it. A 2xx with no/invalid
    JSON body still counts as success — returns ``{}`` so the watermark advances.
    """
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if not (200 <= resp.status < 300):
                return None
            try:
                parsed = json.loads(resp.read())
            except (ValueError, OSError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
    except Exception:  # noqa: BLE001 — network failure = retry next tick
        return None


def report_usage_once(
    root: str | Path,
    *,
    http_post: _HttpPost | None = None,
    now: int | None = None,
    watermark: dict[str, Any] | None = None,
) -> bool:
    """Report the usage delta since the last watermark. Returns True if posted.

    No signed-in token, or no new data since last report -> returns False without
    posting. On a successful post, advances the watermark to the current totals.
    """
    token = store.load_auth_token()
    if not token:
        return False
    try:
        from lemoncrow.core.capabilities.savings_summary import aggregate_window_savings

        # Report deltas of a MONOTONIC lifetime total, never a trailing window:
        # a rolling window shrinks as old days age out, so diffing it against a
        # monotonic watermark drops re-grown savings and the server under-meters
        # (the cap silently stops enforcing). _LIFETIME_DAYS makes the aggregate
        # clamp its cutoff to the epoch -> every day bucket summed -> a
        # non-decreasing cumulative total. The server buckets these positive
        # deltas by receipt-day into its own rolling 30-day window.
        lifetime = aggregate_window_savings(Path(root), days=_LIFETIME_DAYS)
        saved_usd = round(max(0.0, float(getattr(lifetime, "saved_usd", 0.0))), 4)
        spend_usd = round(max(0.0, float(getattr(lifetime, "spend_usd", 0.0))), 4)
    except Exception:  # noqa: BLE001
        return False

    wm = _read_watermark(root) if watermark is None else watermark
    prior_saved = float(wm.get("reported_saved_usd") or 0.0)
    prior_spend = float(wm.get("reported_spend_usd") or 0.0)
    delta_saved = round(saved_usd - prior_saved, 4)
    delta_spend = round(spend_usd - prior_spend, 4)
    if delta_saved <= 0.0 and delta_spend <= 0.0:
        return False  # no new data -> nothing to report

    stamp = int(time.time()) if now is None else now
    payload = {
        # Lifetime cumulative totals (monotonic); kept under these wire names
        # for compatibility. The server accumulates the deltas, not these.
        "window_saved_usd": saved_usd,
        "window_spend_usd": spend_usd,
        "delta_saved_usd": max(0.0, delta_saved),
        "delta_spend_usd": max(0.0, delta_spend),
        "reported_at": stamp,
    }
    post = http_post or _default_post
    result = post(f"{store.load_auth_base()}/api/usage/report", payload, token)
    if result is None or result is False:
        return False  # network/HTTP failure -> leave watermark for a retry
    # Success (a response body, or a legacy True). Persist the fresh signed
    # verdict so the compiled gate can enforce the cap offline.
    if isinstance(result, dict):
        with suppress(Exception):
            from lemoncrow.core.capabilities.plugin_runtime import persist_cap_verdict_token

            persist_cap_verdict_token(root, result.get("capVerdictToken"))
    _write_watermark(root, {"reported_saved_usd": saved_usd, "reported_spend_usd": spend_usd, "at": stamp})
    return True


def maybe_report_usage(root: str | Path, *, http_post: _HttpPost | None = None, now: int | None = None) -> bool:
    """Throttled entry point for the daemon/Stop hook: report at most every
    :data:`REPORT_INTERVAL_SECONDS`, and only when there is new data.
    """
    stamp = int(time.time()) if now is None else now
    wm = _read_watermark(root)
    last = float(wm.get("at") or 0.0)
    if stamp - last < REPORT_INTERVAL_SECONDS:
        return False
    return report_usage_once(root, http_post=http_post, now=stamp, watermark=wm)
