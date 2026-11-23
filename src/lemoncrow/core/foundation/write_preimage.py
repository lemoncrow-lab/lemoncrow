"""One-use preimages for exact native Write attribution.

The Claude Write hook has no old_string; git diff HEAD repeats earlier edits.
Capture the preimage in PreToolUse, then consume it exactly once after a
successful Write. Never credit a guessed diff when a preimage is missing.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

_MAX_BYTES = 2 * 1024 * 1024


def _snapshot_path(root: str | Path, session_id: str, tool_use_id: str) -> Path:
    key = hashlib.sha256((session_id + "\0" + tool_use_id).encode("utf-8")).hexdigest()
    return Path(root) / "transient" / "claude_write_preimages" / f"{key}.json"


def stage_write_preimage(root: str | Path, session_id: str, tool_use_id: str, target: str | Path) -> bool:
    """Capture bytes just before a native Write; fail closed on oversized files."""
    if not session_id or not tool_use_id:
        return False
    path = Path(target).expanduser().resolve()
    try:
        existed = path.is_file()
        if path.exists() and not existed:
            return False
        if existed and path.stat().st_size > _MAX_BYTES:
            return False
        before = path.read_bytes() if existed else b""
        if len(before) > _MAX_BYTES:
            return False
        storage = _snapshot_path(root, session_id, tool_use_id)
        storage.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        storage.parent.chmod(0o700)
        # Failed/denied writes can leave an unconsumed snapshot. Keep only
        # recent one-use snapshots; old source preimages must not linger.
        now = time.time()
        for stale in storage.parent.glob("*.json"):
            with contextlib.suppress(OSError):
                if now - stale.stat().st_mtime > 86_400:
                    stale.unlink()
        payload = {
            "path": str(path),
            "existed": existed,
            "before": base64.b64encode(before).decode("ascii"),
        }
        fd, temp_path = tempfile.mkstemp(dir=storage.parent, prefix=".write-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            Path(temp_path).replace(storage)
        finally:
            Path(temp_path).unlink(missing_ok=True)
        return True
    except (OSError, ValueError, TypeError):
        return False


def discard_write_preimage(root: str | Path, session_id: str, tool_use_id: str) -> None:
    """Delete the staged preimage after a failed or denied native Write."""
    if session_id and tool_use_id:
        with contextlib.suppress(OSError):
            _snapshot_path(root, session_id, tool_use_id).unlink(missing_ok=True)


def consume_write_preimage(
    root: str | Path, session_id: str, tool_use_id: str, target: str | Path
) -> tuple[bytes, bytes] | None:
    """Return exact (old, new) content only for the pre-captured target.

    The snapshot is deleted even when the write failed, another file was
    substituted, the target is now oversized, or its encoding is unsuitable.
    """
    if not session_id or not tool_use_id:
        return None
    storage = _snapshot_path(root, session_id, tool_use_id)
    try:
        payload = json.loads(storage.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    finally:
        with contextlib.suppress(OSError):
            storage.unlink(missing_ok=True)
    try:
        path = Path(target).expanduser().resolve()
        if payload.get("path") != str(path) or not path.is_file() or path.stat().st_size > _MAX_BYTES:
            return None
        before = base64.b64decode(payload["before"], validate=True)
        after = path.read_bytes()
        if len(before) > _MAX_BYTES or len(after) > _MAX_BYTES:
            return None
        # Lossy decoding produces false diffs; skip binaries safely.
        before.decode("utf-8")
        after.decode("utf-8")
        return before, after
    except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError, binascii.Error):
        return None
