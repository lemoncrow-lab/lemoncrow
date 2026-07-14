"""Weakref-able lifecycle holder for the mypyc-native code engine.

mypyc-native classes (the compiled ``pro`` engine) have no ``__weakref__`` slot,
so they cannot be the target of ``weakref.ref`` / ``weakref.finalize``. Engine
lifecycle finalizers and the file-watcher back-reference target an instance of
this small, deliberately *uncompiled* class instead.

This module is excluded from mypyc compilation (see ``hatch_build._SKIP_PATHS``)
so it stays a plain Python class with weakref support at runtime. It carries no
intellectual property -- it is pure lifecycle plumbing.
"""

from __future__ import annotations

from typing import Any


class WeakRefToken:
    """Minimal weakref-able object; optionally carries a strong ``engine`` ref.

    ``__slots__`` (with an explicit ``__weakref__`` entry) keeps instances tiny
    while still permitting weak references to them.
    """

    __slots__ = ("__weakref__", "engine")

    def __init__(self, engine: Any = None) -> None:
        self.engine = engine
