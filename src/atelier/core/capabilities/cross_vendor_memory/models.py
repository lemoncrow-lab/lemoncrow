"""Domain models for cross-vendor memory audit events.

See docs/specs/day30/08-memory-audit-viewer.md for the full spec.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditEvent(BaseModel):
    """A single audit-log entry recording a change to a memory fact."""

    model_config = ConfigDict(extra="forbid")

    vendor: str
    """Which AI vendor owns this fact (claude, codex, gemini, …)."""

    event: str
    """Event type: 'added', 'removed', 'changed', or 'rollback'."""

    fact_id: str
    """Stable fact identifier (from base._fact_id)."""

    source_file: str
    """Path to the source memory file (relative or absolute)."""

    source_line: int = 0
    """Line number inside source_file where the fact lives."""

    content: str = ""
    """Fact content at the time of this event."""

    previous_content: str | None = None
    """For 'changed' events: the old content."""

    actor: str = "atelier"
    """Process or agent that triggered this event."""

    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    """Timestamp of the event (UTC)."""

    # ------------------------------------------------------------------ #
    # Public record                                                        #
    # ------------------------------------------------------------------ #

    def to_public_record(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict suitable for export/redaction."""
        return {
            "vendor": self.vendor,
            "event": self.event,
            "fact_id": self.fact_id,
            "source_file": self.source_file,
            "source_line": self.source_line,
            "content": self.content,
            "previous_content": self.previous_content,
            "actor": self.actor,
            "at": self.at.astimezone(UTC).isoformat(),
        }


__all__ = ["AuditEvent"]
