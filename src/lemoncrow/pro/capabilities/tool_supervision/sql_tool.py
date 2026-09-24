"""The ``sql`` tool's main-package hook: oversized cells go to the spill store.

The engine is :mod:`lemoncrow_client.kit.sql`, shared with the thin client.
"""

from __future__ import annotations

from pathlib import Path

from lemoncrow_client.kit.sql import SqlHooks

from lemoncrow.core.environment import tool_output_spill_enabled


def _spill_cell(full_text: str) -> Path | None:
    """Persist one oversized cell in the shared T7 spill store.

    Returns the file's path, or ``None`` when spilling is switched off or the
    write fails; the kit then reports a plain truncation.
    """
    from lemoncrow.pro.capabilities.tool_supervision import tool_output_spill

    if not tool_output_spill_enabled():
        return None
    record = tool_output_spill.spill(full_text, tool_name="sql", kind="original")
    return record.path if record is not None else None


SQL_HOOKS = SqlHooks(cell_spill=_spill_cell)

__all__ = ["SQL_HOOKS"]
