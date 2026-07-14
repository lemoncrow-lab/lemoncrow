"""Public contract types for memory arbitration.

``ArbitrationDecision`` is the caller-facing decision shape (data contract, not
IP). It lives here (open) because pydantic models cannot be mypyc-compiled, so
the pro arbitration logic compiles to native ``.so``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

ArbitrationOp = Literal["ADD", "UPDATE", "DELETE", "NOOP"]


class ArbitrationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: ArbitrationOp
    target_block_id: str | None = None
    merged_value: str | None = None
    reason: str = ""
