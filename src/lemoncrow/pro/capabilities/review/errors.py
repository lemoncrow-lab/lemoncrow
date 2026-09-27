"""Review-specific exception types kept interpreted for mypyc compatibility."""

from __future__ import annotations


class AnnotationVersionConflict(RuntimeError):
    """A semantic comment changed after the caller last read its version."""


__all__ = ["AnnotationVersionConflict"]
