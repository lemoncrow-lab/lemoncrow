"""Resolved optional third-party runtime dependencies.

These packages are declared as required dependencies, but the imports stay
defensive so a partial install degrades gracefully instead of crashing at import
time. The try/except rebinding pattern (name imported in ``try``, replaced by a
stub/``None`` in ``except``) is incompatible with mypyc, so it lives in this open,
uncompiled module -- the compiled ``pro`` capabilities import the already-resolved
names from here. Kept out of mypyc compilation via the ``no-redef`` marker (see
``hatch_build._NO_REDEF_RE``); it carries no IP.
"""

from __future__ import annotations

import logging
from typing import Any

try:
    from blake3 import blake3
except Exception:  # pragma: no cover - optional dependency fallback
    logging.exception("Recovered from broad exception handler")
    blake3: Any = None  # type: ignore[no-redef]

try:
    from git import Repo
except Exception:  # pragma: no cover - optional dependency fallback
    logging.exception("Recovered from broad exception handler")
    Repo: Any = None  # type: ignore[no-redef]

try:
    from tenacity import retry, stop_after_attempt, wait_exponential
except Exception:  # pragma: no cover - optional dependency fallback
    logging.exception("Recovered from broad exception handler")

    def retry(*_args: Any, **_kwargs: Any) -> Any:  # type: ignore[no-redef]
        def _decorate(fn: Any) -> Any:
            return fn

        return _decorate

    def stop_after_attempt(_attempts: int) -> Any:  # type: ignore[no-redef]
        return None

    def wait_exponential(**_kwargs: Any) -> None:  # type: ignore[no-redef]
        return None


try:
    from prometheus_client import Counter, Histogram
except Exception:  # pragma: no cover - optional dependency fallback
    logging.exception("Recovered from broad exception handler")
    Counter: Any = None  # type: ignore[no-redef]
    Histogram: Any = None  # type: ignore[no-redef]

try:
    import pybreaker
except Exception:  # pragma: no cover - optional dependency fallback
    logging.exception("Recovered from broad exception handler")
    pybreaker: Any = None  # type: ignore[no-redef]

__all__ = [
    "Counter",
    "Histogram",
    "Repo",
    "blake3",
    "pybreaker",
    "retry",
    "stop_after_attempt",
    "wait_exponential",
]
