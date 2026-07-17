"""Identity-bound, retry-safe client usage reporting.

The client sends monotonic cumulative totals. The server owns the per-device
watermark and derives the delta transactionally, so local watermark deletion,
concurrent reporters, and lost-response retries cannot reset or double-count
usage. Every identity — anonymous AND authenticated — refreshes its signed
cap verdict every two hours, well before its eight-hour expiry, even when no
new usage was recorded: an idle-but-under-cap account must never go dormant
just because no new savings landed.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.request
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from lemoncrow.core.capabilities.licensing import store
from lemoncrow.core.capabilities.licensing.entitlements import USER_AGENT

REPORT_INTERVAL_SECONDS = 30 * 60
VERDICT_REFRESH_SECONDS = 2 * 60 * 60
_LIFETIME_DAYS = 36_500

_HttpPost = Callable[[str, dict[str, Any], str], "dict[str, Any] | None"]


def _persist_registered_at_iso(root: str | Path, iso_ts: str) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import persist_registered_at

    persist_registered_at(root, iso_ts)


def _persist_cycle_resets_at_iso(root: str | Path, iso_ts: str) -> None:
    from lemoncrow.core.capabilities.plugin_runtime import persist_cycle_resets_at

    persist_cycle_resets_at(root, iso_ts)


def _persist_unix_display(root: str | Path, unix_value: object, writer: Callable[[str | Path, str], None]) -> None:
    """Best-effort: convert a unix-seconds response field to ISO and persist it.

    Shared by the two display-only anon-report anchors (``deviceRegisteredAt``,
    ``cycleResetsAt``) -- never raises, since a persistence failure here must
    never fail the underlying usage report.
    """
    if not isinstance(unix_value, int | float):
        return
    with suppress(Exception):
        from datetime import UTC, datetime

        iso_ts = datetime.fromtimestamp(unix_value, UTC).replace(microsecond=0, tzinfo=None).isoformat() + "Z"
        writer(root, iso_ts)


def _machine_hash() -> str:
    """Hash the stable OS-backed device id; the raw id never leaves the client."""
    try:
        return hashlib.sha256(store.load_or_create_device_id().encode("utf-8")).hexdigest()
    except Exception:  # noqa: BLE001 — without a stable identity the server must reject the report
        return ""


def _reporting_identity(token: str | None, machine_hash: str) -> str:
    subject = f"auth:{hashlib.sha256(token.encode()).hexdigest()}" if token else "anonymous"
    return hashlib.sha256(f"usage:v2:{subject}:{machine_hash}".encode()).hexdigest()


def _watermark_path(root: str | Path, identity: str) -> Path:
    return Path(root) / "usage_report_watermarks" / f"{identity}.json"


def _read_watermark(root: str | Path, identity: str) -> dict[str, Any]:
    path = _watermark_path(root, identity)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        with suppress(OSError):
            tmp.unlink()


def _write_watermark(root: str | Path, identity: str, data: dict[str, Any]) -> None:
    with suppress(OSError, ValueError):
        _atomic_write(
            _watermark_path(root, identity),
            json.dumps(data, separators=(",", ":"), sort_keys=True),
        )


def _anon_token_path(root: str | Path) -> Path:
    return Path(root) / "cap_anon_token"


def _read_anon_token(root: str | Path) -> str | None:
    path = _anon_token_path(root)
    try:
        return (path.read_text("utf-8").strip() or None) if path.exists() else None
    except OSError:
        return None


def _write_anon_token(root: str | Path, token: str) -> None:
    with suppress(OSError, ValueError):
        _atomic_write(_anon_token_path(root), token)


def _default_post(url: str, payload: dict[str, Any], token: str) -> dict[str, Any] | None:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            if not (200 <= response.status < 300):
                return None
            parsed = json.loads(response.read())
            return parsed if isinstance(parsed, dict) else None
    except Exception:  # noqa: BLE001 — leave the watermark unmoved for retry
        return None


def _report_id(
    identity: str,
    machine_hash: str,
    saved_usd: float,
    spend_usd: float,
) -> str:
    canonical = json.dumps(
        ["usage-v2", identity, machine_hash, saved_usd, spend_usd],
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def report_usage_once(
    root: str | Path,
    *,
    http_post: _HttpPost | None = None,
    now: int | None = None,
    watermark: dict[str, Any] | None = None,
    force: bool = False,
) -> bool:
    """Post a cumulative device total and persist a fresh signed verdict.

    ``force`` skips the nothing-changed short-circuit: identity transitions
    (login/logout/init) and the MCP server's dormant self-heal need a fresh
    mint even when the cumulative totals are unchanged, because the signed
    verdict is bound to (account, device, plan) and the previous identity's
    token no longer verifies.
    """
    token = store.load_auth_token()
    is_anon = not token
    machine_hash = _machine_hash()
    if len(machine_hash) != 64:
        return False
    identity = _reporting_identity(token, machine_hash)
    anon_token = _read_anon_token(root) if is_anon else None
    stamp = int(time.time()) if now is None else now
    wm = _read_watermark(root, identity) if watermark is None else watermark

    try:
        from lemoncrow.core.capabilities.savings_summary import (
            aggregate_window_savings,
            synthetic_backfill_saved_usd,
        )

        lifetime = aggregate_window_savings(Path(root), days=_LIFETIME_DAYS)
        saved_usd = round(max(0.0, float(getattr(lifetime, "saved_usd", 0.0))), 4)
        spend_usd = round(max(0.0, float(getattr(lifetime, "spend_usd", 0.0))), 4)
        # Synthetic reconcile/backfill rows correct only the LOCAL display; never
        # report them as this device's measured savings. The server derives a
        # per-device delta from the cumulative we send, so re-reporting reconcile
        # rows (derived FROM the server figure) makes it double-count them as
        # fresh usage -- a runaway account-inflation loop -- and backfilled
        # estimates would consume the plan's real cap for savings never delivered.
        saved_usd = round(max(0.0, saved_usd - synthetic_backfill_saved_usd(root)), 4)
    except Exception:  # noqa: BLE001
        return False

    prior_saved = float(wm.get("reported_saved_usd") or 0.0)
    prior_spend = float(wm.get("reported_spend_usd") or 0.0)
    need_bootstrap = is_anon and not anon_token
    last_verdict = float(wm.get("verdict_at") or 0.0)
    # ALL identities re-mint on this cadence, not just anonymous ones: an
    # authenticated identity's first report fires through it too (its
    # watermark starts empty), and an idle authenticated device keeps a live
    # verdict instead of going dormant when the old token expires.
    need_refresh = stamp - last_verdict >= VERDICT_REFRESH_SECONDS
    has_new_usage = saved_usd > prior_saved or spend_usd > prior_spend
    counter_regressed = saved_usd < prior_saved or spend_usd < prior_spend
    if not force and not has_new_usage and not counter_regressed and not need_bootstrap and not need_refresh:
        return False

    payload: dict[str, Any] = {
        "report_id": _report_id(identity, machine_hash, saved_usd, spend_usd),
        "cumulative_saved_usd": saved_usd,
        "cumulative_spend_usd": spend_usd,
        "reported_at": stamp,
    }
    post = http_post or _default_post
    base = store.load_auth_base()
    if is_anon:
        payload["anon_token"] = anon_token or ""
        payload["machine_id"] = machine_hash
        result = post(f"{base}/api/usage/report-anon", payload, "")
    else:
        result = post(f"{base}/api/usage/report", payload, token or "")

    if not isinstance(result, dict):
        return False
    cap_token = result.get("capVerdictToken")
    if not isinstance(cap_token, str) or not cap_token:
        return False

    with suppress(Exception):
        from lemoncrow.core.capabilities.plugin_runtime import persist_cap_verdict_token

        persist_cap_verdict_token(root, cap_token)
    if is_anon:
        minted = result.get("anonToken")
        if isinstance(minted, str) and minted:
            _write_anon_token(root, minted)
        # Display-only anchors for `lc account cap` -- the server's
        # authoritative first-usage day for this anon identity, and (when the
        # anon cap is a verified fixed calendar cycle, not a rolling estimate)
        # the instant it hard-resets to 0. See firstSeenDay/cycleSavings in
        # landing/functions/api/usage.ts.
        _persist_unix_display(root, result.get("deviceRegisteredAt"), _persist_registered_at_iso)
        _persist_unix_display(root, result.get("cycleResetsAt"), _persist_cycle_resets_at_iso)

    _write_watermark(
        root,
        identity,
        {
            "reported_saved_usd": saved_usd,
            "reported_spend_usd": spend_usd,
            "verdict_at": stamp,
            "at": stamp,
        },
    )
    return True


def maybe_report_usage(
    root: str | Path,
    *,
    http_post: _HttpPost | None = None,
    now: int | None = None,
) -> bool:
    """Throttle reporting per authenticated identity and device."""
    stamp = int(time.time()) if now is None else now
    token = store.load_auth_token()
    machine_hash = _machine_hash()
    if len(machine_hash) != 64:
        return False
    identity = _reporting_identity(token, machine_hash)
    wm = _read_watermark(root, identity)
    last = float(wm.get("at") or 0.0)
    if stamp - last < REPORT_INTERVAL_SECONDS:
        return False
    return report_usage_once(root, http_post=http_post, now=stamp, watermark=wm)
