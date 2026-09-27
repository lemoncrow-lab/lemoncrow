"""Client half of the LemonCrow runtime trace.

The thin client is the first layer to see a tool call, so it mints the call's
``lt_<16 hex>`` trace id, sends it as ``X-LemonCrow-Request-Id`` for
server-routed tools (the server and in-process runtime reuse it), and writes
its own record for every call -- including tools it runs locally (bash, edit,
grep, ...), which never leave this machine.

Record format and storage layout are the same contract as
``lemoncrow.core.foundation.runtime_trace`` (schema v1, one JSON line per
record under ``<state_dir>/traces/<YYYY-MM-DD>/client-<pid>.jsonl``).  This
package has no dependencies, so the handful of rules are restated here and a
contract test keeps both sides equal.

Privacy: ``LEMONCROW_RUNTIME_TRACE`` = ``raw`` (default for local installs)
keeps arguments except bulk content, ``redacted`` (default for hosted
installs) hashes them, ``off`` disables writing.  Bulk content (edit bodies,
file text, diffs) is never stored.
Retention: 14 days / 256 MB by default, pruned at most hourly on write.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Iterator, Mapping
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

__all__ = [
    "BULK_CONTENT_KEYS",
    "TRACE_SCHEMA_VERSION",
    "ClientTraceWriter",
    "current_trace_id",
    "new_trace_id",
    "sanitize_arguments",
    "trace_scope",
]

TRACE_SCHEMA_VERSION: Final[int] = 1
BULK_CONTENT_KEYS: Final[frozenset[str]] = frozenset(
    {"content", "new", "old", "new_string", "old_string", "text", "source", "diff", "patch", "body", "input"}
)
_MAX_ARG_CHARS: Final[int] = 2048
_MAX_ITEMS: Final[int] = 64
_MAX_DEPTH: Final[int] = 5
_PRUNE_INTERVAL_S: Final[float] = 3600.0

_CURRENT: ContextVar[str | None] = ContextVar("lemoncrow_client_trace_id", default=None)


def new_trace_id() -> str:
    return f"lt_{secrets.token_hex(8)}"


def current_trace_id() -> str | None:
    return _CURRENT.get()


@contextlib.contextmanager
def trace_scope(trace_id: str) -> Iterator[str]:
    token = _CURRENT.set(trace_id)
    try:
        yield trace_id
    finally:
        _CURRENT.reset(token)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:20]


def _bulk(value: Any) -> dict[str, Any]:
    text = value if isinstance(value, str) else json.dumps(value, default=str, sort_keys=True)
    return {"omitted": "bulk_content", "chars": len(text), "sha256": _digest(text)}


def _raw(value: Any, *, key: str = "", depth: int = 0) -> Any:
    if key.lower() in BULK_CONTENT_KEYS and value is not None:
        return _bulk(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return value if len(value) <= _MAX_ARG_CHARS else value[:_MAX_ARG_CHARS] + f"…[+{len(value) - _MAX_ARG_CHARS}]"
    if depth >= _MAX_DEPTH:
        return {"truncated": "max_depth"}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (item_key, item_value) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                out["…"] = f"+{len(value) - _MAX_ITEMS} keys"
                break
            name = str(item_key)[:64]
            out[name] = _raw(item_value, key=name, depth=depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        bounded = [_raw(item, depth=depth + 1) for item in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            bounded.append(f"…+{len(items) - _MAX_ITEMS}")
        return bounded
    return str(value)[:_MAX_ARG_CHARS]


def _redacted(value: Any, *, key: str = "", depth: int = 0) -> Any:
    """Default-deny: every string leaf is hashed (see the runtime's twin)."""
    if key.lower() in BULK_CONTENT_KEYS and value is not None:
        return _bulk(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return {"redacted_sha256": _digest(value)}
    if depth >= _MAX_DEPTH:
        return {"truncated": "max_depth"}
    if isinstance(value, Mapping):
        out: dict[str, Any] = {}
        for index, (item_key, item_value) in enumerate(value.items()):
            if index >= _MAX_ITEMS:
                out["…"] = f"+{len(value) - _MAX_ITEMS} keys"
                break
            name = str(item_key)[:64]
            out[name] = _redacted(item_value, key=name, depth=depth + 1)
        return out
    if isinstance(value, list | tuple | set | frozenset):
        items = list(value)
        bounded = [_redacted(item, depth=depth + 1) for item in items[:_MAX_ITEMS]]
        if len(items) > _MAX_ITEMS:
            bounded.append(f"…+{len(items) - _MAX_ITEMS}")
        return bounded
    return {"redacted_sha256": _digest(str(value))}


def trace_mode(env: Mapping[str, str] | None = None, *, default: str = "raw") -> str:
    raw = (env if env is not None else os.environ).get("LEMONCROW_RUNTIME_TRACE", "").strip().lower()
    if raw in {"off", "0", "false", "disabled", "none"}:
        return "off"
    if raw in {"redacted", "redact", "hashed"}:
        return "redacted"
    if raw in {"raw", "on", "1", "true"}:
        return "raw"
    return default


def sanitize_arguments(arguments: Mapping[str, Any] | None, mode: str) -> dict[str, Any]:
    if not arguments or mode == "off":
        return {}
    out = _redacted(dict(arguments)) if mode == "redacted" else _raw(dict(arguments))
    return dict(out) if isinstance(out, Mapping) else {}


def _env_int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    raw = env.get(name, "").strip()
    if raw:
        with contextlib.suppress(ValueError):
            return max(low, min(high, int(raw)))
    return default


class ClientTraceWriter:
    """Append client trace records and apply retention. Never raises."""

    def __init__(self, state_dir: Path, *, env: Mapping[str, str] | None = None, hosted: bool = False) -> None:
        source = env if env is not None else os.environ
        # Hosted (enterprise) installs hash arguments unless the operator asks
        # for raw; a solo loopback install keeps them for replay.
        self.mode = trace_mode(source, default="redacted" if hosted else "raw")
        self.dir = Path(state_dir) / "traces"
        self.retention_days = _env_int(source, "LEMONCROW_TRACE_RETENTION_DAYS", 14, 1, 365)
        self.max_bytes = _env_int(source, "LEMONCROW_TRACE_MAX_MB", 256, 1, 65536) * 1024 * 1024
        self._lock = threading.Lock()
        self._last_prune = 0.0

    @property
    def enabled(self) -> bool:
        return self.mode != "off"

    def record(
        self,
        *,
        trace_id: str,
        tool: str,
        arguments: Mapping[str, Any] | None,
        started_at: datetime,
        duration_ms: int,
        site: str,
        is_error: bool,
        degraded: bool,
        degraded_reason: str,
        output_chars: int,
        session_id: str | None,
        view_revision: int | None,
        state_token: str = "",
        recoveries: list[str] | None = None,
    ) -> None:
        if not self.enabled:
            return
        redact = self.mode == "redacted"
        record = {
            "schema_version": TRACE_SCHEMA_VERSION,
            "trace_id": trace_id,
            "writer": "client",
            "mode": self.mode,
            "tool": tool,
            "started_at": started_at.isoformat(),
            "session_id": ({"redacted_sha256": _digest(session_id)} if redact and session_id else session_id),
            "workspace": {"id": None, "revision": view_revision},
            "request": {"arguments": sanitize_arguments(arguments, self.mode)},
            "decisions": [],
            "execution": {
                "duration_ms": max(0, int(duration_ms)),
                "site": site,
                "state_token": state_token or None,
                "recoveries": list(recoveries or [])[:16],
            },
            "outcome": {
                "is_error": bool(is_error),
                "degraded": bool(degraded),
                "degraded_reason": degraded_reason or None,
                "output_chars": max(0, int(output_chars)),
                "est_output_tokens": max(0, int(output_chars)) // 4,
            },
        }
        self._append(record)

    def _append(self, record: Mapping[str, Any]) -> None:
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        target = self.dir / day / f"client-{os.getpid()}.jsonl"
        line = json.dumps(record, ensure_ascii=False, default=str, separators=(",", ":")) + "\n"
        try:
            with self._lock:
                # Owner-only: raw traces hold queries and paths.
                self.dir.mkdir(mode=0o700, parents=True, exist_ok=True)
                target.parent.mkdir(mode=0o700, exist_ok=True)
                fd = os.open(target, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
                with os.fdopen(fd, "a", encoding="utf-8") as handle:
                    handle.write(line)
        except OSError:
            return
        now = time.monotonic()
        if not self._last_prune or now - self._last_prune >= _PRUNE_INTERVAL_S:
            self._last_prune = now
            with contextlib.suppress(OSError):
                self.prune()

    def prune(self, *, today: datetime | None = None) -> list[str]:
        if not self.dir.is_dir():
            return []
        days = sorted(
            (p for p in self.dir.iterdir() if p.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.name)),
            reverse=True,
        )
        cutoff = ((today or datetime.now(UTC)) - timedelta(days=self.retention_days)).strftime("%Y-%m-%d")
        removed: list[str] = []
        for day_dir in days:
            if day_dir.name < cutoff:
                shutil.rmtree(day_dir, ignore_errors=True)
                removed.append(day_dir.name)
        kept = [d for d in days if d.name not in removed]
        sizes = {d: sum(f.stat().st_size for f in d.glob("*.jsonl") if f.is_file()) for d in kept}
        total = sum(sizes.values())
        for day_dir in reversed(kept[1:]):
            if total <= self.max_bytes:
                break
            shutil.rmtree(day_dir, ignore_errors=True)
            total -= sizes[day_dir]
            removed.append(day_dir.name)
        return removed
