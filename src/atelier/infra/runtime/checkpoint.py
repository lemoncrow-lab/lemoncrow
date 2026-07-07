"""Idempotent agent checkpoints for resumable execution.

A Checkpoint captures the full execution state at a step boundary so that
network failures, context resets, or budget exhaustion don't restart the
full agent loop from turn 0.

Layout on disk::

    ~/.atelier/checkpoints/<session_id>/<step_id>.json

Resume flow::

    atelier checkpoint list                          # see available checkpoints
    atelier checkpoint resume <session_id> --from-step 7   # replay from step 7
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()[:16]


@dataclass
class Checkpoint:
    """Execution state snapshot at a single step boundary.

    Attributes:
        session_id: The agent run this checkpoint belongs to.
        step_id: Sequential step number within the session (0-based).
        transaction_id: Stable content-addressed ID (hash of session+step+input).
        tool_name: The tool call this step executed.
        model_route: The RouteTier / model selected for this step.
        input_hash: SHA-256[:16] of the tool input arguments.
        output_hash: SHA-256[:16] of the tool output.
        compact_state: Compact JSON state from context_compression at this point.
        cost_so_far_usd: Cumulative cost up to and including this step.
        created_at: ISO timestamp when this checkpoint was recorded.
    """

    session_id: str
    step_id: int
    transaction_id: str
    tool_name: str
    model_route: str
    input_hash: str
    output_hash: str
    compact_state: str = ""
    cost_so_far_usd: float = 0.0
    created_at: str = field(default_factory=lambda: _utcnow().isoformat())

    @classmethod
    def create(
        cls,
        session_id: str,
        step_id: int,
        tool_name: str,
        model_route: str,
        input_data: str,
        output_data: str,
        compact_state: str = "",
        cost_so_far_usd: float = 0.0,
    ) -> Checkpoint:
        input_hash = _hash(input_data)
        output_hash = _hash(output_data)
        transaction_id = _hash(f"{session_id}:{step_id}:{input_hash}")
        return cls(
            session_id=session_id,
            step_id=step_id,
            transaction_id=transaction_id,
            tool_name=tool_name,
            model_route=model_route,
            input_hash=input_hash,
            output_hash=output_hash,
            compact_state=compact_state,
            cost_so_far_usd=cost_so_far_usd,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


class CheckpointStore:
    """Persist and load Checkpoint records under ~/.atelier/checkpoints/.

    Each session gets its own subdirectory; each step gets one JSON file.
    """

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            import os

            from atelier.core.foundation.paths import default_store_root

            root = Path(os.environ.get("ATELIER_ROOT", str(default_store_root())))
        self._root = Path(root) / "checkpoints"

    def _session_dir(self, session_id: str) -> Path:
        return self._root / session_id

    def save(self, checkpoint: Checkpoint) -> Path:
        """Persist a checkpoint to disk. Returns the written path."""
        d = self._session_dir(checkpoint.session_id)
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{checkpoint.step_id:06d}.json"
        path.write_text(json.dumps(checkpoint.to_dict(), indent=2), encoding="utf-8")
        return path

    def load(self, session_id: str, step_id: int) -> Checkpoint | None:
        """Load a specific checkpoint by session and step. Returns None if missing."""
        path = self._session_dir(session_id) / f"{step_id:06d}.json"
        if not path.exists():
            return None
        return Checkpoint.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def list_sessions(self) -> list[str]:
        """Return all session IDs that have at least one checkpoint."""
        if not self._root.exists():
            return []
        return sorted(p.name for p in self._root.iterdir() if p.is_dir())

    def list_checkpoints(self, session_id: str) -> list[Checkpoint]:
        """Return all checkpoints for a session, sorted by step_id."""
        d = self._session_dir(session_id)
        if not d.exists():
            return []
        results = []
        for p in sorted(d.glob("*.json")):
            try:
                results.append(Checkpoint.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except Exception:
                logging.exception("Recovered from broad exception handler")
                continue
        return results

    def latest_checkpoint(self, session_id: str) -> Checkpoint | None:
        """Return the highest-step checkpoint for a session."""
        checkpoints = self.list_checkpoints(session_id)
        return checkpoints[-1] if checkpoints else None

    def delete_session(self, session_id: str) -> int:
        """Delete all checkpoints for a session. Returns count deleted."""
        d = self._session_dir(session_id)
        if not d.exists():
            return 0
        count = 0
        for p in d.glob("*.json"):
            p.unlink(missing_ok=True)
            count += 1
        with contextlib.suppress(OSError):
            d.rmdir()
        return count
