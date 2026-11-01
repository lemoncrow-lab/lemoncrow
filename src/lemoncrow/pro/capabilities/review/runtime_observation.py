"""Runtime side-effect observation of sandboxed runs, diffed base vs head (Phase 9R items 10, 14).

An observed run is an ordinary R4 sandboxed execution (local Bubblewrap,
customer worker or managed sandbox) with one addition: an *observer* records
what the reviewed code did while it ran. Sources, all inside the sandbox:

* **syscall tracer** (``strace -f``, when the sandbox runtime ships it): every
  process exec, every attempted ``connect`` (blocked by the private network
  namespace and recorded with its errno), every file opened for writing,
  renamed or deleted -- for every process, whatever its language;
* **Python observer** (``sitecustomize`` on ``PYTHONPATH``): SQLite statements
  (query count and schema mutations), DNS lookups by name, uncaught exceptions
  and ERROR log records, and the call edges of changed Python files
  (``sys.monitoring``) that ground the changed-execution-flow view
  (:mod:`flow_traces`). Without a tracer it also records execs, connects and
  writes through audit hooks (``hooks`` tier);
* **recording sinks** (R4): intercepted HTTP/SMTP/queue/object-store calls and
  blocked TLS ``CONNECT``;
* **workspace diff**: files the run created, modified or deleted in the
  materialized Snapshot;
* **output scan**: runtime error lines (tracebacks, uncaught errors, panics).

Everything is normalized to stable identities (temporary names, digits and
literals folded), bounded, and redacted before storage. A side is compared
with the other only under the same environment fingerprint (R4) -- which
includes the observer tier, because it is part of the recipe -- otherwise the
pair is *not comparable* and yields no delta. Only differences become
``side_effect`` behavior deltas (runtime-observed, distinct from R3's static
inventory by producer and subject namespace).

Honesty limits (stated on every record): the observer runs inside the
sandbox beside the reviewed code, so a hostile change can suppress or forge
its own observations; the absence of a runtime side effect is therefore never
a verified negative. Server-database queries (sidecars) are visible only as
connections; SQL is captured for SQLite through the Python observer.
"""

from __future__ import annotations

import functools
import hashlib
import ipaddress
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, cast

from lemoncrow.pro.capabilities.review.behavior import (
    BEHAVIOR_CONTRACT_VERSION,
    BehaviorContext,
    BehaviorDelta,
    BehaviorEvidenceRef,
    BehaviorObservation,
    BehaviorProduction,
    BehaviorSeverity,
    BehaviorSide,
    ExecutedScope,
    ExecutorClass,
)
from lemoncrow.pro.capabilities.review.execution import SinkCapture, canonical_digest
from lemoncrow.pro.capabilities.review.run_evidence import RedactionPolicy, protect_text
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence

OBSERVATION_VERSION: Final[int] = 1
RUNTIME_OBSERVATION_MEDIA_TYPE: Final[str] = "application/vnd.lemoncrow.runtime-observation+json;version=1"
RUNTIME_OBSERVATION_PRODUCER_ID: Final[str] = "lemoncrow.runtime_observation"
RUNTIME_OBSERVATION_ENTRY: Final[str] = "side_effects.runtime"

OBSERVE_ASSETS_MOUNT: Final[str] = "/lemoncrow-observe"
OBSERVE_OUT_MOUNT: Final[str] = "/lemoncrow-observe-out"
OBSERVE_EVENTS: Final[str] = "events.jsonl"
OBSERVE_STRACE_PREFIX: Final[str] = "st"

MAX_ROWS: Final[int] = 20_000
MAX_EFFECTS_PER_CATEGORY: Final[int] = 200
MAX_TRACE_EDGES: Final[int] = 512
MAX_TRACE_PATHS: Final[int] = 256
MAX_RAW_BYTES: Final[int] = 8 * 1024 * 1024
_MAX_IDENTITY: Final[int] = 300
_MAX_DETAIL: Final[int] = 400
_SANDBOX_BINS: Final[tuple[str, ...]] = ("/usr/local/bin", "/usr/bin", "/bin")

ObservationCategory = Literal["network", "file_write", "process", "db_query", "schema_mutation", "console_error"]
OBSERVATION_CATEGORIES: Final[tuple[ObservationCategory, ...]] = (
    "network",
    "file_write",
    "process",
    "db_query",
    "schema_mutation",
    "console_error",
)
CATEGORY_LABELS: Final[Mapping[str, str]] = {
    "network": "Network",
    "file_write": "File written",
    "process": "Process spawned",
    "db_query": "Database query",
    "schema_mutation": "Schema mutation",
    "console_error": "Runtime error",
}
ObserverTier = Literal["strace", "hooks"]
NetworkOutcome = Literal["blocked", "intercepted", "connected", ""]

# lc-debt: SQL is observed through Python's sqlite3 only; a server database (PostgreSQL, MySQL) is a connection,
# never a captured query. Upgrade: a wire-protocol or driver-level recorder per engine, qualified like the sinks.
LIMITATIONS: Final[tuple[str, ...]] = (
    "The observer runs inside the sandbox beside the reviewed code; absence of a side effect is not a verified negative.",
    "SQL is captured for SQLite through the Python observer; server databases are visible as connections only.",
)


# --------------------------------------------------------------------------- #
# In-sandbox observer assets
# --------------------------------------------------------------------------- #

