"""Per-call savings rows for Workspaces Lite (standard library only).

The full MCP server wrote one row per savings-bearing tool call to
``sessions/YYYY/MM/DD/<host>/<session>/savings.jsonl``
(``lemoncrow.gateway.adapters.mcp_server._append_savings``). The stop hook,
statusline and savings reports read those rows, and their carry credit (a saved
token is also not re-read on later turns) is built from them. Lite went without
them from 2026-10-03, so none of its savings were counted. This module keeps
that contract.

Lite has no price table: a row carries the measured ``tokens``/``calls``, the
model, and (with calls) the live context size and average turn output read
from the host transcript; readers price it
(``savings_summary._price_savings_row``). A row is written only when the host
session is known; nothing is guessed.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

_SAFE_SEGMENT: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
# Same order and hosts as lemoncrow_client.session._HOST_SESSION_ENVS.
_HOST_SESSION_ENVS: Final[tuple[tuple[str, str], ...]] = (
    ("CLAUDE_CODE_SESSION_ID", "claude"),
    ("CODEX_SESSION_ID", "codex"),
    ("OPENCODE_SESSION_ID", "opencode"),
    ("GITHUB_COPILOT_SESSION_ID", "copilot"),
    ("CURSOR_SESSION_ID", "cursor"),
    ("CURSOR_TRACE_ID", "cursor"),
    ("HERMES_SESSION_ID", "hermes"),
    ("ANTIGRAVITY_SESSION_ID", "antigravity"),
    ("AGY_SESSION_ID", "antigravity"),
)
_SEARCH_DAYS: Final = 3
_TAIL_BYTES: Final = 64 * 1024
# The host shows at most this much of a tool result inline (the full server's
# HOST_INLINE_RESULT_CHARS), and a targeted read opens about twice the section.
_HOST_INLINE_RESULT_CHARS: Final = 50 * 1024
_REALISTIC_READ_FACTOR: Final = 2


class SavingsLedger:
    """Append per-call savings rows to the host session's ledger."""

    def __init__(self, root: Path, environment: Mapping[str, str], repo_root: Path) -> None:
        self._root = root
        self._env = environment
        self._repo_root = repo_root
        self._lock = threading.Lock()
        self._ledgers: dict[tuple[str, str], Path] = {}
        self._transcripts: dict[str, Path] = {}
        self._context: dict[str, tuple[tuple[int, int], tuple[int, str, int]]] = {}
        self._credited: set[str] = set()

    def identity(self) -> tuple[str, str]:
        """``(session_id, host)`` of the coding-host session, or empty strings."""
        explicit = self._env.get("LEMONCROW_AGENT", "").strip().lower()
        found = [(sid, host) for env, host in _HOST_SESSION_ENVS if (sid := self._env.get(env, "").strip())]
        pick = next((f for f in found if f[1] == explicit), None) or next((f for f in found if f[1] == "claude"), None)
        sid, host = pick or (found[0] if found else ("", ""))
        if not _SAFE_SEGMENT.fullmatch(sid) or not _SAFE_SEGMENT.fullmatch(host) or sid in {".", ".."}:
            return "", ""
        return sid, host

    def record(self, tool: str, *, tokens: int = 0, calls: int = 0) -> None:
        """Append one row; never raises (a ledger write must not fail a tool call)."""
        tokens, calls = max(0, int(tokens)), max(0, int(calls))
        if tokens <= 0 and calls <= 0:
            return
        # A latency-profile run drives synthetic calls; they avoided no work.
        if self._env.get("LEMONCROW_TOOL_PROFILE_PATH"):
            return
        try:
            sid, host = self.identity()
            if not sid:
                return
            ctx_tokens, model, avg_output = self._context_state(sid, host)
            entry: dict[str, object] = {
                "tool": tool,
                "tokens": tokens,
                "calls": calls,
                "model": model,
                # Naive UTC, as the full server wrote it; readers assume UTC.
                "ts": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                "source": "lite",
            }
            if calls and ctx_tokens:
                # An avoided call re-reads this context and emits a turn's output.
                entry["ctx_tokens"] = ctx_tokens
                if avg_output:
                    entry["avg_output_tokens"] = avg_output
            line = json.dumps(entry) + "\n"
            with self._lock:
                path = self._ledger(host, sid) / "savings.jsonl"
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(line)
        except Exception:  # a ledger write must never fail a tool call
            return

    def code_search_savings(self, payload: Mapping[str, Any], shown: str, root: Path) -> tuple[int, int]:
        """``(tokens, calls)`` one code_search answer saved (the full server's credit).

        The search itself replaces one grep, 1-for-1. It also avoids one read per
        file whose source came back whole, and one more search when the symbol
        map points past those files. Tokens: the neighborhood a targeted read
        would have opened (twice the source, at most the file or the host's
        inline cap) minus the source. A file credits once per session: a repeat
        search re-reads nothing. Source cut from the rendered answer earns nothing.
        """
        files = payload.get("files")
        if not isinstance(files, list):
            return 0, 0
        tokens = reads = 0
        sourced: set[str] = set()
        for item in files:
            if not isinstance(item, Mapping):
                continue
            path, source = item.get("path"), item.get("source")
            if not isinstance(path, str) or not path or not isinstance(source, str) or not source:
                continue
            if source not in shown:
                continue
            sourced.add(path)
            target = Path(path) if Path(path).is_absolute() else root / path
            try:
                size = min(target.stat().st_size, _HOST_INLINE_RESULT_CHARS)
            except OSError:
                continue
            with self._lock:
                if str(target) in self._credited:
                    continue
                self._credited.add(str(target))
            if "[truncated" not in source:
                reads += 1
            tokens += max(0, min(size, _REALISTIC_READ_FACTOR * len(source)) - len(source)) // 4
        nav = 0
        related = payload.get("related_symbols")
        for symbol in related if isinstance(related, list) else ():
            other = symbol.get("path") if isinstance(symbol, Mapping) else None
            if isinstance(other, str) and other and other not in sourced:
                nav = 1
                break
        return tokens, reads + nav

    def _ledger(self, host: str, sid: str) -> Path:
        """``lemoncrow.core.foundation.paths.session_dir`` without the package.

        A session keeps the folder it already has (newest partition for this
        host); only a session with none mints today's date.
        """
        key = (host, sid)
        cached = self._ledgers.get(key)
        if cached is not None:
            return cached
        sessions = self._root / "sessions"
        today = date.today()
        found: Path | None = None
        for offset in range(_SEARCH_DAYS):
            d = today - timedelta(days=offset)
            candidate = sessions / f"{d:%Y}" / f"{d:%m}" / f"{d:%d}" / host / sid
            if candidate.exists():
                found = candidate
                break
        if found is None:
            existing = sorted(sessions.glob(f"*/*/*/{host}/{sid}"))
            found = existing[-1] if existing else sessions / f"{today:%Y}" / f"{today:%m}" / f"{today:%d}" / host / sid
        self._ledgers[key] = found
        return found

    def _context_state(self, sid: str, host: str) -> tuple[int, str, int]:
        """Measured ``(context tokens, model, average output tokens)``.

        Claude: the last usage entry of the session transcript (per-turn model
        truth, as the full server read it) and the mean output of the turns in
        its tail. Other hosts, or no transcript: the workspace bridge's model
        for this exact session, context and output unknown (0).
        """
        if host == "claude":
            path = self._transcript(sid)
            if path is not None:
                try:
                    st = path.stat()
                    sig = (st.st_mtime_ns, st.st_size)
                    cached = self._context.get(sid)
                    if cached is not None and cached[0] == sig:
                        return cached[1]
                    state = _last_usage(path)
                    if state[1]:
                        self._context[sid] = (sig, state)
                        return state
                except OSError:
                    pass
        return 0, self._bridge_model(sid), 0

    def _transcript(self, sid: str) -> Path | None:
        cached = self._transcripts.get(sid)
        if cached is not None:
            return cached
        base = Path(self._env.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude").expanduser() / "projects"
        found = next(iter(sorted(base.glob(f"*/{sid}.jsonl"))), None)
        if found is not None:
            self._transcripts[sid] = found
        return found

    def _bridge_model(self, sid: str) -> str:
        path = self._repo_root / ".lemoncrow" / "workspace" / "session_state.json"
        try:
            if path.stat().st_size > 256 * 1024:
                return ""
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            return ""
        if not isinstance(raw, dict) or str(raw.get("session_id") or "").strip() != sid:
            return ""
        return str(raw.get("model") or "").strip()


def _last_usage(path: Path) -> tuple[int, str, int]:
    """``(context, model, mean output)`` from the transcript's tail.

    One API response is written as several entries (one per content block)
    carrying the same usage, so output is averaged per message id.
    """
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        start = max(0, fh.tell() - _TAIL_BYTES)
        fh.seek(start)
        lines = fh.read().split(b"\n")
    if start:
        lines = lines[1:]
    ctx, model = 0, ""
    outputs: dict[str, int] = {}
    for line in reversed(lines):
        if b'"usage"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        msg = entry.get("message") if isinstance(entry, dict) else None
        usage = msg.get("usage") if isinstance(msg, dict) else None
        name = str(msg.get("model") or "") if isinstance(msg, dict) else ""
        if not isinstance(msg, dict) or not isinstance(usage, dict) or not name or name.startswith("<"):
            continue
        if not model:
            model = name
            ctx = sum(
                int(usage.get(key) or 0)
                for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
            )
        outputs.setdefault(str(msg.get("id") or len(outputs)), int(usage.get("output_tokens") or 0))
    average = sum(outputs.values()) // len(outputs) if outputs else 0
    return ctx, model, average
