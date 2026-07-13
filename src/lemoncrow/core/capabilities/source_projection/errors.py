"""Projection error types (kept here, uncompiled, so the algorithm modules compile).

mypyc cannot compile a module that subclasses a builtin (``ValueError``). Holding
these exception classes in this tiny open module lets ``minify.py`` and
``edit.py`` — the projection algorithms — be compiled to ``.so`` and shipped
without readable source, while still raising/catching these types normally
(importing a class is fine for mypyc; only *subclassing* a builtin is not).
"""

from __future__ import annotations

from typing import Any


class ProjectionEditError(ValueError):
    """Raised when a projection-aware edit cannot be safely applied."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "projection_edit_error",
        hint: str = "",
        retry_with: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
        self.retry_with = retry_with

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"error": str(self), "kind": "projection", "code": self.code}
        if self.hint:
            payload["hint"] = self.hint
        if self.retry_with is not None:
            payload["retry_with"] = self.retry_with
        return payload


class MinifiedEditError(ValueError):
    """Raised when an edit against the minified view cannot be applied safely."""

    def __init__(self, message: str, *, code: str, hint: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint
