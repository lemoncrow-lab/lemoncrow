"""Local-only maintenance: fresh imported host usage and the landing watch.

The retired local controller imported host sessions periodically. The unified
loopback server retained index maintenance but lost that duty, leaving /usage
backed by a stale history database. This wrapper restores the periodic import
without reviving the old daemon stack or scanning hosts from request handlers.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

_LOG = logging.getLogger("lemoncrow.server.local_maintenance")
_DEFAULT_IMPORT_INTERVAL_S = 30 * 60.0
_FAILED_IMPORT_RETRY_S = 5 * 60.0
_IMPORT_TIMEOUT_S = 300.0


class LocalMaintenance:
    """Compose engine maintenance with periodic local host-session import."""

    def __init__(
        self,
        base: Any,
        *,
        runtime_root: Path,
        state_root: Path,
        import_interval_s: float = _DEFAULT_IMPORT_INTERVAL_S,
        clock: Callable[[], float] = time.time,
        landing_watch: Callable[[], object] | None = None,
        flush_telemetry: bool = True,
    ) -> None:
        self._base = base
        self._landing_watch = landing_watch
        self._flush_telemetry_enabled = flush_telemetry
        self._runtime_root = Path(runtime_root)
        self._state_path = Path(state_root) / "session-import.json"
        self._import_interval_s = max(0.0, float(import_interval_s))
        self._clock = clock

    @property
    def last_report(self) -> Any:
        return getattr(self._base, "last_report", None)

    def run_once(self) -> Any:
        try:
            return self._base.run_once()
        finally:
            # A failed index pass must not also stop landings, session import or telemetry.
            self._watch_landings()
            self._maybe_import_sessions()
            self._flush_telemetry()

    def _flush_telemetry(self) -> None:
        """Daily anonymous rollups (savings and event counts). Each is a cheap
        no-op until due, lock-guarded across processes, and opt-out aware; this
        long-lived server is the one reliable caller now that thin-client
        sessions no longer run the stdio MCP worker tick."""
        if not self._flush_telemetry_enabled:
            return
        from lemoncrow.core.service.telemetry.event_rollup import maybe_flush_event_rollup
        from lemoncrow.core.service.telemetry.public_rollup import maybe_flush_public_rollup

        for flush in (maybe_flush_public_rollup, maybe_flush_event_rollup):
            try:
                flush(self._runtime_root)
            except Exception:
                _LOG.exception("telemetry rollup flush failed")

    def _watch_landings(self) -> None:
        if self._landing_watch is None:
            return
        try:
            self._landing_watch()
        except Exception:
            _LOG.exception("landing watch failed")

    def _read_state(self) -> dict[str, Any]:
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _write_state(self, payload: dict[str, Any]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_name(f".{self._state_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
                tmp.replace(self._state_path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        except OSError:
            _LOG.exception("failed to persist local session-import state")

    def _maybe_import_sessions(self) -> None:
        if self._import_interval_s <= 0:
            return
        now = float(self._clock())
        state = self._read_state()
        try:
            last_attempt = float(state.get("last_attempt_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            last_attempt = 0.0
        retry_after = (
            self._import_interval_s
            if state.get("last_attempt_succeeded")
            else min(self._import_interval_s, _FAILED_IMPORT_RETRY_S)
        )
        if last_attempt > 0 and now - last_attempt < retry_after:
            return
        # Root `lc import` is retired; the internal worker prints the same JSON counts.
        command = [sys.executable, "-m", "lemoncrow.core.service.session_import", "--root", str(self._runtime_root)]
        succeeded = False
        counts: dict[str, int] = {}
        error = ""
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=_IMPORT_TIMEOUT_S, check=False)
            if result.returncode == 0:
                try:
                    raw = json.loads(result.stdout)
                except (ValueError, TypeError):
                    error = "session import returned invalid JSON"
                else:
                    if isinstance(raw, dict):
                        counts = {str(key): int(value) for key, value in raw.items() if isinstance(value, (int, float))}
                        succeeded = True
                    else:
                        error = "session import returned non-object JSON"
            else:
                error = (result.stderr or result.stdout or f"exit {result.returncode}").strip()[-500:]
        except subprocess.TimeoutExpired:
            error = f"session import timed out after {int(_IMPORT_TIMEOUT_S)}s"
        except OSError as exc:
            error = f"session import could not start: {exc}"
        if succeeded:
            _LOG.info("local session import refreshed usage: %s", counts)
        else:
            _LOG.warning("local session import failed: %s", error or "unknown error")
        self._write_state(
            {
                "last_attempt_at": now,
                "last_attempt_succeeded": succeeded,
                "last_success_at": now if succeeded else state.get("last_success_at"),
                "imported_sessions": counts,
                "error": "" if succeeded else error,
            }
        )


__all__ = ["LocalMaintenance"]
