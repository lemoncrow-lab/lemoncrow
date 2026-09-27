"""Host capacity for every sandboxed Review build, capture, and runtime run.

Review executes reviewed project code in bubblewrap. bubblewrap isolates the
filesystem and the network but bounds neither memory nor process count, and a
single preview request can start a full production build plus a headless
Chrome. Every sandboxed job therefore passes through this one gate:

* a process-wide slot pool caps how many run at once, so a burst of preview or
  ranking requests queues instead of starting many builds together;
* each command runs in its own transient cgroup (``systemd-run --user
  --scope``) with a memory, swap, and task ceiling when the host supports it,
  and otherwise under a per-process ``RLIMIT_DATA`` ceiling set by a tiny exec
  shim, so a runaway build is killed instead of exhausting the host.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Final

CONCURRENCY_ENV: Final[str] = "LEMONCROW_PREVIEW_CONCURRENCY"
MEMORY_ENV: Final[str] = "LEMONCROW_PREVIEW_MEMORY_MB"
DEFAULT_MEMORY_MB: Final[int] = 4096
MIN_MEMORY_MB: Final[int] = 256
MAX_MEMORY_MB: Final[int] = 65536
MAX_CONCURRENCY: Final[int] = 16
TASKS_MAX: Final[int] = 1024
# A queued job waits at most this long for a slot before it reports "busy".
MAX_SLOT_WAIT_SECONDS: Final[float] = 180.0

# Applied between fork and the sandbox's exec; inherited by every descendant.
_RLIMIT_SHIM: Final[str] = (
    "import os,resource,sys;n=int(sys.argv[1]);"
    "resource.setrlimit(resource.RLIMIT_DATA,(n,n));os.execvp(sys.argv[2],sys.argv[2:])"
)


class SandboxCapacityBusy(RuntimeError):
    """Every sandbox slot stayed busy for the whole wait; the message is user-facing."""


def sandbox_concurrency() -> int:
    """How many sandboxed jobs may run at once in this process."""

    raw = os.environ.get(CONCURRENCY_ENV, "").strip()
    try:
        configured = int(raw) if raw else 0
    except ValueError:
        configured = 0
    if configured > 0:
        return min(configured, MAX_CONCURRENCY)
    # 0 or unset: automatic.
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def sandbox_memory_mb() -> int:
    """Memory ceiling per sandboxed job in MB; an explicit ``0`` disables it."""

    raw = os.environ.get(MEMORY_ENV, "").strip()
    if not raw:
        return DEFAULT_MEMORY_MB
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MEMORY_MB
    return 0 if value == 0 else max(MIN_MEMORY_MB, min(value, MAX_MEMORY_MB))


_SLOTS_GUARD = threading.Lock()
_SLOTS: tuple[int, threading.BoundedSemaphore] | None = None


def _slots() -> tuple[int, threading.BoundedSemaphore]:
    # lc-debt: the pool is per process; a multi-worker deployment gets one pool
    # per worker. A host-wide cap would need a shared lock directory.
    global _SLOTS
    with _SLOTS_GUARD:
        if _SLOTS is None:
            size = sandbox_concurrency()
            _SLOTS = (size, threading.BoundedSemaphore(size))
        return _SLOTS


@contextmanager
def sandbox_slot(timeout_seconds: float) -> Iterator[None]:
    """Hold one of the process-wide sandbox slots, waiting up to *timeout_seconds*."""

    size, slots = _slots()
    wait = max(0.0, min(float(timeout_seconds), MAX_SLOT_WAIT_SECONDS))
    if not slots.acquire(timeout=wait):
        raise SandboxCapacityBusy(
            f"sandbox capacity is busy: {size} sandboxed builds, captures, or runs are already in progress "
            f"(set {CONCURRENCY_ENV} to allow more); retry shortly"
        )
    try:
        yield
    finally:
        slots.release()


# ``systemd-run --user`` reaches the user manager through these. Sandboxed jobs
# are started with a cleared environment, so the limiter passes them explicitly;
# bubblewrap's ``--clearenv`` keeps them away from the reviewed code itself.
_USER_BUS_ENV: Final[tuple[str, ...]] = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


@lru_cache(maxsize=1)
def _cgroup_scope_runner() -> tuple[str, ...]:
    """``env`` + user-bus variables + ``systemd-run`` when transient user scopes work here."""

    systemd_run = shutil.which("systemd-run")
    env_bin = shutil.which("env")
    if not systemd_run or not env_bin:
        return ()
    bus = tuple(f"{name}={os.environ[name]}" for name in _USER_BUS_ENV if os.environ.get(name))
    runner = (env_bin, *bus, systemd_run)
    try:
        # Probe exactly as jobs run: from an otherwise empty environment.
        code = subprocess.call(
            [*_scope_prefix(runner, 64 * 1024 * 1024, 16), "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    return runner if code == 0 else ()


def _scope_prefix(runner: Sequence[str], memory_bytes: int, tasks: int) -> list[str]:
    return [
        *runner,
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--property=MemoryMax={memory_bytes}",
        "--property=MemorySwapMax=0",
        f"--property=TasksMax={tasks}",
        "--",
    ]


def limit_prefix() -> list[str]:
    """Command prefix that bounds one sandboxed process tree's memory and tasks.

    The cgroup scope caps the whole tree (every build worker and Chrome process
    together) and also caps its task count. The fallback ceiling applies to each
    process separately; the slot pool still bounds how many trees run at once.
    """

    memory_mb = sandbox_memory_mb()
    if memory_mb <= 0:
        return []
    memory_bytes = memory_mb * 1024 * 1024
    runner = _cgroup_scope_runner()
    if runner:
        return _scope_prefix(runner, memory_bytes, TASKS_MAX)
    return [sys.executable, "-I", "-S", "-c", _RLIMIT_SHIM, str(memory_bytes)]


def limit_note(returncode: int) -> str:
    """Explain a SIGKILL that the memory ceiling most likely caused, or ``""``."""

    memory_mb = sandbox_memory_mb()
    if returncode in (-9, 137) and memory_mb > 0:
        return (
            f"the sandbox was killed (SIGKILL), most likely for exceeding its {memory_mb} MB memory limit; "
            f"set {MEMORY_ENV} to raise it"
        )
    return ""


def run_limited(command: Sequence[str], *, timeout: float, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
    """Run one sandboxed command in a capacity slot and under the memory ceiling."""

    with sandbox_slot(timeout):
        return subprocess.run([*limit_prefix(), *command], timeout=timeout, **kwargs)
