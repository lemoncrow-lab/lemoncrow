"""Service for ingesting session files into the Atelier store."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from atelier.core.foundation.paths import default_store_root
from atelier.gateway.integrations.ledger_reconstructor import LedgerReconstructor
from atelier.infra.runtime.run_ledger import RunLedger
from atelier.infra.storage.factory import make_memory_store


def ingest_session_file(file_path: str, store: Any = None) -> dict[str, Any]:
    """Ingest a session file and store its contents.

    Args:
        file_path: Path to the session file (expected to be JSONL format).
        store: Optional store instance. If not provided, the default store is used.

    Returns:
        A dictionary with the result of the ingestion.
    """
    if store is None:
        store_root = default_store_root().resolve()
        store = make_memory_store(store_root)
    else:
        store_root = Path(getattr(store, "root", default_store_root())).resolve()

    path = Path(file_path)
    if not path.exists():
        return {"status": "error", "message": f"File not found: {file_path}"}

    try:
        raw_content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"status": "error", "message": f"Failed to read file: {exc}"}

    # Try to infer the session ID from the file content or name.
    # We'll use a hash of the content as a fallback session ID.
    content_hash = hashlib.sha256(raw_content.encode("utf-8")).hexdigest()[:16]
    session_id = f"session_{content_hash}"

    # Attempt to extract a session ID from the first line if it looks like a session ID.
    first_line = raw_content.splitlines()[0] if raw_content else ""
    if (
        first_line
        and 16 <= len(first_line) <= 64
        and all(c in "0123456789abcdef" for c in first_line.lower())
    ):
        # Looks like a hex ID, use it if it's reasonable length.
        session_id = first_line

    reconstructor = LedgerReconstructor(root=store_root)
    try:
        ledger: RunLedger = reconstructor.reconstruct(
            source="unknown",
            session_id=session_id,
            raw_content=raw_content,
        )
    except Exception as exc:  # pylint: disable=broad-except
        return {
            "status": "error",
            "message": f"Failed to reconstruct ledger from session file: {exc}",
        }

    # TODO: Store reconstructed ledger events as traces.
    return {
        "status": "success",
        "session_id": session_id,
        "event_count": len(ledger.events),
        "ledger_summary": {
            "event_count": len(ledger.events),
            "created_at": ledger.created_at.isoformat(),
        },
    }