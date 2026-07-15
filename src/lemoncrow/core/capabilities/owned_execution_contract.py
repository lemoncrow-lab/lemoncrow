"""Public exception contract for owned execution.

Held open because mypyc cannot compile builtin-exception subclasses. The Pro
execution module re-exports this type so existing imports remain stable.
"""

from __future__ import annotations

from typing import Any


class OwnedExecutionError(RuntimeError):
    """Owned execution failed after exhausting its permitted routes."""

    def __init__(self, message: str, *, receipt: Any) -> None:
        super().__init__(message)
        self.receipt = receipt


__all__ = ["OwnedExecutionError"]