#: ``sitecustomize.py`` mounted read-only at ``/lemoncrow-observe/py``. It only
#: records (an append-only JSONL file) and never alters what the program does;
#: a project's own ``sitecustomize`` is chained after it.
PYTHON_OBSERVER: Final[str] = r"""
def _lemoncrow_observe():
    import os, sys
    out = os.environ.get("LEMONCROW_OBSERVE_OUT", "")
    if not out:
        return
    import json, threading
    try:
        limit = int(os.environ.get("LEMONCROW_OBSERVE_MAX", "20000"))
    except ValueError:
        limit = 20000
    hooks = os.environ.get("LEMONCROW_OBSERVE_HOOKS", "") == "1"
    root = os.environ.get("LEMONCROW_OBSERVE_ROOT", "/workspace")
    try:
        trace = frozenset(str(item) for item in json.loads(os.environ.get("LEMONCROW_OBSERVE_TRACE", "[]")))
    except ValueError:
        trace = frozenset()
    try:
        fd = os.open(out, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_CLOEXEC, 0o600)
    except OSError:
        return
    local = threading.local()
    lock = threading.Lock()
    count = [0]

    def emit(row):
        with lock:
            if count[0] >= limit:
                return
            count[0] += 1
        row["pid"] = os.getpid()
        try:
            os.write(fd, (json.dumps(row, separators=(",", ":"), default=str) + "\n").encode("utf-8", "replace"))
        except (OSError, ValueError):
            pass

    def text(value):
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return str(value)

    def argv_of(value):
        if isinstance(value, (str, bytes)):
            return [text(value)[:256]]
        try:
            return [text(item)[:256] for item in list(value)[:32]]
        except TypeError:
            return []

    write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND

    def hook(event, args):
        if getattr(local, "busy", False):
            return
        local.busy = True
        try:
            if event == "socket.getaddrinfo":
                emit({"t": "dns", "host": text(args[0])[:255]})
            elif not hooks:
                return
            elif event == "socket.connect":
                address = args[1]
                if isinstance(address, tuple) and len(address) >= 2:
                    port = address[1] if isinstance(address[1], int) else 0
                    emit({"t": "connect", "addr": text(address[0])[:255], "port": port, "result": "attempted"})
            elif event == "subprocess.Popen":
                argv = argv_of(args[1])
                emit({"t": "exec", "path": text(args[0] or (argv[0] if argv else "")), "argv": argv})
            elif event in ("os.exec", "os.posix_spawn"):
                emit({"t": "exec", "path": text(args[0]), "argv": argv_of(args[1])})
            elif event == "os.spawn":
                emit({"t": "exec", "path": text(args[1]), "argv": argv_of(args[2])})
            elif event == "os.system":
                emit({"t": "exec", "path": "/bin/sh", "argv": ["sh", "-c", text(args[0])[:256]]})
            elif event == "open":
                path, mode, flags = args[0], args[1], args[2]
                if isinstance(path, int):
                    return
                writes = (isinstance(mode, str) and any(char in mode for char in "wax+")) or (
                    mode is None and isinstance(flags, int) and flags & write_flags
                )
                if writes:
                    emit({"t": "write", "op": "open", "path": os.path.abspath(text(path))[:1024]})
            elif event == "os.remove":
                emit({"t": "write", "op": "delete", "path": os.path.abspath(text(args[0]))[:1024]})
            elif event == "os.rename":
                emit({"t": "write", "op": "rename", "path": os.path.abspath(text(args[1]))[:1024]})
        except Exception:
            pass
        finally:
            local.busy = False

    sys.addaudithook(hook)

    # SQLite statements: the audit event fires before the connection is usable,
    # so ``connect`` is wrapped (same arguments, same return value).
    try:
        import sqlite3

        original_connect = sqlite3.connect

        def connect(*args, **kwargs):
            connection = original_connect(*args, **kwargs)
            try:
                connection.set_trace_callback(lambda sql: emit({"t": "sql", "sql": text(sql)[:1000]}))
            except Exception:
                pass
            return connection

        sqlite3.connect = connect
        sqlite3.dbapi2.connect = connect
    except Exception:
        pass

    prior_excepthook = sys.excepthook

    def excepthook(kind, value, tb):
        emit({"t": "error", "type": getattr(kind, "__qualname__", str(kind)), "message": text(value)[:300]})
        return prior_excepthook(kind, value, tb)

    sys.excepthook = excepthook
    prior_thread_hook = threading.excepthook

    def thread_hook(info):
        kind = info.exc_type
        emit({"t": "error", "type": getattr(kind, "__qualname__", str(kind)), "message": text(info.exc_value)[:300]})
        return prior_thread_hook(info)

    threading.excepthook = thread_hook
    try:
        import logging

        original_handle = logging.Logger.handle

        def handle(self, record):
            if record.levelno >= logging.ERROR:
                emit({"t": "log", "level": record.levelname, "logger": text(record.name)[:100], "message": text(record.msg)[:300]})
            return original_handle(self, record)

        logging.Logger.handle = handle
    except Exception:
        pass

    monitoring = getattr(sys, "monitoring", None)
    if not trace or monitoring is None:
        return
    tool = 4
    try:
        monitoring.use_tool_id(tool, "lemoncrow-observe")
    except ValueError:
        return
    real_root = os.path.realpath(root)
    cache = {}
    seen = set()
    disable = monitoring.DISABLE

    def relative(filename):
        found = cache.get(filename)
        if found is None:
            found = ""
            if filename and not filename.startswith("<"):
                try:
                    found = os.path.relpath(os.path.realpath(filename), real_root)
                except ValueError:
                    found = ""
                if found.startswith(".."):
                    found = ""
            cache[filename] = found
        return found

    def on_start(code, offset):
        callee = relative(code.co_filename)
        if callee not in trace or code.co_name == "<module>":
            return disable
        try:
            caller = sys._getframe(2).f_code
        except ValueError:
            return None
        edge = (relative(caller.co_filename) or "<external>", caller.co_qualname, callee, code.co_qualname)
        if edge in seen:
            return None
        if len(seen) >= 2048:
            return disable
        seen.add(edge)
        emit({"t": "call", "caller": edge[0], "caller_fn": edge[1], "callee": edge[2], "callee_fn": edge[3]})
        return None

    monitoring.register_callback(tool, monitoring.events.PY_START, on_start)
    monitoring.set_events(tool, monitoring.events.PY_START)


def _lemoncrow_chain():
    import importlib.machinery, importlib.util, os, sys
    here = os.path.realpath(os.path.dirname(__file__))
    path = [entry for entry in sys.path if os.path.realpath(entry or ".") != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", path)
    if spec is None or spec.loader is None:
        return
    module = importlib.util.module_from_spec(spec)
    sys.modules["sitecustomize"] = module
    spec.loader.exec_module(module)


try:
    _lemoncrow_observe()
except Exception:
    pass
try:
    _lemoncrow_chain()
except Exception:
    pass
"""

_STRACE_SYSCALLS: Final[str] = "execve,connect,openat,open,creat,unlinkat,unlink,renameat,renameat2,rename"


def sandbox_strace() -> str:
    """The tracer inside the sandbox's read-only runtime mounts, or ``""``."""

    for root in _SANDBOX_BINS:
        candidate = Path(root, "strace")
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


