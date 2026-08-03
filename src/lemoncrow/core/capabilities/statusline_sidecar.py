"""Structured statusline sidecar shared with rich frontends.

``statusline.sh`` renders a single pre-formatted ANSI line for Claude Code.
Frontends that draw their own panel (the LemonCode TUI sidebar) need the same
numbers as *data* instead, so they can theme and lay them out natively.

``lc code`` picks a snapshot path per run and exports it as
``LEMONCROW_STATUS_FILE`` to both the gateway process and the frontend. The
gateway rewrites the file after every turn; the frontend polls it.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STATUS_FILE_ENV = "LEMONCROW_STATUS_FILE"
SCHEMA_VERSION = 1


@dataclass
class IndexStatus:
    """Code-index state for the workspace the frontend is pointed at."""

    present: bool = False
    files: int = 0
    symbols: int = 0
    languages: int = 0
    indexed_at: float = 0.0
    zoekt: bool = False


@dataclass
class StatusSnapshot:
    """Cumulative view of one frontend session, as written for the sidebar."""

    provider: str = ""
    model: str = ""
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    saved_usd: float = 0.0
    cache_efficiency_pct: float = 0.0
    turns: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    mcp_calls: int = 0
    index: IndexStatus = field(default_factory=IndexStatus)
    updated_at: float = 0.0
    version: int = SCHEMA_VERSION

    @property
    def context_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    @property
    def tool_call_total(self) -> int:
        return sum(self.tool_calls.values())

    def add_turn(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        cache_read_tokens: int,
        cache_write_tokens: int,
        output_tokens: int,
        cost_usd: float,
        saved_usd: float,
        cache_efficiency_pct: float,
    ) -> None:
        if provider:
            self.provider = provider
        if model:
            self.model = model
        self.input_tokens += max(0, input_tokens)
        self.cache_read_tokens += max(0, cache_read_tokens)
        self.cache_write_tokens += max(0, cache_write_tokens)
        self.output_tokens += max(0, output_tokens)
        self.cost_usd += max(0.0, cost_usd)
        self.saved_usd += max(0.0, saved_usd)
        self.cache_efficiency_pct = cache_efficiency_pct
        self.turns += 1

    def record_tool_call(self, tool_name: str) -> None:
        if not tool_name:
            return
        self.tool_calls[tool_name] = self.tool_calls.get(tool_name, 0) + 1
        if tool_name.startswith("mcp__") or tool_name == "mcp_tool":
            self.mcp_calls += 1

    def to_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["context_tokens"] = self.context_tokens
        payload["tool_call_total"] = self.tool_call_total
        payload["updated_at"] = self.updated_at or time.time()
        return payload


_index_cache: tuple[float, float, IndexStatus] | None = None
_INDEX_TTL_SECONDS = 30.0


def read_index_status(workspace_root: Path | str) -> IndexStatus:
    """Code-index counts for *workspace_root*, cached per process.

    Re-read only when the index file's mtime moved and at most once per
    ``_INDEX_TTL_SECONDS`` -- this runs on the turn-finalization path, never on
    a render.
    """
    global _index_cache
    from lemoncrow.core.foundation.paths import resolve_workspace_store_dir

    try:
        db_path = resolve_workspace_store_dir(workspace_root=Path(workspace_root)) / "code_context.sqlite"
    except Exception:
        return IndexStatus()
    try:
        mtime = db_path.stat().st_mtime
    except OSError:
        return IndexStatus(zoekt=_zoekt_present())

    now = time.time()
    if _index_cache is not None:
        cached_mtime, cached_at, cached = _index_cache
        if cached_mtime == mtime and now - cached_at < _INDEX_TTL_SECONDS:
            return cached

    status = IndexStatus(present=True, indexed_at=mtime, zoekt=_zoekt_present())
    try:
        import sqlite3

        # Read-only URI: never create or upgrade the index from a status read.
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            status.files = int(conn.execute("SELECT COUNT(*) FROM files").fetchone()[0])
            status.symbols = int(conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0])
            status.languages = int(conn.execute("SELECT COUNT(DISTINCT language) FROM files").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return IndexStatus(present=True, indexed_at=mtime, zoekt=status.zoekt)

    _index_cache = (mtime, now, status)
    return status


def _zoekt_present() -> bool:
    index_dir = Path(os.environ.get("LEMONCROW_ZOEKT_INDEX_DIR", "") or (Path.home() / ".zoekt"))
    try:
        return index_dir.is_dir() and any(index_dir.iterdir())
    except OSError:
        return False


def status_file_path(root: Path | str, key: str) -> Path:
    """Snapshot path for one frontend run."""
    return Path(root).expanduser() / "statusline" / f"{key}.json"


def configured_status_path() -> Path | None:
    """Snapshot path exported by the launcher, when the frontend wants one."""
    raw = os.environ.get(STATUS_FILE_ENV, "").strip()
    return Path(raw) if raw else None


def write_status_snapshot(path: Path, snapshot: StatusSnapshot) -> None:
    """Atomically replace *path* with *snapshot*; never raise into a turn."""
    snapshot.updated_at = time.time()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(json.dumps(snapshot.to_payload(), separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return


def read_status_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


__all__ = [
    "SCHEMA_VERSION",
    "STATUS_FILE_ENV",
    "IndexStatus",
    "StatusSnapshot",
    "configured_status_path",
    "read_index_status",
    "read_status_snapshot",
    "status_file_path",
    "write_status_snapshot",
]
