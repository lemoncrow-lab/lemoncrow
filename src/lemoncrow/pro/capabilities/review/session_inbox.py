"""Durable exact-session inbox for review feedback.

Some coding hosts can accept a message directly into an already-running session;
others cannot. The inbox is the safe fallback for the latter: Review can hand
feedback to the exact author session without spawning a copy, and the host's
normal hook consumes it on that session's next model turn.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.paths import safe_segment


@dataclass(frozen=True, slots=True)
class SessionInboxMessage:
    id: str
    host: str
    session_id: str
    message: str
    created_at: float


def _inbox_dir(root: Path | str, host: str, session_id: str) -> Path:
    return (
        Path(root)
        / "review"
        / "session-inbox"
        / safe_segment(host.strip().lower(), field="host")
        / safe_segment(session_id.strip(), field="session_id")
    )


def enqueue_session_message(
    root: Path | str,
    *,
    host: str,
    session_id: str,
    message: str,
) -> SessionInboxMessage:
    """Persist one exact-session message idempotently by semantic payload."""

    host = host.strip().lower()
    session_id = session_id.strip()
    if not host or not session_id or not message.strip():
        raise ValueError("host, session_id and message are required")
    digest = hashlib.sha256(f"{host}\0{session_id}\0{message}".encode()).hexdigest()[:32]
    record = SessionInboxMessage(
        id=f"msg-{digest}",
        host=host,
        session_id=session_id,
        message=message,
        created_at=time.time(),
    )
    directory = _inbox_dir(root, host, session_id)
    pending = directory / "pending"
    pending.mkdir(parents=True, exist_ok=True)
    target = pending / f"{record.id}.json"
    if target.exists():
        return record
    payload = {
        "id": record.id,
        "host": host,
        "session_id": session_id,
        "message": message,
        "created_at": record.created_at,
    }
    tmp = pending / f".{record.id}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
        try:
            os.replace(tmp, target)
        except OSError:
            tmp.unlink(missing_ok=True)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return record


def claim_session_messages(
    root: Path | str,
    *,
    host: str,
    session_id: str,
    limit: int = 8,
) -> tuple[SessionInboxMessage, ...]:
    """Atomically claim pending messages for one exact session, oldest first."""

    directory = _inbox_dir(root, host, session_id)
    pending = directory / "pending"
    claimed = directory / "claimed"
    try:
        paths = sorted(pending.glob("msg-*.json"), key=lambda path: (path.stat().st_mtime_ns, path.name))
    except OSError:
        return ()
    if not paths:
        return ()
    claimed.mkdir(parents=True, exist_ok=True)
    out: list[SessionInboxMessage] = []
    for source in paths[: max(1, limit)]:
        destination = claimed / source.name
        try:
            os.replace(source, destination)
            raw: Any = json.loads(destination.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            message = str(raw.get("message") or "")
            if not message:
                continue
            out.append(
                SessionInboxMessage(
                    id=str(raw.get("id") or source.stem),
                    host=str(raw.get("host") or host),
                    session_id=str(raw.get("session_id") or session_id),
                    message=message,
                    created_at=float(raw.get("created_at") or 0.0),
                )
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    return tuple(out)


__all__ = ["SessionInboxMessage", "claim_session_messages", "enqueue_session_message"]