@functools.lru_cache(maxsize=4)
def strace_works(path: str) -> bool:
    """Whether the tracer can attach here (ptrace may be denied by Yama or a seccomp profile).

    Probed once per process: a tracer that cannot attach would fail the observed run itself.
    """

    try:
        proc = subprocess.run(
            [path, "-f", "-qq", "-o", os.devnull, "--", "true"],
            capture_output=True,
            timeout=15,
            check=False,
            env={"PATH": ":".join(_SANDBOX_BINS)},
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


@dataclass(frozen=True, slots=True)
class ObservationSpec:
    """What a trusted dispatcher asks the runner to observe (never YAML-constructible).

    ``trace_paths`` are repository-relative Python files whose call edges are
    recorded (the changed files). ``tier`` ``auto`` uses the syscall tracer
    when the sandbox runtime ships it; ``hooks`` forces the Python-only tier.
    """

    trace_paths: tuple[str, ...] = ()
    tier: Literal["auto", "hooks"] = "auto"

    def to_wire(self) -> dict[str, Any]:
        return {"version": OBSERVATION_VERSION, "trace_paths": list(self.trace_paths), "tier": self.tier}

    @classmethod
    def from_wire(cls, raw: object) -> ObservationSpec:
        if not isinstance(raw, Mapping) or int(raw.get("version") or 0) != OBSERVATION_VERSION:
            raise ValueError("runtime observation spec is malformed")
        paths = raw.get("trace_paths")
        rows = paths if isinstance(paths, Sequence) and not isinstance(paths, (str, bytes)) else ()
        return cls(trace_paths=_trace_paths(rows), tier="hooks" if raw.get("tier") == "hooks" else "auto")


def _trace_paths(rows: Iterable[object]) -> tuple[str, ...]:
    out: list[str] = []
    for item in rows:
        text = str(item)
        rel = PurePosixPath(text)
        if not text.endswith(".py") or rel.is_absolute() or ".." in rel.parts or not rel.parts:
            continue
        if text not in out:
            out.append(text)
        if len(out) >= MAX_TRACE_PATHS:
            break
    return tuple(sorted(out))


def observer_environment(spec: ObservationSpec, *, tier: str, sandbox_root: str) -> dict[str, str]:
    """Observer variables of one run; part of the recipe, so the tier enters the fingerprint."""

    return {
        "PYTHONPATH": f"{OBSERVE_ASSETS_MOUNT}/py",
        "PYTHONDONTWRITEBYTECODE": "1",
        "LEMONCROW_OBSERVE_OUT": f"{OBSERVE_OUT_MOUNT}/{OBSERVE_EVENTS}",
        "LEMONCROW_OBSERVE_ROOT": sandbox_root,
        "LEMONCROW_OBSERVE_TRACE": json.dumps(list(spec.trace_paths), separators=(",", ":")),
        "LEMONCROW_OBSERVE_HOOKS": "0" if tier == "strace" else "1",
        "LEMONCROW_OBSERVE_MAX": str(MAX_ROWS),
        "LEMONCROW_OBSERVE_TIER": tier,
    }


@dataclass
class ObservationSetup:
    """One run's observer: assets, output directory, environment and tracer wrap."""

    directory: Path
    spec: ObservationSpec
    tier: ObserverTier
    sandbox_root: str
    strace: str = ""

    @classmethod
    def create(cls, parent: Path, spec: ObservationSpec, *, sandbox_root: str) -> ObservationSetup:
        parent.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="observe-", dir=parent))
        directory.chmod(0o700)
        assets = directory / "assets" / "py"
        assets.mkdir(parents=True)
        (assets / "sitecustomize.py").write_text(PYTHON_OBSERVER, encoding="utf-8")
        (directory / "out").mkdir(mode=0o700)
        strace = sandbox_strace() if spec.tier == "auto" else ""
        if strace and not strace_works(strace):
            strace = ""  # no ptrace here: the Python hook tier observes instead
        return cls(directory, spec, "strace" if strace else "hooks", sandbox_root, strace)

    @property
    def out(self) -> Path:
        return self.directory / "out"

    def environment(self) -> dict[str, str]:
        return observer_environment(self.spec, tier=self.tier, sandbox_root=self.sandbox_root)

    def bwrap_args(self, *, pythonpath: str = "") -> list[str]:
        """Mounts plus ``--setenv`` (after ``--clearenv``); an existing *pythonpath* is kept after ours."""

        args = [
            "--ro-bind",
            str(self.directory / "assets"),
            OBSERVE_ASSETS_MOUNT,
            "--bind",
            str(self.out),
            OBSERVE_OUT_MOUNT,
        ]
        environment = self.environment()
        if pythonpath:
            environment["PYTHONPATH"] = f"{environment['PYTHONPATH']}:{pythonpath}"
        for key, value in sorted(environment.items()):
            args.extend(("--setenv", key, value))
        return args

    def wrap(self, command: Sequence[str]) -> list[str]:
        if self.tier != "strace":
            return list(command)
        return [
            self.strace,
            "-f",
            "-ff",
            "-qq",
            "-y",
            "-s",
            "256",
            "-e",
            f"trace={_STRACE_SYSCALLS}",
            "-e",
            "signal=none",
            "-o",
            f"{OBSERVE_OUT_MOUNT}/{OBSERVE_STRACE_PREFIX}",
            "--",
            *command,
        ]

    def collect(self, command: Sequence[str], **bounds: int) -> dict[str, Any]:
        """Bounded raw observer rows (still unredacted; :func:`build_observation` redacts)."""

        return collect_raw(self.out, tier=self.tier, command=command, **bounds)

    def cleanup(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def _read_bounded(path: Path, budget: list[int]) -> str:
    """Read a reviewed-code-written file without following links, within *budget* bytes."""

    if budget[0] <= 0:
        return ""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return ""
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return ""
        payload = handle.read(budget[0])
    budget[0] -= len(payload)
    return payload.decode("utf-8", errors="replace")


def _strace_order(name: str) -> tuple[int, str]:
    suffix = name.rpartition(".")[2]
    return (int(suffix), name) if suffix.isdigit() else (1 << 62, name)


class _RowAggregate:
    """Identical rows collapse into one with a count ``n`` (first-seen order kept)."""

    def __init__(self, max_rows: int, max_bytes: int) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.max_rows = max_rows
        self.bytes_left = max_bytes
        self.truncated = False

    def add(self, raw: Mapping[str, Any]) -> None:
        row = {str(key): value for key, value in raw.items() if key not in {"pid", "n"}}
        if row.get("t") == "sql":
            row["sql"] = normalize_sql(str(row.get("sql") or ""))
        key = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
        found = self.rows.get(key)
        if found is not None:
            found["n"] += 1
            return
        if len(self.rows) >= self.max_rows or len(key) > self.bytes_left:
            self.truncated = True
            return
        self.bytes_left -= len(key)
        row["n"] = 1
        self.rows[key] = row


def collect_raw(
    out: Path,
    *,
    tier: str,
    command: Sequence[str],
    max_rows: int = MAX_ROWS,
    max_bytes: int = MAX_RAW_BYTES,
) -> dict[str, Any]:
    """Observer rows from an output directory: hook JSONL plus parsed tracer files, aggregated."""

    budget = [MAX_RAW_BYTES]
    aggregate = _RowAggregate(max_rows, max_bytes)
    for line in _read_bounded(out / OBSERVE_EVENTS, budget).splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            aggregate.add(row)
    if tier == "strace":
        try:
            names = sorted(
                (entry.name for entry in os.scandir(out) if entry.name.startswith(f"{OBSERVE_STRACE_PREFIX}.")),
                key=_strace_order,
            )
        except OSError:
            names = []
        root_skipped = False
        for name in names[:4096]:
            for row in parse_strace(_read_bounded(out / name, budget)):
                if not root_skipped and row.get("t") == "exec" and list(row.get("argv") or ()) == list(command):
                    # The recipe command itself: identical on both sides, not a side effect.
                    root_skipped = True
                    continue
                aggregate.add(row)
        if len(names) > 4096:
            aggregate.truncated = True
        if not names:
            # The tracer wrote nothing (it could not attach, or the run never started):
            # an empty syscall record is no observation, never "no side effects".
            tier = "unobserved"
    return {
        "tier": tier,
        "rows": list(aggregate.rows.values()),
        "truncated": aggregate.truncated or budget[0] <= 0,
    }


# --------------------------------------------------------------------------- #
# strace parsing
# --------------------------------------------------------------------------- #

_SYSCALL_RE = re.compile(r"^(?:\d+\s+)?(?P<name>[a-z_0-9]+)\((?P<args>.*)\)\s+=\s+(?P<result>-?\d+|\?)(?P<rest>.*)$")
_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_FD_PATH_RE = re.compile(r"^<(.*)>")
_ERRNO_RE = re.compile(r"\s([A-Z][A-Z0-9_]+)\s")
_INET_RE = re.compile(r'sin_port=htons\((\d+)\), sin_addr=inet_addr\("([^"]+)"\)')
_INET6_RE = re.compile(r'sin6_port=htons\((\d+)\).*?inet_pton\(AF_INET6, "([^"]+)"')
_WRITE_FLAGS: Final[tuple[str, ...]] = ("O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND")


def _unescape(value: str) -> str:
    try:
        return value.encode("latin-1", errors="backslashreplace").decode("unicode_escape")
    except (UnicodeDecodeError, ValueError):
        return value


def _dirfd_join(args: str, path: str) -> str:
    """Resolve a relative ``*at`` path against strace's decoded ``AT_FDCWD</cwd>``."""

    if path.startswith("/"):
        return path
    match = re.match(r"^(?:AT_FDCWD|\d+)<([^>]*)>", args)
    return f"{match.group(1).rstrip('/')}/{path}" if match else path


def parse_strace(text: str) -> list[dict[str, Any]]:
    """Structured rows from one ``strace -ff -y`` file (one process)."""

    rows: list[dict[str, Any]] = []
    for line in text.splitlines():
        match = _SYSCALL_RE.match(line.strip())
        if match is None:
            continue
        name, args, result, rest = match.group("name", "args", "result", "rest")
        ok = result not in {"?"} and not result.startswith("-")
        errno_match = _ERRNO_RE.search(rest + " ")
        errno = errno_match.group(1) if errno_match and not ok else ""
        quoted = [_unescape(item) for item in _QUOTED_RE.findall(args)]
        if name == "execve":
            if not ok or not quoted:
                continue
            bracket = args.find("[")
            argv = [_unescape(item) for item in _QUOTED_RE.findall(args[bracket:])] if bracket >= 0 else []
            rows.append({"t": "exec", "path": quoted[0], "argv": argv[:32]})
        elif name == "connect":
            family = "inet6" if "AF_INET6" in args else "inet" if "AF_INET" in args else ""
            found = (_INET6_RE if family == "inet6" else _INET_RE).search(args) if family else None
            if found is None:
                continue
            rows.append(
                {
                    "t": "connect",
                    "addr": found.group(2),
                    "port": int(found.group(1)),
                    "result": "ok" if ok else (errno or "failed"),
                }
            )
        elif name in {"openat", "open", "creat"}:
            if not ok or not quoted:
                continue
            if name != "creat" and not any(flag in args for flag in _WRITE_FLAGS):
                continue
            resolved = _FD_PATH_RE.match(rest.strip())
            path = resolved.group(1) if resolved else (_dirfd_join(args, quoted[0]) if name == "openat" else quoted[0])
            rows.append({"t": "write", "op": "open", "path": path})
        elif name in {"unlinkat", "unlink"}:
            if ok and quoted:
                rows.append({"t": "write", "op": "delete", "path": _dirfd_join(args, quoted[0])})
        elif name in {"renameat", "renameat2", "rename"}:
            if ok and len(quoted) >= 2:
                destination = quoted[1]
                directories = re.findall(r"(?:AT_FDCWD|\d+)<([^>]*)>", args)
                if name != "rename" and not destination.startswith("/") and len(directories) >= 2:
                    destination = f"{directories[1].rstrip('/')}/{destination}"
                rows.append({"t": "write", "op": "rename", "path": destination})
    return rows


# --------------------------------------------------------------------------- #
# normalization
# --------------------------------------------------------------------------- #

_DIGITS_RE = re.compile(r"\d+")
_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
_RANDOM_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9_]*\d)(?=[A-Za-z0-9_]*[A-Za-z])[A-Za-z0-9_]{8,}(?![A-Za-z0-9])"
)
_TMP_NAME_RE = re.compile(r"\btmp[A-Za-z0-9_]{4,}")
_SQL_STRING_RE = re.compile(r"'(?:[^']|'')*'")
_SQL_NUMBER_RE = re.compile(r"(?<![A-Za-z_])-?\d+(?:\.\d+)?")
_SQL_LIST_RE = re.compile(r"\(\s*\?(?:\s*,\s*\?)+\s*\)")
_DDL_RE = re.compile(r"^\s*(CREATE|ALTER|DROP|RENAME|TRUNCATE)\b", re.IGNORECASE)
_SQL_SKIP_RE = re.compile(r"^\s*(BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE|PRAGMA)\b", re.IGNORECASE)
_IGNORED_WRITE_PREFIXES: Final[tuple[str, ...]] = (
    "/dev/",
    "/proc/",
    "/sys/",
    OBSERVE_OUT_MOUNT,
    OBSERVE_ASSETS_MOUNT,
    "/lemoncrow-sinks",
    "/tmp/lemoncrow-reports",
    "/tmp/lc-profile",
)
_IGNORED_WRITE_PARTS: Final[frozenset[str]] = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
_ERROR_LINE_RE = re.compile(
    r"^(?:Uncaught |Error: )?(?P<type>(?:[A-Za-z_][\w]*\.)*[A-Z]\w*(?:Error|Exception|Rejection))(?::\s?(?P<msg>.*))?$"
)
_PANIC_RE = re.compile(r"^(?:panic: (?P<go>.*)|thread '.*' panicked at (?P<rust>.*))$")


