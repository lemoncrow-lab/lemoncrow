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
  shim. macOS monitors descendant RSS because Darwin rejects RLIMIT_DATA;
  its ceiling can briefly overshoot between samples.
"""

from __future__ import annotations

import os
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
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


# Darwin rejects finite RLIMIT_DATA limits. Monitor resident memory for the
# entire descendant tree instead; kill it on excess memory or deadline.
# lc-debt: macOS RSS sampling permits brief overshoot; use a kernel job limit when available.
_MACOS_MEMORY_SHIM: Final[str] = """
import os, signal, subprocess, sys, time
ceiling = int(sys.argv[1])
deadline = time.monotonic() + float(sys.argv[2])
child = subprocess.Popen(sys.argv[3:], start_new_session=True)
known = {child.pid}
def sample():
    rows = subprocess.check_output(
        ["/bin/ps", "-axo", "pid=,ppid=,rss="], text=True, timeout=2
    ).splitlines()
    processes = [tuple(map(int, row.split())) for row in rows if len(row.split()) == 3]
    children = {}
    rss = {}
    for pid, parent, size in processes:
        children.setdefault(parent, []).append(pid)
        rss[pid] = size * 1024
    pending = [child.pid]
    current = set()
    while pending:
        pid = pending.pop()
        if pid in current:
            continue
        current.add(pid)
        pending.extend(children.get(pid, ()))
    known.update(current)
    return sum(rss.get(pid, 0) for pid in current)
def stop(*args):
    # Include descendants that started a new session (the HTTP supervisor does).
    try:
        sample()
    except Exception:
        pass
    for pid in reversed(sorted(known)):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
signal.signal(signal.SIGTERM, stop)
signal.signal(signal.SIGINT, stop)
code = 1
try:
    while child.poll() is None:
        if sample() > ceiling:
            stop()
            code = 137
            break
        if time.monotonic() >= deadline:
            stop()
            code = 124
            break
        time.sleep(.02)
    else:
        code = child.returncode
finally:
    stop()
    child.wait()
sys.exit(code if code >= 0 else 128 - code)
"""


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


def limit_prefix_for(*, memory_mb: int, timeout: float = 180, tasks: int = TASKS_MAX) -> list[str]:
    """Command prefix for an explicit memory/task budget.

    Shared by Review and other local sandboxed runtimes so cgroup/RLIMIT
    behavior does not fork into subtly different implementations.
    """

    if memory_mb <= 0:
        return []
    bounded_mb = max(MIN_MEMORY_MB, min(int(memory_mb), MAX_MEMORY_MB))
    bounded_tasks = max(1, min(int(tasks), TASKS_MAX))
    memory_bytes = bounded_mb * 1024 * 1024
    if sys.platform == "darwin":
        return [sys.executable, "-I", "-S", "-c", _MACOS_MEMORY_SHIM, str(memory_bytes), str(timeout)]
    runner = _cgroup_scope_runner()
    if runner:
        return _scope_prefix(runner, memory_bytes, bounded_tasks)
    return [sys.executable, "-I", "-S", "-c", _RLIMIT_SHIM, str(memory_bytes)]


def limit_prefix(*, timeout: float = 180) -> list[str]:
    """Command prefix that bounds one sandboxed process tree's memory and tasks."""

    return limit_prefix_for(memory_mb=sandbox_memory_mb(), timeout=timeout, tasks=TASKS_MAX)


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
        proc = subprocess.run(
            [*limit_prefix(timeout=timeout), *command],
            timeout=timeout + 3 if sys.platform == "darwin" and sandbox_memory_mb() else timeout,
            **kwargs,
        )
        if sys.platform == "darwin" and proc.returncode == 124:
            raise subprocess.TimeoutExpired(command, timeout, output=proc.stdout, stderr=proc.stderr)
        return proc


def run_limited_bounded(
    command: Sequence[str],
    *,
    timeout: float,
    max_output_bytes: int = 8192,
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Execute with one sandbox slot and bounded stdout/stderr memory.

    Unlike subprocess.run(PIPE), this drains *both* pipes while retaining only
    their tails. Nothing from a reviewed process is accumulated unbounded in
    the controller, even if a command continuously prints until timeout.
    A fresh process group allows a deadline to kill descendants holding pipes.
    """
    if not 1 <= max_output_bytes <= 65536:
        raise ValueError("bounded Review output must be 1..65536 bytes per stream")
    forbidden = {"stdout", "stderr", "text", "encoding", "errors", "check", "start_new_session"}
    if forbidden.intersection(kwargs):
        raise ValueError("bounded Review capture owns stdout, stderr and process lifetime")
    actual = [*limit_prefix(timeout=timeout), *command]
    tails: dict[int, bytearray] = {}
    proc: subprocess.Popen[bytes] | None = None
    completed = False
    with sandbox_slot(timeout):
        try:
            proc = subprocess.Popen(
                actual,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
                **kwargs,
            )
            assert proc.stdout is not None and proc.stderr is not None
            with selectors.DefaultSelector() as selector:
                for stream in (proc.stdout, proc.stderr):
                    fd = stream.fileno()
                    tails[fd] = bytearray()
                    selector.register(stream, selectors.EVENT_READ)
                deadline = time.monotonic() + (
                    timeout + 3 if sys.platform == "darwin" and sandbox_memory_mb() else timeout
                )
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(actual, timeout)
                    for key, _event in selector.select(min(remaining, 0.2)):
                        chunk = os.read(key.fd, 32768)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        tail = tails[key.fd]
                        tail.extend(chunk)
                        if len(tail) > max_output_bytes:
                            del tail[:-max_output_bytes]
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(actual, timeout)
                proc.wait(timeout=remaining)
            if sys.platform == "darwin" and proc.returncode == 124:
                raise subprocess.TimeoutExpired(actual, timeout)
            completed = True
            return subprocess.CompletedProcess(
                actual,
                proc.returncode,
                stdout=bytes(tails[proc.stdout.fileno()]).decode("utf-8", "replace"),
                stderr=bytes(tails[proc.stderr.fileno()]).decode("utf-8", "replace"),
            )
        finally:
            if proc is not None:
                if not completed or proc.poll() is None:
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                if proc.stdout is not None:
                    proc.stdout.close()
                if proc.stderr is not None:
                    proc.stderr.close()
