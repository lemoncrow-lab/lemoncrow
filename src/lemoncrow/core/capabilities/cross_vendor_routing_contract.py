"""Public contract types for cross-vendor routing.

Exception types are part of the caller-facing API, not engine IP. They live in
this open module (mypyc cannot compile classes that inherit builtin exception
types) so the pro routing logic can compile to native ``.so`` while callers keep
importing the same names. Mirrors the ``code_context_contract`` pattern.
"""

from __future__ import annotations


class RoutePolicyError(ValueError):
    """Raised when routing policy cannot be applied safely."""


class NoFeasibleRouteError(ValueError):
    """Raised when no configured vendor can satisfy the requested turn safely."""