def _fold(text: str) -> str:
    folded = _HEX_RE.sub("0x#", text)
    folded = _TMP_NAME_RE.sub("tmp*", folded)
    folded = _RANDOM_TOKEN_RE.sub("*", folded)
    return _DIGITS_RE.sub("#", folded)


def normalize_path(path: str, *, sandbox_root: str) -> str:
    """Stable identity of a written path: workspace-relative, temporary names folded."""

    clean = os.path.normpath(path) if path else ""
    root = sandbox_root.rstrip("/")
    if root and (clean == root or clean.startswith(root + "/")):
        return "./" + clean[len(root) + 1 :] if clean != root else "."
    return _fold(clean)


def ignored_write(path: str, *, sandbox_root: str) -> bool:
    if not path or path.startswith(_IGNORED_WRITE_PREFIXES):
        return True
    parts = PurePosixPath(path).parts
    return any(part in _IGNORED_WRITE_PARTS for part in parts) or path.endswith((".pyc", ".pyo"))


def normalize_sql(sql: str) -> str:
    text = " ".join(str(sql).split())
    text = _SQL_STRING_RE.sub("?", text)
    text = _SQL_NUMBER_RE.sub("?", text)
    text = _SQL_LIST_RE.sub("(?...)", text)
    return text[:_MAX_IDENTITY]


def _loopback(address: str) -> bool:
    try:
        parsed = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return address in {"localhost", ""}
    return parsed.is_loopback or parsed.is_unspecified


def _is_address(value: str) -> bool:
    try:
        ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    return True


