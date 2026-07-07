"""Store protocol — structural interface for all Atelier storage backends.

Any class that provides these methods satisfies StoreProtocol without
needing to inherit from it.  Both SQLiteStore and PostgresStore implement
this protocol.

Resources covered:
  playbooks, rubrics, traces            (core runtime)
  run_ledgers, monitor_events              (observability)
  eval_cases                               (improvement pipeline)
  audit_log, savings_events, jobs          (ops / billing)
  projects, environments, trace_events,
  playbook_applications, eval_runs            (extended schema)
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from atelier.core.foundation.models import Playbook, PlaybookStatus, Rubric, Trace


class StoreProtocol(Protocol):
    """Minimal read/write interface shared by all store backends.

    Only the methods used by runtime code are required here.  Additional
    resource-specific helpers (monitor_events, run_ledgers, etc.) are
    provided by the concrete implementations.
    """

    # ----- lifecycle ------------------------------------------------------- #

    def init(self) -> None:
        """Initialise the backing store (create tables / dirs)."""
        ...

    # ----- playbooks ---------------------------------------------------- #

    def upsert_block(self, block: Playbook, *, write_markdown: bool = True) -> None:
        """Insert or update a Playbook."""
        ...

    def get_block(self, block_id: str) -> Playbook | None:
        """Return a block by id, or None."""
        ...

    def list_blocks(
        self,
        *,
        domain: str | None = None,
        status: PlaybookStatus | None = "active",
        include_deprecated: bool = False,
    ) -> list[Playbook]:
        """Return blocks, optionally filtered by domain / status."""
        ...

    def search_blocks(self, query: str, *, limit: int = 20) -> list[Playbook]:
        """Full-text search over blocks."""
        ...

    def update_block_status(self, block_id: str, status: PlaybookStatus) -> bool:
        """Update status field; return True if a row was updated."""
        ...

    def delete_block(self, block_id: str) -> bool:
        """Hard-delete a Playbook; return True if a row was removed."""
        ...

    def increment_usage(
        self,
        block_id: str,
        *,
        success: bool | None = None,
    ) -> None:
        """Bump usage / success / failure counters."""
        ...

    # ----- traces ---------------------------------------------------------- #

    def record_trace(self, trace: Trace, *, write_json: bool = True) -> None:
        """Persist a trace."""
        ...

    def get_trace(self, trace_id: str) -> Trace | None:
        """Return a trace by id, or None."""
        ...

    def list_traces(
        self,
        *,
        domain: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[Trace]:
        """Return traces, optionally filtered."""
        ...

    # ----- rubrics --------------------------------------------------------- #

    def upsert_rubric(self, rubric: Rubric, *, write_yaml: bool = True) -> None:
        """Insert or update a Rubric."""
        ...

    def get_rubric(self, rubric_id: str) -> Rubric | None:
        """Return a rubric by id, or None."""
        ...

    def list_rubrics(self, *, domain: str | None = None) -> list[Rubric]:
        """Return rubrics, optionally filtered by domain."""
        ...

    def import_blocks(self, blocks: Iterable[Playbook]) -> int:
        """Bulk-upsert blocks; return count inserted/updated."""
        ...

    def import_rubrics(self, rubrics: Iterable[Rubric]) -> int:
        """Bulk-upsert rubrics; return count inserted/updated."""
        ...

    # ----- bulk import ----------------------------------------------------- #

    # ----- generic low-level (optional helper) ----------------------------- #

    def health_check(self) -> dict[str, Any]:
        """Return a dict with at least {"ok": bool, "backend": str}."""
        ...


__all__ = ["StoreProtocol"]
