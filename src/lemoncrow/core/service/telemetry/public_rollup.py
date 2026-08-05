"""Public aggregate savings rollup publisher.

Sends only the anonymous aggregate fields used by the public landing-page
counters (saved_usd, tokens_saved, calls_avoided, turn_count).  Install IDs
and session IDs are SHA-256 hashed before leaving the process.  Never raises
into hooks.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import urllib.error
import urllib.request
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.identity import get_anon_id
from lemoncrow.core.foundation.telemetry_cadence import TELEMETRY_PUSH_INTERVAL_SECONDS

logger = logging.getLogger("lemoncrow.product.telemetry.public_rollup")


def _hash_hex(value: str) -> str:
    """One-way hash so only an opaque key ever leaves the machine.

    Must match the formula the server previously applied to raw ids
    (functions/api/telemetry/rollup.ts), so already-stored session/install
    keys stay stable across the cutover.
    """
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


DEFAULT_PUBLIC_ROLLUP_ENDPOINT = "https://lemoncrow.com/api/telemetry/rollup"
# Session-hook default: this runs inline on a Stop hook, so it must never add
# perceptible latency -- one attempt, sub-second.
DEFAULT_TIMEOUT_SECONDS = 0.75
# Background daemon default: nothing is waiting on it, and a dropped post costs
# a whole day of reporting, so give it a realistic budget and real retries.
# (Measured round-trip to the production endpoint is ~0.2-0.6s, i.e. right at
# the hook timeout -- a single slow TLS handshake used to lose the day.)
FLUSH_TIMEOUT_SECONDS = 5.0
FLUSH_ATTEMPTS = 3
_RETRY_SLEEP_SECONDS = (0.5, 1.5)

# Durable flush state (checkpoint + retry schedule) lives next to the store so
# ANY process can run the daily flush, not just the servicectl daemon, and so a
# failure reschedules in minutes instead of silently waiting another full day.
_STATE_RELPATH = ("telemetry", "public_rollup_state.json")
_LOCK_RELPATH = ("telemetry", "public_rollup.lock")
# Kept in sync with usage_report.REPORT_INTERVAL_SECONDS via the shared
# constant -- see telemetry_cadence's module docstring for why.
FLUSH_INTERVAL_SECONDS = TELEMETRY_PUSH_INTERVAL_SECONDS
_RETRY_BASE_SECONDS = 900  # 15 min after the first failure ...
_RETRY_MAX_SECONDS = 21_600  # ... doubling up to 6 h
_LOCK_STALE_SECONDS = 900
_FAILURE_REASONS = frozenset({"post_failed", "error"})


def publish_public_savings_rollup(
    *,
    session_id: str,
    saved_usd: float,
    tokens_saved: int,
    calls_avoided: int,
    turn_count: int,
    source: str,
    # mypyc note: keyword-only params below all carry defaults. Keep every
    # non-default (required) kwonly param above this line -- mypyc's
    # synthetic __bitmap tracking arg (added once a function has enough
    # optional args) mis-scans a required kwonly param that comes AFTER an
    # optional one as "positional-only", which then trips CPython's
    # inspect.Signature ordering check ("non-default argument follows
    # default argument") while generating the compiled function's docstring.
    turns_avoided: int = 0,
    occurred_at: datetime | None = None,
    carry_usd: float = 0.0,
    carry_tokens: int = 0,
    est_cost_usd: float = 0.0,
    time_saved_seconds: float = 0.0,
    domain: str = "code",
    output_saved_tokens: int = 0,
    output_saved_usd: float = 0.0,
    tokens_processed: int = 0,
    calls_made: int = 0,
    time_spent_seconds: float = 0.0,
    timeout_s: float | None = None,
    attempts: int = 1,
) -> bool:
    """Publish one sanitized savings rollup (a single session, or one daily aggregate).

    Returns whether the request was accepted. All failures are swallowed so
    telemetry cannot break a user session or hook.
    """

    try:
        from lemoncrow.core.service.telemetry.config import remote_enabled

        # Opt-out gate: the public savings rollup is remote telemetry, on by
        # default but suppressed the moment the user disables it
        # (`lc telemetry remote off`, DO_NOT_TRACK=1, LEMONCROW_TELEMETRY=off).
        # Covers both the session-end hook and the daily daemon flush.
        if not remote_enabled():
            return False

        endpoint = public_rollup_endpoint()
        if not endpoint:
            return False

        payload = _payload(
            session_id=session_id,
            saved_usd=saved_usd,
            tokens_saved=tokens_saved,
            calls_avoided=calls_avoided,
            turn_count=turn_count,
            turns_avoided=turns_avoided,
            source=source,
            occurred_at=occurred_at,
            carry_usd=carry_usd,
            carry_tokens=carry_tokens,
            est_cost_usd=est_cost_usd,
            time_saved_seconds=time_saved_seconds,
            domain=domain,
            output_saved_tokens=output_saved_tokens,
            output_saved_usd=output_saved_usd,
            tokens_processed=tokens_processed,
            calls_made=calls_made,
            time_spent_seconds=time_spent_seconds,
        )
        if payload is None:
            return False
        return _post_json(
            endpoint,
            payload,
            timeout_s=public_rollup_timeout_seconds() if timeout_s is None else max(0.1, float(timeout_s)),
            attempts=max(1, int(attempts)),
        )
    except Exception as exc:
        logger.debug("public_rollup.publish_failed", extra={"error": str(exc)})
        return False


def public_rollup_endpoint() -> str:
    """Return the rollup endpoint URL.

    Override via LEMONCROW_PUBLIC_TELEMETRY_ENDPOINT (useful for self-hosting
    or local dev).  Falls back to the production endpoint.
    """
    raw = os.environ.get("LEMONCROW_PUBLIC_TELEMETRY_ENDPOINT", DEFAULT_PUBLIC_ROLLUP_ENDPOINT).strip()
    return raw if raw else DEFAULT_PUBLIC_ROLLUP_ENDPOINT


def public_rollup_timeout_seconds() -> float:
    raw = os.environ.get("LEMONCROW_PUBLIC_TELEMETRY_TIMEOUT_MS", "")
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
    # mypyc note: see the matching comment in publish_public_savings_rollup --
    # every non-default kwonly param must stay above this line.
    turns_avoided: int = 0,
    carry_usd: float = 0.0,
    carry_tokens: int = 0,
    est_cost_usd: float = 0.0,
    time_saved_seconds: float = 0.0,
    domain: str = "code",
    output_saved_tokens: int = 0,
    output_saved_usd: float = 0.0,
    tokens_processed: int = 0,
    calls_made: int = 0,
    time_spent_seconds: float = 0.0,
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
    turns_av = max(0, int(turns_avoided or 0))
    carry_s = max(0.0, float(carry_usd or 0.0))
    carry_t = max(0, int(carry_tokens or 0))
    cost = max(0.0, float(est_cost_usd or 0.0))
    time_s = float(time_saved_seconds or 0.0)
    out_tok = max(0, int(output_saved_tokens or 0))
    out_usd = max(0.0, float(output_saved_usd or 0.0))
    # Real totals (not savings) -- the baseline "tokens saved" etc. are a
    # fraction of. Absent (0) for hosts/paths that haven't computed them yet
    # (see aggregate_usage_totals_since_day) -- the server treats 0 as "not
    # reported", not "processed nothing".
    tok_proc = max(0, int(tokens_processed or 0))
    calls_made_n = max(0, int(calls_made or 0))
    time_spent = max(0.0, float(time_spent_seconds or 0.0))
    # Skip only a wholly empty rollup; a negative-but-nonzero one is real signal.
    if not (
        saved
        or tokens
        or calls
        or turns
        or turns_av
        or carry_s
        or carry_t
        or cost
        or time_s
        or out_tok
        or out_usd
        or tok_proc
        or calls_made_n
        or time_spent
    ):
        return None
    at = occurred_at or datetime.now(UTC)
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    return {
        "install_key": install_key,
        "session_key": session_key,
        "lemoncrow_version": _service_version(),
        "source": _label(source, fallback="lemoncrow", max_length=40),
        # Which retrieval vertical produced the savings (code today; docs,
        # tickets, chat memory as the engine generalizes). Forward-compatible
        # aggregation key for per-domain rollups.
        "domain": _label(domain, fallback="code", max_length=40),
        "saved_usd": round(saved, 6),
        "tokens_saved": tokens,
        "calls_avoided": calls,
        "carry_usd": round(carry_s, 6),
        "carry_tokens": carry_t,
        "turn_count": turns,
        "turns_avoided": turns_av,
        "est_cost_usd": round(cost, 6),
        "time_saved_seconds": round(time_s, 3),
        "output_saved_tokens": out_tok,
        "output_saved_usd": round(out_usd, 6),
        "tokens_processed": tok_proc,
        "calls_made": calls_made_n,
        "time_spent_seconds": round(time_spent, 3),
        "occurred_at": at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }


def _post_json(endpoint: str, payload: dict[str, Any], *, timeout_s: float, attempts: int = 1) -> bool:
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    # The User-Agent is load-bearing, not cosmetic: the production endpoint
    # sits behind a bot filter that 403s the default `Python-urllib/x.y`.
    request = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"lemoncrow/{payload.get('lemoncrow_version', 'unknown')}",
        },
        method="POST",
    )
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout_s) as response:
                return 200 <= int(response.status) < 300
        except urllib.error.HTTPError as exc:
            # 4xx is a verdict on the payload -- retrying sends the identical
            # bytes and gets the identical answer. Only retry 429/5xx.
            code = int(getattr(exc, "code", 0) or 0)
            logger.debug("public_rollup.post_failed", extra={"error": str(exc), "status": code})
            if code < 500 and code != 429:
                return False
        except (OSError, urllib.error.URLError) as exc:
            logger.debug("public_rollup.post_failed", extra={"error": str(exc)})
        if attempt + 1 < attempts:
            time.sleep(_RETRY_SLEEP_SECONDS[min(attempt, len(_RETRY_SLEEP_SECONDS) - 1)])
    return False


def _service_version() -> str:
    try:
        return version("lemoncrow")
    except PackageNotFoundError:
        return "0.1.0"


def _label(value: str, *, fallback: str, max_length: int) -> str:
    cleaned = "".join(ch for ch in str(value or fallback) if ch.isalnum() or ch in "_.:+/-")[:max_length]
    return cleaned or fallback


def flush_daily_public_rollup(root: str | Path, *, checkpoint_day: str | None) -> tuple[dict[str, Any], str | None]:
    """Publish one rollup per UTC day fully elapsed since ``checkpoint_day``,
    computed directly from the canonical per-session savings ledger
    (:func:`lemoncrow.core.capabilities.savings_summary.aggregate_savings_by_day`)
    -- no separate queue file to maintain.

    Days are posted oldest-first, one at a time, and the checkpoint advances
    after each one that succeeds. Posting one lump sum for the whole unflushed
    range (the original design) meant any backlog -- a missed flush, a
    sleeping laptop -- could push a single post's saved_usd/carry_usd past the
    ingest endpoint's per-session cap (MAX_SESSION_USD in
    functions/api/telemetry/rollup.ts) and get rejected outright; since a
    rejected post left the checkpoint untouched, the next flush resent the
    same (now even larger) backlog and failed again -- a silent, permanent
    wedge with no way to recover on its own. Per-day posting keeps each post
    within a single day's totals (what the cap was actually sized for) and
    lets days that DO fit through even when a later one is still stuck.

    Returns ``(result, new_checkpoint_day)``; callers persist the returned
    checkpoint so each calendar day is reported at most once, however often
    (or rarely) this is actually called.

    ``checkpoint_day is None`` means "never flushed before". Rather than
    resending a user's entire local history (which would double report every
    session the old always-on Stop-hook push already sent before this daily
    batching existed), the first call only establishes today as the baseline
    and reports nothing.
    """
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    if checkpoint_day is None:
        return {"flushed": False, "reason": "baseline"}, today

    from lemoncrow.core.capabilities.savings_summary import (
        aggregate_savings_by_day,
        aggregate_usage_totals_by_day,
        estimate_time_saved_seconds,
    )

    by_day = aggregate_savings_by_day(root, since_day=checkpoint_day, today=today)
    if not by_day:
        return {"flushed": False, "reason": "no_new_days"}, checkpoint_day

    # Independent pass (real per-session transcript parse, not the $-savings
    # ledger) -- see aggregate_usage_totals_by_day's docstring for why this
    # is deliberately not folded into the per-day totals above. Best-effort: a
    # transcript-read failure here must not block the $ figures below from
    # reporting, so a raise here degrades to "not reported" (0), not a
    # skipped flush. Bucketed per day (by each session's first-activity day)
    # so every day carries its own real totals instead of the whole window's
    # sum riding on the newest day -- that lump also blew past the ingest
    # endpoint's per-post real-total caps and was silently stored as 0.
    usage_by_day: dict[str, dict[str, float | int]] = {}
    with suppress(Exception):
        usage_by_day = aggregate_usage_totals_by_day(root, since_day=checkpoint_day, today=today)

    days_in_order = sorted(by_day)
    checkpoint = checkpoint_day
    flushed_through: str | None = None
    for day in days_in_order:
        totals = by_day[day]
        usage = usage_by_day.get(day) or {}
        ok = publish_public_savings_rollup(
            session_id=f"daily-rollup-{day}",
            saved_usd=float(totals["saved_usd"]),
            tokens_saved=int(totals["tokens_saved"]),
            calls_avoided=int(totals["calls_avoided"]),
            turn_count=int(totals["turn_count"]),
            turns_avoided=int(totals["turns_avoided"]),
            source="claude",
            # Stamp the day being reported, not "now": a backlog (sleeping
            # laptop, stuck checkpoint) would otherwise pile every backfilled
            # day onto the flush date, and the public per-day breakdown
            # buckets on occurred_at.
            occurred_at=_day_midpoint(day),
            carry_usd=float(totals["carry_usd"]),
            carry_tokens=int(totals.get("carry_tokens", 0) or 0),
            est_cost_usd=float(totals["est_cost_usd"]),
            time_saved_seconds=estimate_time_saved_seconds(
                calls_avoided=int(totals["calls_avoided"]),
                output_saved_tokens=int(totals.get("output_saved_tokens", 0) or 0),
            ),
            output_saved_tokens=int(totals.get("output_saved_tokens", 0) or 0),
            output_saved_usd=float(totals.get("output_saved_usd", 0.0) or 0.0),
            tokens_processed=int(usage.get("tokens_processed", 0) or 0),
            calls_made=int(usage.get("calls_made", 0) or 0),
            time_spent_seconds=float(usage.get("time_spent_seconds", 0.0) or 0.0),
            timeout_s=FLUSH_TIMEOUT_SECONDS,
            attempts=FLUSH_ATTEMPTS,
        )
        if not ok:
            # This day (and everything after it) is retried on the next
            # flush; days already posted above stay committed instead of
            # being resent or permanently blocked by a later stuck day.
            return {
                "flushed": flushed_through is not None,
                "through_day": flushed_through,
                "stuck_day": day,
                "reason": "post_failed",
            }, checkpoint
        checkpoint = day
        flushed_through = day
    return {"flushed": True, "through_day": flushed_through}, checkpoint


def _day_midpoint(day: str) -> datetime:
    """Midday UTC of an ISO ``YYYY-MM-DD`` day (noon, so no rounding or clock
    skew on either side can push the stamp into an adjacent calendar day)."""
    return datetime.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Durable flush scheduling (checkpoint + retry backoff)
# ---------------------------------------------------------------------------


def _state_path(root: Path) -> Path:
    return root.joinpath(*_STATE_RELPATH)


def _lock_path(root: Path) -> Path:
    return root.joinpath(*_LOCK_RELPATH)


def read_public_rollup_state(root: str | Path) -> dict[str, Any]:
    """Persisted flush state; ``{}`` when absent or unreadable (fail-open:
    a corrupt file must degrade to "flush now", never to "never flush")."""
    path = _state_path(Path(root))
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_state(root: Path, payload: dict[str, Any]) -> None:
    path = _state_path(root)
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _acquire_lock(root: Path, now: datetime) -> bool:
    """Best-effort cross-process lock so two hosts/daemons cannot post the same
    day twice. A lock older than ``_LOCK_STALE_SECONDS`` (crashed holder) is
    reclaimed rather than wedging the flush forever."""
    path = _lock_path(root)
    with suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                age = now.timestamp() - path.stat().st_mtime
            except OSError:
                return False
            if age < _LOCK_STALE_SECONDS:
                return False
            with suppress(OSError):
                path.unlink()
            continue
        except OSError:
            return False
        with suppress(OSError):
            os.write(fd, f"{os.getpid()} {now.isoformat()}".encode())
        with suppress(OSError):
            os.close(fd)
        return True
    return False


def _release_lock(root: Path) -> None:
    with suppress(OSError):
        _lock_path(root).unlink()


def _next_delay_seconds(failures: int) -> int:
    if failures <= 0:
        return FLUSH_INTERVAL_SECONDS
    return int(min(_RETRY_MAX_SECONDS, _RETRY_BASE_SECONDS * (2 ** (failures - 1))))


def maybe_flush_public_rollup(
    root: str | Path,
    *,
    force: bool = False,
    legacy_checkpoint_day: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run the daily flush if it is due, and reschedule it durably.

    Owns everything the caller used to own (when to run, what the checkpoint
    is, what to do after a failure) so a flush is safe to trigger from any
    process -- the servicectl tick, a CLI command, a hook -- without two of
    them double-posting a day.

    Scheduling: success -> next attempt in 1 h (``FLUSH_INTERVAL_SECONDS``, so a
    day that finishes elapsing mid-cycle surfaces on the public page within
    the hour instead of sitting a day behind). Failure -> 15 min, doubling to
    a 6 h ceiling, because the old "mark it done and try again tomorrow"
    behaviour turned one dropped POST (a flaky network, a laptop suspending
    mid-request) into a whole lost day, and a machine that is only awake in
    bursts could miss the window indefinitely. ``next_attempt_at`` is wall-clock
    and persisted, so a sleeping laptop simply flushes on wake.
    """
    root_path = Path(root)
    now = now or datetime.now(UTC)
    state = read_public_rollup_state(root_path)
    checkpoint_day = state.get("checkpoint_day")
    if not isinstance(checkpoint_day, str) or not checkpoint_day:
        # One-time migration from the servicectl-owned state key, so the
        # cutover does not resend (or re-baseline) an existing install.
        checkpoint_day = legacy_checkpoint_day if isinstance(legacy_checkpoint_day, str) else None

    next_attempt_at = _parse_iso(state.get("next_attempt_at"))
    if not force and next_attempt_at is not None and now < next_attempt_at:
        return {
            "flushed": False,
            "reason": "not_due",
            "checkpoint_day": checkpoint_day,
            "next_attempt_at": next_attempt_at.isoformat(),
        }

    if not _acquire_lock(root_path, now):
        return {"flushed": False, "reason": "locked", "checkpoint_day": checkpoint_day}

    try:
        result, checkpoint_day = flush_daily_public_rollup(root_path, checkpoint_day=checkpoint_day)
    except Exception as exc:  # never raise into a hook or the daemon tick
        logger.debug("public_rollup.flush_failed", extra={"error": str(exc)})
        result = {"flushed": False, "reason": "error", "error": str(exc)}
    finally:
        _release_lock(root_path)

    failures = int(state.get("consecutive_failures") or 0) + 1 if result.get("reason") in _FAILURE_REASONS else 0
    delay = _next_delay_seconds(failures)
    _write_state(
        root_path,
        {
            "checkpoint_day": checkpoint_day,
            "consecutive_failures": failures,
            "last_attempt_at": now.isoformat(),
            "next_attempt_at": (now + timedelta(seconds=delay)).isoformat(),
            "last_result": result,
        },
    )
    return {
        **result,
        "checkpoint_day": checkpoint_day,
        "consecutive_failures": failures,
        "next_attempt_at": (now + timedelta(seconds=delay)).isoformat(),
    }