def process_identity(path: str, argv: Sequence[str]) -> str:
    """``tool subcommand``: the executable's name and a word-like first argument."""

    name = PurePosixPath(str(argv[0]) if argv else path).name or PurePosixPath(path).name
    if not name:
        return ""
    first = str(argv[1]) if len(argv) > 1 else ""
    if first and (re.fullmatch(r"-[A-Za-z]{1,2}", first) or re.fullmatch(r"[a-z][a-z0-9_.:-]{0,30}", first)):
        return f"{name} {first}"
    return name


def _host_of(url: str) -> str:
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return url[:200]
    host = parts.hostname or ""
    path = _fold(parts.path or "/")[:120]
    return f"{host}{path}" if host else url[:200]


# --------------------------------------------------------------------------- #
# the observation model
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ObservedEffect:
    """One distinct runtime side effect of one side (normalized identity, aggregated)."""

    category: ObservationCategory
    identity: str
    detail: str = ""
    count: int = 1
    outcome: NetworkOutcome = ""
    sources: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.category}:{self.identity}"

    def to_wire(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "identity": self.identity,
            "detail": self.detail,
            "count": self.count,
            "outcome": self.outcome,
            "sources": list(self.sources),
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> ObservedEffect:
        category = str(raw.get("category") or "")
        if category not in OBSERVATION_CATEGORIES:
            raise ValueError(f"unknown observation category {category!r}")
        outcome = str(raw.get("outcome") or "")
        sources = raw.get("sources")
        return cls(
            category=category,
            identity=str(raw.get("identity") or "")[:_MAX_IDENTITY],
            detail=str(raw.get("detail") or "")[:_MAX_DETAIL],
            count=max(0, int(raw.get("count") or 0)),
            outcome=cast(NetworkOutcome, outcome if outcome in {"blocked", "intercepted", "connected"} else ""),
            sources=(
                tuple(str(item)[:32] for item in sources)[:8]
                if isinstance(sources, Sequence) and not isinstance(sources, str)
                else ()
            ),
        )


@dataclass(frozen=True, slots=True)
class TraceEdge:
    """One observed call edge into a changed file (first-seen order)."""

    caller: str
    caller_fn: str
    callee: str
    callee_fn: str

    def to_wire(self) -> dict[str, str]:
        return {"caller": self.caller, "caller_fn": self.caller_fn, "callee": self.callee, "callee_fn": self.callee_fn}

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> TraceEdge:
        return cls(*(str(raw.get(name) or "")[:300] for name in ("caller", "caller_fn", "callee", "callee_fn")))


@dataclass(frozen=True, slots=True)
class RuntimeObservation:
    """Everything one side was observed to do during one sandboxed run."""

    side: str
    tier: str
    effects: tuple[ObservedEffect, ...] = ()
    query_count: int = 0
    trace: tuple[TraceEdge, ...] = ()
    trace_supported: bool = True
    truncated: bool = False
    version: int = OBSERVATION_VERSION

    def effect(self, key: str) -> ObservedEffect | None:
        return next((item for item in self.effects if item.key == key), None)

    @property
    def digest(self) -> str:
        return "obs:sha256:" + canonical_digest(
            {
                "tier": self.tier,
                "effects": sorted(
                    [item.category, item.identity, item.outcome, item.count if item.category == "db_query" else 0]
                    for item in self.effects
                ),
                "query_count": self.query_count,
            }
        )

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = dict.fromkeys(OBSERVATION_CATEGORIES, 0)
        for item in self.effects:
            out[item.category] += 1
        return out

    def to_wire(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "side": self.side,
            "tier": self.tier,
            "effects": [item.to_wire() for item in self.effects],
            "query_count": self.query_count,
            "trace": [edge.to_wire() for edge in self.trace],
            "trace_supported": self.trace_supported,
            "truncated": self.truncated,
            "counts": self.counts(),
            "digest": self.digest,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> RuntimeObservation:
        if int(raw.get("version") or 0) != OBSERVATION_VERSION:
            raise ValueError("runtime observation version is not supported")
        effects = raw.get("effects")
        trace = raw.get("trace")
        return cls(
            side=str(raw.get("side") or ""),
            tier=str(raw.get("tier") or ""),
            effects=tuple(
                ObservedEffect.from_wire(item)
                for item in (effects if isinstance(effects, list) else [])[: MAX_EFFECTS_PER_CATEGORY * 6]
                if isinstance(item, Mapping)
            ),
            query_count=max(0, int(raw.get("query_count") or 0)),
            trace=tuple(
                TraceEdge.from_wire(item)
                for item in (trace if isinstance(trace, list) else [])[:MAX_TRACE_EDGES]
                if isinstance(item, Mapping)
            ),
            trace_supported=raw.get("trace_supported") is not False,
            truncated=bool(raw.get("truncated")),
        )


@dataclass
class _Aggregate:
    category: ObservationCategory
    identity: str
    detail: str
    count: int = 0
    outcome: NetworkOutcome = ""
    sources: set[str] = field(default_factory=set)


_OUTCOME_RANK: Final[Mapping[str, int]] = {"": 0, "intercepted": 1, "blocked": 2, "connected": 3}


class _Builder:
    def __init__(self, policy: RedactionPolicy | None) -> None:
        self.policy = policy
        self.rows: dict[tuple[str, str], _Aggregate] = {}
        self.per_category: dict[str, int] = {}
        self.truncated = False

    def add(
        self,
        category: ObservationCategory,
        identity: str,
        detail: str,
        *,
        source: str,
        outcome: NetworkOutcome = "",
        count: int = 1,
    ) -> None:
        identity = protect_text(identity[:_MAX_IDENTITY], self.policy)[0]
        if not identity:
            return
        key = (category, identity)
        row = self.rows.get(key)
        if row is None:
            if self.per_category.get(category, 0) >= MAX_EFFECTS_PER_CATEGORY:
                self.truncated = True
                return
            self.per_category[category] = self.per_category.get(category, 0) + 1
            row = _Aggregate(category, identity, protect_text(detail[:_MAX_DETAIL], self.policy)[0])
            self.rows[key] = row
        row.count += count
        row.sources.add(source)
        if _OUTCOME_RANK[outcome] > _OUTCOME_RANK[row.outcome]:
            row.outcome = outcome

    def effects(self) -> tuple[ObservedEffect, ...]:
        return tuple(
            ObservedEffect(
                category=row.category,
                identity=row.identity,
                detail=row.detail,
                count=row.count,
                outcome=row.outcome,
                sources=tuple(sorted(row.sources)),
            )
            for row in sorted(self.rows.values(), key=lambda item: (item.category, item.identity))
        )


def scan_output_errors(output: str) -> list[tuple[str, str]]:
    """``(identity, detail)`` of runtime error lines in a run's (redacted) output."""

    found: list[tuple[str, str]] = []
    for raw in output.splitlines()[-4000:]:
        line = raw.strip()
        if not line or len(line) > 2000:
            continue
        match = _ERROR_LINE_RE.match(line)
        if match is not None:
            kind = match.group("type").rsplit(".", 1)[-1]
            message = _fold(str(match.group("msg") or "").strip())[:160]
            found.append((f"{kind}: {message}" if message else kind, line[:_MAX_DETAIL]))
            continue
        panic = _PANIC_RE.match(line)
        if panic is not None:
            message = _fold(str(panic.group("go") or panic.group("rust") or "").strip())[:160]
            found.append((f"panic: {message}", line[:_MAX_DETAIL]))
    return found


def build_observation(
    *,
    side: str,
    raw: Mapping[str, Any] | None,
    sink_captures: Sequence[SinkCapture] = (),
    workspace_changes: Sequence[tuple[str, str]] = (),
    output: str = "",
    sandbox_root: str = "/workspace",
    policy: RedactionPolicy | None = None,
) -> RuntimeObservation:
    """Normalize one side's raw observer rows, sink captures and workspace diff into effects.

    *raw* is untrusted (written beside reviewed code): every row is bounded,
    type-checked and redacted here before anything is stored.
    """

    builder = _Builder(policy)
    raw = raw if isinstance(raw, Mapping) else {}
    tier = str(raw.get("tier") or "")
    tier = tier if tier in {"strace", "hooks", "unobserved"} else "hooks"
    rows = raw.get("rows")
    query_count = 0
    trace: list[TraceEdge] = []
    seen_edges: set[TraceEdge] = set()
    for row in (rows if isinstance(rows, list) else [])[:MAX_ROWS]:
        if not isinstance(row, Mapping):
            continue
        kind = str(row.get("t") or "")
        repeat = row.get("n")
        count = max(1, min(int(repeat), 10_000_000)) if isinstance(repeat, int) else 1
        if kind == "exec":
            argv_raw = row.get("argv")
            argv = [str(item)[:256] for item in argv_raw[:32]] if isinstance(argv_raw, list) else []
            path = str(row.get("path") or "")[:1024]
            identity = process_identity(path, argv)
            builder.add("process", identity, " ".join(argv)[:_MAX_DETAIL] or path, source="exec", count=count)
        elif kind == "connect":
            address = str(row.get("addr") or "")[:255]
            port = row.get("port")
            if _loopback(address):
                continue  # sinks, sidecars and fixtures live on loopback inside the sandbox
            result = str(row.get("result") or "")
            outcome: NetworkOutcome = "connected" if result == "ok" else "blocked"
            target = f"[{address}]" if ":" in address else address
            builder.add(
                "network",
                f"connect {target}:{int(port) if isinstance(port, int) else 0}",
                f"connect {target}:{port} -> {result or 'attempted'}",
                source="connect",
                outcome=outcome,
                count=count,
            )
        elif kind == "dns":
            host = str(row.get("host") or "").strip().lower()[:255]
            if _loopback(host) or _is_address(host) or host in {"localhost", "localhost.localdomain"}:
                continue  # an address literal is no name lookup; its connect is recorded itself
            builder.add(
                "network",
                f"dns {host}",
                f"name lookup {host} (no resolver in the sandbox)",
                source="dns",
                outcome="blocked",
                count=count,
            )
        elif kind == "write":
            path = str(row.get("path") or "")[:1024]
            if ignored_write(path, sandbox_root=sandbox_root):
                continue
            op = str(row.get("op") or "open")[:16]
            identity = normalize_path(path, sandbox_root=sandbox_root)
            builder.add("file_write", identity, op, source="write", count=count)
        elif kind == "sql":
            sql = str(row.get("sql") or "")[:1000]
            if not sql.strip() or _SQL_SKIP_RE.match(sql):
                continue
            normalized = normalize_sql(sql)
            if _DDL_RE.match(sql):
                builder.add("schema_mutation", normalized, normalized, source="sqlite", count=count)
            else:
                query_count += count
                builder.add("db_query", normalized, normalized, source="sqlite", count=count)
        elif kind in {"error", "log"}:
            label = str(row.get("type") or row.get("level") or "Error")[:80]
            message = _fold(str(row.get("message") or "").strip())[:160]
            identity = f"{label}: {message}" if message else label
            detail = str(row.get("message") or "")[:_MAX_DETAIL]
            builder.add("console_error", identity, detail, source=kind, count=count)
        elif kind == "call" and len(trace) < MAX_TRACE_EDGES:
            edge = TraceEdge.from_wire(row)
            if edge.callee and edge not in seen_edges:
                seen_edges.add(edge)
                trace.append(edge)
    for capture in sink_captures:
        method = capture.method or capture.kind
        if capture.kind == "http":
            identity = (
                f"https {capture.destination.removeprefix('https://')} (TLS)"
                if method == "CONNECT"
                else f"http {method} {_host_of(capture.destination)}"
            )
        else:
            identity = f"{capture.kind} {_fold(capture.destination)[:200]}"
        builder.add(
            "network",
            identity,
            capture.summary or f"{capture.kind} {capture.destination}",
            source=f"sink:{capture.kind}",
            outcome="blocked" if capture.blocked else "intercepted",
        )
    for path, status in workspace_changes:
        rel = f"{sandbox_root.rstrip('/')}/{path}"
        if ignored_write(rel, sandbox_root=sandbox_root):
            continue
        builder.add("file_write", normalize_path(rel, sandbox_root=sandbox_root), status, source="workspace")
    for identity, detail in scan_output_errors(output):
        builder.add("console_error", identity, detail, source="output")
    return RuntimeObservation(
        side=side,
        tier=tier,
        effects=builder.effects(),
        query_count=query_count,
        trace=tuple(trace),
        truncated=builder.truncated or bool(raw.get("truncated")),
    )


def workspace_change_rows(before: Mapping[str, str], after: Mapping[str, str]) -> list[tuple[str, str]]:
    """``(path, created|modified|deleted)`` between two ``path -> sha256`` maps."""

    rows: list[tuple[str, str]] = []
    for path in sorted(set(before) | set(after)):
        if before.get(path) == after.get(path):
            continue
        rows.append((path, "created" if path not in before else "deleted" if path not in after else "modified"))
    return rows


# --------------------------------------------------------------------------- #
# base vs head
# --------------------------------------------------------------------------- #

EffectChangeKind = Literal["added", "removed", "changed"]


@dataclass(frozen=True, slots=True)
class EffectChange:
    category: ObservationCategory
    identity: str
    change: EffectChangeKind
    base: ObservedEffect | None = None
    head: ObservedEffect | None = None

    @property
    def key(self) -> str:
        return f"{self.category}:{self.identity}"

    def to_wire(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "identity": self.identity,
            "change": self.change,
            "base": None if self.base is None else self.base.to_wire(),
            "head": None if self.head is None else self.head.to_wire(),
        }


@dataclass(frozen=True, slots=True)
class ObservationDiff:
    comparable: bool
    reasons: tuple[str, ...] = ()
    differences: tuple[str, ...] = ()
    changes: tuple[EffectChange, ...] = ()
    unchanged: int = 0
    query_count: tuple[int, int] = (0, 0)
    # A truncated observation cannot support an exact query-count comparison.
    # Absent in v1 persisted diffs: unknown, not automatically comparable.
    query_count_comparable: bool = False

    def to_wire(self) -> dict[str, Any]:
        return {
            "comparable": self.comparable,
            "reasons": list(self.reasons),
            "differences": list(self.differences),
            "changes": [item.to_wire() for item in self.changes],
            "unchanged": self.unchanged,
            "query_count": {"base": self.query_count[0], "head": self.query_count[1]},
            "query_count_comparable": self.query_count_comparable,
        }


def diff_observations(
    base: RuntimeObservation,
    head: RuntimeObservation,
    *,
    base_environment: str,
    head_environment: str,
    differences: Sequence[str] = (),
) -> ObservationDiff:
    """Observed presence differences under the same environment fingerprint.

    Truncation prevents an absence claim on the truncated side: a truncated
    base cannot prove additions and a truncated head cannot prove removals.
    Truncation on either side also prevents exact count/outcome comparisons;
    recorded counts remain available as raw observations, not proof of change.
    """

    if "unobserved" in {base.tier, head.tier}:
        return ObservationDiff(
            comparable=False,
            reasons=("the observer recorded nothing on at least one side; not comparable",),
            differences=("observer",),
            query_count=(base.query_count, head.query_count),
        )
    if not base_environment or base_environment != head_environment or base.tier != head.tier:
        why = ["environment fingerprints differ; runtime side effects are not comparable"]
        if base.tier != head.tier:
            why.append(f"observer tiers differ ({base.tier} vs {head.tier})")
        fallback = ("observer tier",) if base.tier != head.tier else ("environment fingerprint",)
        return ObservationDiff(
            comparable=False,
            reasons=tuple(why),
            differences=tuple(differences) or fallback,
            query_count=(base.query_count, head.query_count),
        )
    base_map = {item.key: item for item in base.effects}
    head_map = {item.key: item for item in head.effects}
    changes: list[EffectChange] = []
    unchanged = 0
    for key in sorted(set(base_map) | set(head_map)):
        before, after = base_map.get(key), head_map.get(key)
        if before is None and after is not None:
            if base.truncated:
                continue
            changes.append(EffectChange(after.category, after.identity, "added", None, after))
        elif after is None and before is not None:
            if head.truncated:
                continue
            changes.append(EffectChange(before.category, before.identity, "removed", before, None))
        elif before is not None and after is not None:
            # A partial trace can undercount queries or miss a stronger network
            # outcome, so it cannot establish a comparative change.
            if base.truncated or head.truncated:
                continue
            changed = (before.category == "db_query" and before.count != after.count) or before.outcome != after.outcome
            if changed:
                changes.append(EffectChange(after.category, after.identity, "changed", before, after))
            else:
                unchanged += 1
    reasons: list[str] = []
    if base.truncated:
        reasons.append("base observation was truncated; additions and count/outcome changes are not claimed")
    if head.truncated:
        reasons.append(
            "head observation was truncated; removals are not claimed; count/outcome changes are not claimed"
        )
    return ObservationDiff(
        comparable=True,
        reasons=tuple(reasons),
        changes=tuple(changes),
        unchanged=unchanged,
        query_count=(base.query_count, head.query_count),
        query_count_comparable=not (base.truncated or head.truncated),
    )


# --------------------------------------------------------------------------- #
# behavior producer
# --------------------------------------------------------------------------- #

_SEVERITY: Final[Mapping[tuple[str, str], BehaviorSeverity]] = {
    ("network", "added"): "high",
    ("network", "changed"): "high",
    ("schema_mutation", "added"): "high",
    ("schema_mutation", "removed"): "medium",
    ("process", "added"): "medium",
    ("file_write", "added"): "medium",
    ("console_error", "added"): "medium",
    ("db_query", "added"): "medium",
    ("db_query", "changed"): "medium",
}
_EXECUTORS: Final[frozenset[str]] = frozenset({"author_machine", "customer_worker", "managed_sandbox"})


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _effect_summary(effect: ObservedEffect) -> str:
    count = f" (x{effect.count})" if effect.count > 1 else ""
    outcome = f" ({effect.outcome})" if effect.outcome else ""
    return f"{effect.identity}{count}{outcome}"


def _effect_observation(side: BehaviorSide, effect: ObservedEffect | None, ref: str) -> BehaviorObservation | None:
    if effect is None:
        return None
    identity = {"category": effect.category, "identity": effect.identity, "outcome": effect.outcome}
    if effect.category == "db_query":
        identity["count"] = str(effect.count)
    return BehaviorObservation(side, "observed", _digest(identity), _effect_summary(effect), ref)


def change_summary(change: EffectChange) -> str:
    label = CATEGORY_LABELS[change.category].lower()
    if change.change == "added" and change.head is not None:
        extra = {
            "blocked": "; blocked by the sandbox and recorded",
            "intercepted": "; intercepted by a recording sink, not delivered",
        }.get(change.head.outcome, "")
        return f"Observed on head, not observed in base run: {label} {change.identity}{extra}."
    if change.change == "removed":
        return f"Observed on base, not observed in head run: {change.identity}."
    if change.category == "db_query" and change.base is not None and change.head is not None:
        return f"Query count changed from {change.base.count} to {change.head.count}: {change.identity}."
    before = change.base.outcome if change.base is not None else ""
    after = change.head.outcome if change.head is not None else ""
    return f"Outcome changed from {before or 'none'} to {after or 'none'}: {change.identity}."


def _change_from_wire(raw: Mapping[str, Any]) -> EffectChange | None:
    category = str(raw.get("category") or "")
    kind = str(raw.get("change") or "")
    if category not in OBSERVATION_CATEGORIES or kind not in {"added", "removed", "changed"}:
        return None
    base_raw, head_raw = raw.get("base"), raw.get("head")
    try:
        base = ObservedEffect.from_wire(base_raw) if isinstance(base_raw, Mapping) else None
        head = ObservedEffect.from_wire(head_raw) if isinstance(head_raw, Mapping) else None
    except (TypeError, ValueError):
        return None
    if base is None and head is None:
        return None
    return EffectChange(
        category,
        str(raw.get("identity") or "")[:_MAX_IDENTITY],
        cast(EffectChangeKind, kind),
        base,
        head,
    )


class RuntimeObservationProducer:
    """``side_effect`` behavior targets from persisted runtime-observation diff records.

    Reads only what a planned run persisted for this exact Snapshot (newest
    record per recipe); a record whose bytes do not match their digest is
    reported as degraded, a not-comparable pair yields nothing.
    """

    producer_id = RUNTIME_OBSERVATION_PRODUCER_ID
    contract_version = BEHAVIOR_CONTRACT_VERSION

    def produce(self, context: BehaviorContext) -> BehaviorProduction:
        newest: dict[str, tuple[ReviewEvidence, Mapping[str, Any]]] = {}
        degraded: list[str] = []
        rows = sorted(
            (
                item
                for item in context.evidence
                if item.mime_type == RUNTIME_OBSERVATION_MEDIA_TYPE
                and item.revision_id == context.revision.id
                and item.execution_intent != "explicit_historical"
            ),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )
        for item in rows:
            record = read_record(context.read_artifact(item), item.content_hash)
            if record is None:
                degraded.append(f"runtime observation {item.id} is missing or does not match its digest")
                continue
            recipe_id = str(_mapping(record.get("recipe")).get("id") or "")
            if record.get("kind") == "diff" and recipe_id:
                newest.setdefault(recipe_id, (item, record))
        deltas: list[BehaviorDelta] = []
        executed: list[ExecutedScope] = []
        unchanged = 0
        for recipe_id in sorted(newest):
            item, found = newest[recipe_id]
            production = self._recipe(item, found, recipe_id)
            deltas.extend(production.deltas)
            executed.extend(production.executed)
            unchanged += production.unchanged
            degraded.extend(production.degraded)
        return BehaviorProduction(
            deltas=tuple(deltas), executed=tuple(executed), unchanged=unchanged, degraded=tuple(degraded)
        )

    def _recipe(self, item: ReviewEvidence, record: Mapping[str, Any], recipe_id: str) -> BehaviorProduction:
        sides = _mapping(record.get("sides"))
        refs: dict[BehaviorSide, str] = {}
        side: BehaviorSide
        for side in ("base", "head"):
            ref = str(_mapping(sides.get(side)).get("evidence_ref") or "")
            if ref:
                refs[side] = ref
        executed = tuple(
            ExecutedScope(producer=self.producer_id, run_ref=ref, side=side, target_ids=(RUNTIME_OBSERVATION_ENTRY,))
            for side, ref in sorted(refs.items())
        )
        diff = _mapping(record.get("diff"))
        if diff.get("comparable") is not True:
            return BehaviorProduction(
                executed=executed,
                degraded=(
                    f"runtime observation incomplete: {recipe_id} has non-comparable runs; re-run the same recipe/environment",
                ),
            )
        environment = str(_mapping(record.get("environment")).get("head") or "")
        executor_raw = str(record.get("executor_class") or "")
        executor = cast(ExecutorClass, executor_raw if executor_raw in _EXECUTORS else "")
        title = str(_mapping(record.get("recipe")).get("title") or recipe_id)[:120]
        evidence: list[BehaviorEvidenceRef] = [
            BehaviorEvidenceRef(
                ref=f"artifact:{item.id}",
                kind="artifact",
                label=f"{title} · runtime side effects",
                digest=item.content_hash,
                role="record",
                evidence_id=item.id,
                media_type=item.mime_type,
            )
        ]
        evidence.extend(
            BehaviorEvidenceRef(ref=ref, kind="check", label=f"{title} · {side} run", side=side)
            for side, ref in sorted(refs.items())
        )
        template = BehaviorDelta(
            kind="side_effect",
            producer=self.producer_id,
            subject="",
            title="",
            summary="",
            evidence=tuple(evidence),
            environment_fingerprint=environment,
            executor_class=executor,
            target_ids=(RUNTIME_OBSERVATION_ENTRY,),
        )
        deltas: list[BehaviorDelta] = []
        # v1 diff records lacked direction-aware truncation/complete-count
        # evidence. If such a record reported truncation, even its precomputed
        # 'added' edges may be artifacts of missing base observations. Treat
        # the comparison as unproven rather than resurrecting a false claim.
        legacy_truncated = "query_count_comparable" not in diff and any(
            "truncat" in str(reason).lower() for reason in diff.get("reasons", ())
        )
        changes = [] if legacy_truncated else diff.get("changes")
        for raw in changes if isinstance(changes, list) else []:
            change = _change_from_wire(_mapping(raw))
            if change is None:
                continue
            label = CATEGORY_LABELS[change.category]
            facts: list[tuple[str, str]] = [
                ("Observed", "runtime (sandboxed run)"),
                ("Category", label),
                ("Recipe", recipe_id),
            ]
            shown = change.head or change.base
            if shown is not None and shown.outcome:
                facts.append(("Outcome", shown.outcome))
            if change.category == "db_query" and change.base is not None and change.head is not None:
                facts.append(("Count", f"{change.base.count} -> {change.head.count}"))
            deltas.append(
                replace(
                    template,
                    subject=f"runtime:{change.category}:{change.identity}",
                    title=f"Runtime {label.lower()} · {change.identity}",
                    summary=change_summary(change),
                    base=_effect_observation("base", change.base, refs.get("base", "")),
                    head=_effect_observation("head", change.head, refs.get("head", "")),
                    change=change.change,
                    severity=_SEVERITY.get((change.category, change.change), "low"),
                    facts=tuple(facts),
                )
            )
        query = _mapping(diff.get("query_count"))
        before, after = int(query.get("base") or 0), int(query.get("head") or 0)
        if before != after and diff.get("query_count_comparable") is True:
            deltas.append(
                replace(
                    template,
                    subject=f"runtime:db_query_count:{recipe_id}",
                    change="changed",
                    title=f"Runtime database query count · {before} -> {after}",
                    summary=f"{title} issued {after} SQL queries on head and {before} on base.",
                    base=BehaviorObservation(
                        "base", "observed", _digest(["queries", before]), str(before), refs.get("base", "")
                    ),
                    head=BehaviorObservation(
                        "head", "observed", _digest(["queries", after]), str(after), refs.get("head", "")
                    ),
                    severity="medium" if after > before else "low",
                    facts=(
                        ("Observed", "runtime (sandboxed run)"),
                        ("Category", "Database query count"),
                        ("Recipe", recipe_id),
                    ),
                )
            )
        incomplete = diff.get("query_count_comparable") is not True
        return BehaviorProduction(
            deltas=tuple(deltas),
            executed=executed,
            unchanged=max(0, int(diff.get("unchanged") or 0)),
            degraded=(
                (
                    f"runtime observation incomplete: {recipe_id} has truncated or legacy comparison evidence; re-run to verify",
                )
                if incomplete
                else ()
            ),
        )


def read_record(payload: bytes | None, content_hash: str) -> dict[str, Any] | None:
    """A stored JSON record whose bytes match their evidence digest, else ``None``."""

    if payload is None or not content_hash or hashlib.sha256(payload).hexdigest() != content_hash:
        return None
    try:
        decoded = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


__all__ = [
    "CATEGORY_LABELS",
    "LIMITATIONS",
    "OBSERVATION_CATEGORIES",
    "OBSERVE_ASSETS_MOUNT",
    "OBSERVE_OUT_MOUNT",
    "PYTHON_OBSERVER",
    "RUNTIME_OBSERVATION_ENTRY",
    "RUNTIME_OBSERVATION_MEDIA_TYPE",
    "RUNTIME_OBSERVATION_PRODUCER_ID",
    "EffectChange",
    "ObservationDiff",
    "ObservationSetup",
    "ObservationSpec",
    "ObservedEffect",
    "RuntimeObservation",
    "RuntimeObservationProducer",
    "TraceEdge",
    "build_observation",
    "collect_raw",
    "diff_observations",
    "normalize_path",
    "normalize_sql",
    "parse_strace",
    "process_identity",
    "read_record",
    "sandbox_strace",
    "scan_output_errors",
    "workspace_change_rows",
]
