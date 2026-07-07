"""Bench-mode singleton. ATELIER_BENCH_MODE=off produces the
clean baseline arm — no Atelier routing, compaction, memory reads, or MCP
tool substitution. Call bootstrap() once at process start (done by main()).
is_off() lazy-bootstraps if needed so test code works without calling main().
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

__all__ = ["BenchMode", "bootstrap", "is_off", "make_arm_env", "mode"]


class BenchMode(StrEnum):
    ON = "on"
    OFF = "off"


_mode: BenchMode | None = None


def bootstrap() -> None:
    """Read ATELIER_BENCH_MODE once and freeze _mode. Idempotent."""
    global _mode
    if _mode is not None:
        return
    raw = os.environ.get("ATELIER_BENCH_MODE", "on").strip().lower()
    _mode = BenchMode.OFF if raw == "off" else BenchMode.ON


def is_off() -> bool:
    """Return True when bench mode is off. Lazy-bootstraps if needed."""
    if _mode is None:
        bootstrap()
    return _mode == BenchMode.OFF


def mode() -> BenchMode:
    """Return the current BenchMode. Lazy-bootstraps if needed."""
    if _mode is None:
        bootstrap()
    return _mode  # type: ignore[return-value]


def make_arm_env(atelier_root: Path, *, mode: BenchMode | None = None) -> dict[str, str]:
    """Return a subprocess env dict with isolated ATELIER_ROOT and ATELIER_BENCH_MODE.

    Priority for mode: explicit arg > current _mode singleton > BenchMode.ON fallback.
    """
    env: dict[str, str] = dict(os.environ)
    env["ATELIER_ROOT"] = str(atelier_root)
    if mode is not None:
        mode_to_use = mode
    elif _mode is not None:
        mode_to_use = _mode
    else:
        mode_to_use = BenchMode.ON
    env["ATELIER_BENCH_MODE"] = mode_to_use.value
    return env
