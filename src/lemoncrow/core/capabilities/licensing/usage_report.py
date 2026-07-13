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
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.licensing import store

REPORT_INTERVAL_SECONDS = 30 * 60
_BILLING_WINDOW_DAYS = 30
USER_AGENT = "LemonCrow-CLI/1.0"

_HttpPost = Callable[[str, dict[str, Any], str], bool]


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


def _default_post(url: str, payload: dict[str, Any], token: str) -> bool:
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
            return 200 <= resp.status < 300
    except Exception:  # noqa: BLE001 — network failure = retry next tick
        return False


def report_usage_once(root: str | Path, *, http_post: _HttpPost | None = None, now: int | None = None) -> bool:
    """Report the usage delta since the last watermark. Returns True if posted.

    No signed-in token, or no new data since last report -> returns False without
    posting. On a successful post, advances the watermark to the current totals.
    """
    token = store.load_auth_token()
    if not token:
        return False
    try:
        from lemoncrow.core.capabilities.savings_summary import aggregate_window_savings

        window = aggregate_window_savings(Path(root), days=_BILLING_WINDOW_DAYS)
        saved_usd = round(max(0.0, float(getattr(window, "saved_usd", 0.0))), 4)
        spend_usd = round(max(0.0, float(getattr(window, "spend_usd", 0.0))), 4)
    except Exception:  # noqa: BLE001
        return False

    wm = _read_watermark(root)
    prior_saved = float(wm.get("reported_saved_usd") or 0.0)
    prior_spend = float(wm.get("reported_spend_usd") or 0.0)
    delta_saved = round(saved_usd - prior_saved, 4)
    delta_spend = round(spend_usd - prior_spend, 4)
    if delta_saved <= 0.0 and delta_spend <= 0.0:
        return False  # no new data -> nothing to report

    stamp = int(time.time()) if now is None else now
    payload = {
        "window_saved_usd": saved_usd,
        "window_spend_usd": spend_usd,
        "delta_saved_usd": max(0.0, delta_saved),
        "delta_spend_usd": max(0.0, delta_spend),
        "reported_at": stamp,
    }
    post = http_post or _default_post
    if not post(f"{store.load_auth_base()}/api/usage/report", payload, token):
        return False
    _write_watermark(root, {"reported_saved_usd": saved_usd, "reported_spend_usd": spend_usd, "at": stamp})
    return True


def maybe_report_usage(root: str | Path, *, http_post: _HttpPost | None = None, now: int | None = None) -> bool:
    """Throttled entry point for the daemon/Stop hook: report at most every
    :data:`REPORT_INTERVAL_SECONDS`, and only when there is new data.
    """
    stamp = int(time.time()) if now is None else now
    last = float(_read_watermark(root).get("at") or 0.0)
    if stamp - last < REPORT_INTERVAL_SECONDS:
        return False
    return report_usage_once(root, http_post=http_post, now=stamp)
