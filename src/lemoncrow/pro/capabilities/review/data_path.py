"""Review's experimental data path: request-data flows Opengrep finds on base and head, and how they changed (P10-7).

The producer runs the unmodified Opengrep v2.0.0 release (cross-file taint) as a separate process on a private copy of
the Snapshot's base tree and of its head tree, with LemonCrow's own taint rules (:data:`RULES_DIR`), and reports the
flows that were added, removed and rerouted. It is experimental and says so in every report (:data:`DISCLOSURE`): it
shows only flows it found, never that a change has none.

* **Key.** A flow is keyed on (source symbol, sink symbol): the function that reads the request data plus the read,
  and the function that writes plus the callee. The ordered function path between them is detail. Head only is
  ``added``, base only is ``removed``, the same key with another path is ``rerouted``. Lines are never in the key, so
  code that only moved is unchanged. Each step is ``path:line`` in its own side's tree.
* **Bounds.** :class:`Bounds` caps the files, bytes, time, memory, engine output, flows and steps. A cap that is hit
  is named in ``caps`` and whatever it left out is in ``not_measured``; nothing is dropped silently. A framework
  whose base or head run did not finish is not diffed at all (half a diff would read as flows added or removed).
* **Partial.** A file one side did not read or the engine did not scan (a cap, its bytes, a link, its name, an
  engine error) is named, and a flow through a file the other side did not measure is neither added nor removed:
  its key is left out of the diff and counted. Any such gap makes the report ``partial`` and its summary says so.
* **Honesty.** The report carries the engine version and digest, the rules digest, the languages and frameworks it
  covered, everything it did not measure with the reason, and :data:`DISCLOSURE`. An empty diff is
  :data:`NO_FLOWS` with its coverage, never "no flow changes".

Trust boundaries (each crossing is narrowed here, see the hand-back of P10-7 for the list):

1. *The binary.* Found locally only (:func:`locate_engine`: ``LEMONCROW_DATA_PATH_OPENGREP_BIN``, then the security
   scan's ``LEMONCROW_OPENGREP_BIN``, then ``opengrep`` on ``PATH``); never downloaded during a review. It is copied
   into a private scratch directory while being hashed, and only that copy runs, and only when its SHA-256 equals
   :data:`~lemoncrow.pro.capabilities.review.pr_bot.opengrep_install.DATA_PATH_PINS` for this platform.
2. *Repository content into the engine.* Only regular files of the covered languages are copied from the Snapshot's
   side tree (never the working tree): no symlink, no path outside the tree, no vendored/generated path, no name
   with control or markup characters, no NUL byte, bounded in count and size. Rules come from this package only;
   ``nosem`` comments are ignored. The files derived from repository content are the flattened ``tsconfig.json``,
   written by :func:`flatten_tsconfig` from validated alias strings and in-tree target paths (each tsconfig read
   once, at most ``Bounds.tsconfigs`` per side), and an empty ``.semgrepignore`` so the engine skips nothing staged.
3. *The engine's process.* Offline: on macOS inside ``sandbox-exec`` (no network, reads only the scratch directory
   and system paths, writes only its scratch home); on Linux inside an offline bubblewrap namespace. Without one of
   them it does not run (``not_available`` with the setup line): never unsandboxed. Always under a wall-clock and a
   memory ceiling, its own process group, no stdin, and the environment allow-list.
4. *Engine output into the Review.* The JSON is size-capped, opened without following a link or blocking, and
   parsed as data. Paths must be staged files and lines within them; source and callee text is kept only when it is
   identifier-like (else a digest stands in), function names only when they are plain names, argument text is
   dropped, and ``extra.lines`` is never read. Everything after the engine is charged to ``Bounds.total_seconds``.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import platform
import posixpath
import re
import shutil
import stat
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any, Final

from lemoncrow.pro.capabilities.review.pr_bot import opengrep_install
from lemoncrow.pro.capabilities.review.pr_bot.security import (
    _SKIP,
    Runner,
    _safe_relative,
    resolve_binary,
    run_process,
    scanner_env,
)
from lemoncrow.pro.capabilities.review.pr_bot.security import (
    BIN_ENV as SCAN_BIN_ENV,
)

ENTRY: Final[str] = "data_path.diff"
DERIVED_KIND: Final[str] = "data_path"
SCHEMA_VERSION: Final[int] = 1
RULES_DIR: Final[Path] = Path(__file__).resolve().parent / "data_path_rules"
#: Names this producer's binary; checked before the security scan's variable and ``PATH``.
BIN_ENV: Final[str] = "LEMONCROW_DATA_PATH_OPENGREP_BIN"
ENGINE_VERSION: Final[str] = opengrep_install.DATA_PATH_VERSION
KINDS: Final[tuple[str, ...]] = ("db", "http", "file", "log")
CHANGE_KINDS: Final[tuple[str, ...]] = ("added", "removed", "rerouted")
#: Shown with every report, whatever it found.
DISCLOSURE: Final[str] = (
    "Experimental: shows only the flows Opengrep's taint analysis found with LemonCrow's rules. It finds a minority "
    "of flows (about 17 % of hand-traced flows in the spike), so a flow that is not shown may still have changed."
)
#: The summary of an empty diff. Never "no flow changes": only what was found is ever reported.
NO_FLOWS: Final[str] = "No flows found"
NO_FLOWS_DETAIL: Final[str] = "Opengrep found no added, removed or rerouted flow in what it covered."


@dataclass(frozen=True, slots=True)
class Framework:
    id: str
    title: str
    language: str
    suffixes: tuple[str, ...]
    #: ``--interfile-depth``: TypeScript resolves more and runs out of memory past 2 (the spike); Python and Java at 4.
    depth: int
    #: Found in a file's text when the framework is in use on that side.
    marker: re.Pattern[str]
    rules: str
    #: Known gaps of the rules, stated in every report that covers the framework.
    limits: tuple[str, ...] = ()


FRAMEWORKS: Final[tuple[Framework, ...]] = (
    Framework(
        "django",
        "Django and Django REST framework",
        "Python",
        (".py",),
        4,
        re.compile(r"^[ \t]*(?:from|import)[ \t]+(?:django|rest_framework)\b", re.MULTILINE),
        "django.yaml",
        ("generic views that write without a handler method of their own are not followed",),
    ),
    Framework(
        "express",
        "Express",
        "TypeScript and JavaScript",
        (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
        2,
        re.compile(r"""(?:\bfrom[ \t]+|\brequire\([ \t]*)["']express["']"""),
        "express.yaml",
        ("calls deeper than 2 are not followed (TypeScript and JavaScript run at depth 2)",),
    ),
    Framework(
        "spring",
        "Spring",
        "Java",
        (".java",),
        4,
        re.compile(r"^[ \t]*import[ \t]+(?:static[ \t]+)?org\.springframework\.", re.MULTILINE),
        "spring.yaml",
        ("application events and aspects are not followed",),
    ),
)
_BY_ID: Final[Mapping[str, Framework]] = {item.id: item for item in FRAMEWORKS}
_COVERED_SUFFIXES: Final[frozenset[str]] = frozenset(suffix for item in FRAMEWORKS for suffix in item.suffixes)
#: Source languages with no rules here, named when the change touches them.
_OTHER_LANGUAGES: Final[Mapping[str, str]] = {
    ".go": "Go",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".kt": "Kotlin",
    ".kts": "Kotlin",
    ".php": "PHP",
    ".cs": "C#",
    ".scala": "Scala",
    ".swift": "Swift",
    ".c": "C",
    ".h": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".hpp": "C++",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".dart": "Dart",
    ".groovy": "Groovy",
    ".vue": "Vue",
    ".svelte": "Svelte",
}
_TSCONFIG_BYTES: Final[int] = 256 * 1024
_EXTENDS_DEPTH: Final[int] = 8
#: Entries of one ``extends`` list followed; the rest are named.
_EXTENDS_LIST: Final[int] = 16
#: tsconfig notes listed per side; the rest are counted.
_NOTES: Final[int] = 20
#: Callee text longer than this is a digest: emptying argument lists costs its length times its nesting.
_CALLEE_INPUT: Final[int] = 1_024
_SIDES: Final[tuple[tuple[str, str], ...]] = (("old", "base"), ("new", "head"))
_SCRATCH_PREFIX: Final[str] = "lc-data-path-"
#: A scratch directory of this producer untouched this long was left by a run that died (a killed server); the next
#: run removes it. A live run touches its directory at every engine run, and one run is capped at ``total_seconds``.
_STALE_SCRATCH_SECONDS: Final[int] = 6 * 3600
#: What a stored path or name may not hold: C0 and C1 controls, markup and quotes, bidirectional and other format
#: controls, and lone surrogates (an undecodable file name). A path with any of them is not staged and is named by
#: its digest; a name with any of them is a digest.
_UNSAFE_TEXT: Final[re.Pattern[str]] = re.compile(
    "[\x00-\x1f\x7f-\x9f<>&\"'`\\\\\u00ad\u061c\u180e\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff\ufff9-\ufffb"
    "\ud800-\udfff]"
)


# lc-debt: fixed bounds sized from the spike (Grist, 1,900 TypeScript files: 38 to 72 s and 600 MiB at depth 2);
# past them a framework or a file is not measured. Upgrade: scan only the changed files' import closure.
@dataclass(frozen=True, slots=True)
class Bounds:
    """How far one run may go; each one hit is named in the report."""

    #: Source files copied per side (first by path) and the bytes of each and of all.
    files: int = 4_000
    file_bytes: int = 1_000_000
    total_bytes: int = 48 << 20
    #: Wall clock of one engine run (also ``--interfile-timeout``) and of the whole producer, seconds.
    run_seconds: int = 300
    total_seconds: int = 900
    #: Memory of one engine run, MiB (the process ceiling and ``--max-memory``).
    memory_mb: int = 4096
    #: The engine's JSON document and the results read from it, per run.
    json_bytes: int = 32 << 20
    results: int = 5_000
    #: Flows shown per kind of change (the rest are counted) and steps shown per flow.
    flows_per_kind: int = 50
    steps: int = 32
    #: tsconfig files read per side to flatten path aliases: each root ``tsconfig.json`` and every file its
    #: ``extends`` names, each counted once.
    tsconfigs: int = 20


DEFAULT_BOUNDS: Final[Bounds] = Bounds()


# --------------------------------------------------------------------------- #
# rules
# --------------------------------------------------------------------------- #


def rule_files() -> tuple[Path, ...]:
    return tuple(RULES_DIR / item.rules for item in FRAMEWORKS)


@lru_cache(maxsize=1)
def rules_digest() -> str:
    """SHA-256 over every rule file's name and bytes: a stored report from other rules is not current."""

    digest = hashlib.sha256()
    for path in rule_files():
        digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def stamp() -> str:
    return f"data-path-v{SCHEMA_VERSION}.{ENGINE_VERSION}.{rules_digest()[:16]}"


def _rule_kind(check_id: str, framework: str) -> str:
    prefix = f"lemoncrow.data-path.{framework}."
    kind = check_id[len(prefix) :] if check_id.startswith(prefix) else ""
    return kind if kind in KINDS else ""


# --------------------------------------------------------------------------- #
# the engine: found locally, verified by digest, never downloaded
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Engine:
    """The verified private copy that runs."""

    path: Path
    sha256: str
    asset: str
    version: str = ENGINE_VERSION


@dataclass(frozen=True, slots=True)
class Isolation:
    """How the engine's process is contained: ``sandbox-exec`` or ``bubblewrap``; ``none`` refuses the run."""

    mode: str
    note: str
    #: What to install so the engine can run contained (``none`` only).
    setup: str = ""


#: The containments the engine runs in; anything else is ``not_available``.
_CONTAINED: Final[frozenset[str]] = frozenset({"sandbox-exec", "bubblewrap"})
_NEVER_UNSANDBOXED: Final[str] = "the data path never runs the engine unsandboxed"
_BUBBLEWRAP_SETUP: Final[str] = (
    "bubblewrap required: install it (sudo apt-get install bubblewrap on Debian and Ubuntu, sudo dnf install "
    "bubblewrap on Fedora), allow unprivileged user namespaces, and run the data path again."
)
_SANDBOX_EXEC_SETUP: Final[str] = (
    "sandbox-exec required: it ships with macOS as /usr/bin/sandbox-exec; restore it and run the data path again."
)


def _never() -> bool:
    return False


def setup_hint(system: str, machine: str) -> str:
    """What to do so a local review can run the data path on this platform."""

    pin, why = opengrep_install.pin_for(system, machine, pins=opengrep_install.DATA_PATH_PINS)
    if pin is None:
        return f"The data path cannot run here: {why}."
    url = opengrep_install.RELEASE_URL.format(version=ENGINE_VERSION, asset=pin.asset)
    return (
        f"Download {url}, check that its SHA-256 is {pin.sha256}, make it executable, and set {BIN_ENV} to its "
        "path (or put it on PATH as opengrep). A review never downloads it."
    )


def _copy_hashed(source: Path, target: Path, *, limit: int) -> str:
    """Copy *source* to a new *target* (owner-only) and return the SHA-256 of the bytes copied; ``""`` past *limit*."""

    digest = hashlib.sha256()
    written = 0
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with source.open("rb") as reader:
        handle = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(handle, "wb") as writer:
            for chunk in iter(lambda: reader.read(1024 * 1024), b""):
                written += len(chunk)
                if written > limit:
                    return ""
                digest.update(chunk)
                writer.write(chunk)
    return digest.hexdigest()


def locate_engine(environ: Mapping[str, str], scratch: Path, *, system: str, machine: str) -> tuple[Engine | None, str]:
    """The pinned Opengrep v2.0.0 as a verified private copy under *scratch*, or ``(None, why not)``.

    The route a local ``lc review`` uses for the security scan (an environment variable, then ``PATH``), with this
    producer's own variable first. Nothing is downloaded.
    """

    pin, why = opengrep_install.pin_for(system, machine, pins=opengrep_install.DATA_PATH_PINS)
    if pin is None:
        return None, why
    named = str(environ.get(BIN_ENV) or "").strip()
    if named:
        origin = BIN_ENV
        if not (os.path.isfile(named) and os.access(named, os.X_OK)):
            return None, f"{BIN_ENV} does not name an executable file"
        candidate = named
    else:
        origin = SCAN_BIN_ENV if str(environ.get(SCAN_BIN_ENV) or "").strip() else "PATH"
        candidate, _why = resolve_binary(environ)
        if not candidate:
            # The reason names no local path: the report is stored and served with the Review.
            missing = f"{SCAN_BIN_ENV} does not name an executable file" if origin != "PATH" else "no opengrep on PATH"
            return None, f"Opengrep {ENGINE_VERSION} is not installed ({missing})"
    target = scratch / "engine" / "opengrep"
    try:
        digest = _copy_hashed(Path(candidate), target, limit=opengrep_install.MAX_BYTES)
    except OSError as exc:
        return None, f"the Opengrep named by {origin} could not be read ({type(exc).__name__})"
    if digest != pin.sha256:
        return None, (
            f"the Opengrep found through {origin} is not the pinned {ENGINE_VERSION} release ({pin.asset}): "
            "its SHA-256 differs"
        )
    target.chmod(0o500)
    home = scratch / "smoke-home"
    home.mkdir(mode=0o700, exist_ok=True)
    problem = opengrep_install.smoke_test(target, _engine_env(environ, home), version=ENGINE_VERSION)
    if problem:
        return None, problem
    return Engine(path=target, sha256=digest, asset=pin.asset), ""


def _engine_env(environ: Mapping[str, str], home: Path) -> dict[str, str]:
    """The security scan's allow-list, with every home and temporary directory moved into the scratch directory."""

    env = scanner_env(environ)
    for key in ("HOME", "TMPDIR", "TEMP", "TMP"):
        env[key] = str(home)
    env.setdefault("PATH", "/usr/bin:/bin")
    return env


@lru_cache(maxsize=1)
def _probe_bubblewrap() -> str:
    """``""`` when an offline bubblewrap namespace starts here, else why not (once per process).

    The check ``proof.run.probe_bubblewrap`` makes, here because the Review engine does not ship ``proof``.
    """

    import subprocess

    from lemoncrow.pro.capabilities.review.hermetic import HermeticUnavailable, offline_bwrap_prefix

    try:
        command = [*offline_bwrap_prefix(), "--", "/bin/true"]
    except HermeticUnavailable:
        return "bubblewrap is not installed"
    try:
        done = subprocess.run(
            command, capture_output=True, text=True, timeout=20, check=False, env={"PATH": "/usr/bin:/bin"}
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"bubblewrap could not start ({type(exc).__name__})"
    return "" if done.returncode == 0 else f"bubblewrap does not work here (exit {done.returncode})"


def detect_isolation(platform_name: str | None = None) -> Isolation:
    """The containment this host offers the engine; ``none`` (with its setup line) when it offers none.

    The stance of Review's hermetic stages: without ``sandbox-exec`` on macOS or a working bubblewrap on Linux the
    engine does not run at all.
    """

    name = platform_name or sys.platform
    if name == "darwin":
        from lemoncrow.pro.environment.host_tools import host_executable

        if host_executable("sandbox-exec"):
            return Isolation(
                "sandbox-exec", "macOS sandbox: no network, reads only its copy, writes only its scratch home"
            )
        return Isolation("none", f"sandbox-exec is missing, and {_NEVER_UNSANDBOXED}", _SANDBOX_EXEC_SETUP)
    if name.startswith("linux"):
        problem = _probe_bubblewrap()
        if not problem:
            return Isolation("bubblewrap", "offline bubblewrap namespace: no network, its copy mounted read-only")
        return Isolation("none", f"{problem}, and {_NEVER_UNSANDBOXED}", _BUBBLEWRAP_SETUP)
    return Isolation(
        "none",
        f"there is no sandbox for the engine on this platform, and {_NEVER_UNSANDBOXED}",
        "Run the data path on macOS (sandbox-exec) or on Linux with bubblewrap.",
    )


# --------------------------------------------------------------------------- #
# staging a side: covered source files only, bounded, never a link
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class Staged:
    """One side copied for the engine."""

    root: Path
    files: dict[str, int] = field(default_factory=dict)
    #: Lines of each staged file: an engine location past a file's last line is dropped.
    lines: dict[str, int] = field(default_factory=dict)
    detected: set[str] = field(default_factory=set)
    #: Staged files per framework (by suffix).
    counts: dict[str, int] = field(default_factory=dict)
    over_files: int = 0
    over_file_bytes: list[str] = field(default_factory=list)
    over_total_bytes: int = 0
    #: Covered source files this side has but did not stage, with why (a cap, its bytes, a link, its name): a flow
    #: through one of them on the other side is not diffed.
    excluded: dict[str, str] = field(default_factory=dict)
    #: Folders this side has but did not read (``path/``), with why: a link, or a name with control characters.
    excluded_dirs: dict[str, str] = field(default_factory=dict)
    #: Vendored, generated and minified paths (``_SKIP``), skipped alike on both sides; named, never read.
    skipped: list[str] = field(default_factory=list)
    tsconfigs: list[str] = field(default_factory=list)
    over_tsconfigs: int = 0
    tsconfig_notes: list[str] = field(default_factory=list)
    tsconfig_written: bool = False
    notes: list[str] = field(default_factory=list)
    #: The whole time cap ran out while this side was copied: it is incomplete and nothing is diffed.
    timed_out: bool = False


def _framework_of(path: str) -> tuple[Framework, ...]:
    suffix = PurePosixPath(path).suffix.lower()
    return tuple(item for item in FRAMEWORKS if suffix in item.suffixes)


def _covered(path: str) -> bool:
    return PurePosixPath(path).suffix.lower() in _COVERED_SUFFIXES


def _path_text(rel: str) -> str:
    """A repository path as it may be stored and shown: itself when it holds no unsafe character, else a digest."""

    return rel if rel and len(rel) <= 400 and not _UNSAFE_TEXT.search(rel) else _digest_text("path", rel)


def _names(rels: Sequence[str], limit: int = 5) -> str:
    shown = ", ".join(_path_text(rel) for rel in rels[:limit])
    return shown + (f" and {len(rels) - limit} more" if len(rels) > limit else "")


_LINK: Final[str] = "a symbolic link: not followed"
_UNSAFE_NAME: Final[str] = "the name holds control or markup characters"


@dataclass(slots=True)
class _Listing:
    files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    #: Excluded paths (folders end with ``/``) and why.
    excluded: dict[str, str] = field(default_factory=dict)
    timed_out: bool = False


def _walk(tree: Path, expired: Callable[[], bool]) -> _Listing:
    """The regular files under *tree*, sorted, and every path left out with why; links are never followed."""

    listing = _Listing()
    for top, dirnames, filenames in os.walk(tree, followlinks=False):
        if expired():
            listing.timed_out = True
            break
        rel_top = Path(top).relative_to(tree).as_posix()
        prefix = "" if rel_top == "." else f"{rel_top}/"
        keep: list[str] = []
        for name in sorted(dirnames):
            rel = f"{prefix}{name}/"
            if _UNSAFE_TEXT.search(name):
                listing.excluded[rel] = _UNSAFE_NAME
            elif _SKIP.search(rel):
                listing.skipped.append(rel)
            elif os.path.islink(os.path.join(top, name)):
                listing.excluded[rel] = _LINK
            else:
                keep.append(name)
        dirnames[:] = keep
        for name in filenames:
            rel = f"{prefix}{name}"
            covered = _covered(rel) or name == "tsconfig.json"
            if _UNSAFE_TEXT.search(name) or not _safe_relative(rel):
                if covered:
                    listing.excluded[rel] = _UNSAFE_NAME
                continue
            if _SKIP.search(rel):
                if covered:
                    listing.skipped.append(rel)
                continue
            try:
                mode = os.lstat(os.path.join(top, name)).st_mode
            except OSError:
                if covered:
                    listing.excluded[rel] = "could not be read"
                continue
            if stat.S_ISREG(mode):
                listing.files.append(rel)
            elif covered:
                listing.excluded[rel] = _LINK if stat.S_ISLNK(mode) else "not a regular file"
    listing.files.sort()
    listing.skipped.sort()
    return listing


def _read_regular(path: Path, limit: int) -> bytes | None:
    """The bytes of a regular file, never through a link nor blocking on a pipe, or ``None`` past *limit*."""

    try:
        handle = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None
    with os.fdopen(handle, "rb") as reader:
        if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
            return None
        data = reader.read(limit + 1)
    return None if len(data) > limit else data


def _write_new(path: Path, data: bytes) -> bool:
    """Write *data* to a new owner-only file; ``False`` when anything is already there (a staged folder, say)."""

    try:
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        return False
    with os.fdopen(handle, "wb") as writer:
        writer.write(data)
    return True


def _is_tsconfig(path: str) -> bool:
    return PurePosixPath(path).name == "tsconfig.json"


def _line_count(data: bytes) -> int:
    return data.count(b"\n") + (1 if data and not data.endswith(b"\n") else 0)


def stage_side(tree: Path, target: Path, bounds: Bounds, *, expired: Callable[[], bool] = _never) -> Staged:
    """Copy the covered source files of one side's tree into *target* and flatten its tsconfig there.

    Everything left out is recorded with why; *expired* (the whole time cap) is checked as it goes.
    """

    staged = Staged(root=target)
    target.mkdir(mode=0o700, parents=True)
    listing = _walk(tree, expired)
    staged.timed_out = listing.timed_out
    staged.skipped = listing.skipped
    for rel, reason in listing.excluded.items():
        (staged.excluded_dirs if rel.endswith("/") else staged.excluded)[rel] = reason
    total = 0
    for rel in listing.files:
        frameworks = _framework_of(rel)
        if not frameworks:
            continue
        if expired():
            staged.timed_out = True
            break
        try:
            size = os.lstat(tree / rel).st_size
        except OSError:
            staged.excluded[rel] = "could not be read"
            continue
        if size > bounds.file_bytes:
            staged.over_file_bytes.append(rel)
            staged.excluded[rel] = "cap file_bytes hit"
            continue
        if len(staged.files) >= bounds.files:
            staged.over_files += 1
            staged.excluded[rel] = "cap files hit"
            continue
        if total + size > bounds.total_bytes:
            staged.over_total_bytes += 1
            staged.excluded[rel] = "cap total_bytes hit"
            continue
        data = _read_regular(tree / rel, bounds.file_bytes)
        if data is None:
            staged.excluded[rel] = "could not be read"
            continue
        if b"\0" in data:
            staged.excluded[rel] = "contains a NUL byte"
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            staged.excluded[rel] = "not UTF-8 text"
            continue
        destination = target / rel
        try:
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            written = _write_new(destination, data)
        except OSError:
            written = False
        if not written:  # a path that clashes with one already staged (a case-insensitive disk, say)
            staged.excluded[rel] = "could not be staged"
            continue
        total += len(data)
        staged.files[rel] = len(data)
        staged.lines[rel] = _line_count(data)
        for item in frameworks:
            staged.counts[item.id] = staged.counts.get(item.id, 0) + 1
            if item.marker.search(text):
                staged.detected.add(item.id)
    # Empty: the engine's built-in ignore list (tests/, build/ ...) would skip staged files without saying so.
    if not _write_new(target / ".semgrepignore", b""):
        staged.notes.append(
            "a staged folder is named .semgrepignore, so the engine applied its own ignore list (what it skipped is "
            "named below)"
        )
    configs = sorted((rel for rel in listing.files if _is_tsconfig(rel)), key=lambda rel: (rel.count("/"), rel))
    if configs and not staged.timed_out:
        flattened, notes, read = _flatten(
            lambda rel: _tsconfig_text(tree, rel), configs, budget=bounds.tsconfigs, expired=expired
        )
        staged.tsconfigs = [rel for rel in configs if rel in read.parsed]
        staged.over_tsconfigs = len(read.refused)
        staged.timed_out = staged.timed_out or read.timed_out
        staged.tsconfig_notes = notes
        text = json.dumps(flattened, indent=2, sort_keys=True) + "\n"
        staged.tsconfig_written = _write_new(target / "tsconfig.json", text.encode("utf-8"))
        if not staged.tsconfig_written:
            staged.tsconfig_notes.append("a staged folder is named tsconfig.json: the flattened aliases were not used")
    return staged


def _tsconfig_text(tree: Path, rel: str) -> str | None:
    if not _safe_relative(rel) or _UNSAFE_TEXT.search(rel):
        return None
    data = _read_regular(tree / rel, _TSCONFIG_BYTES)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


# --------------------------------------------------------------------------- #
# tsconfig: one root file with the path aliases inline (Opengrep does not follow `extends`)
# --------------------------------------------------------------------------- #


def parse_jsonc(text: str) -> Any:
    """JSON with ``//`` and ``/* */`` comments and trailing commas, as tsconfig files are written."""

    out: list[str] = []
    index, size, in_string = 0, len(text), False
    while index < size:
        char = text[index]
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < size:
                out.append(text[index + 1])
                index += 2
                continue
            in_string = char != '"'
            index += 1
            continue
        if char == '"':
            in_string = True
        elif text.startswith("//", index):
            end = text.find("\n", index)
            index = size if end < 0 else end
            continue
        elif text.startswith("/*", index):
            end = text.find("*/", index + 2)
            index = size if end < 0 else end + 2
            continue
        elif char == ",":
            ahead = index + 1
            while ahead < size and text[ahead] in " \t\r\n":
                ahead += 1
            if ahead < size and text[ahead] in "}]":
                index += 1
                continue
        out.append(char)
        index += 1
    return json.loads("".join(out))


def _in_tree(base: str, rel: str) -> str | None:
    """*rel* joined to the root-relative directory *base*, normalised; ``None`` when it leaves the tree."""

    if not isinstance(rel, str) or not rel or "\0" in rel or "\\" in rel or rel.startswith("/"):
        return None
    joined = posixpath.normpath(posixpath.join(base or ".", rel))
    if joined == ".." or joined.startswith("../"):
        return None
    return "" if joined == "." else joined


_ALIAS: Final[re.Pattern[str]] = re.compile(r"^[\w@$~.*/-]{1,200}$")


@dataclass(slots=True)
class _Notes:
    """Notes about one side's tsconfig files: each once, at most :data:`_NOTES` listed (the rest counted)."""

    rows: set[str] = field(default_factory=set)

    def add(self, text: str) -> None:
        self.rows.add(text)

    def listed(self) -> list[str]:
        rows = sorted(self.rows)
        if len(rows) <= _NOTES:
            return rows
        return [*rows[:_NOTES], f"{len(rows) - _NOTES} more tsconfig notes not listed"]


@dataclass(slots=True)
class _Configs:
    """The tsconfig files of one side: each read and parsed once, at most *budget* of them, until *expired*."""

    read: Callable[[str], str | None]
    budget: int
    expired: Callable[[], bool] = _never
    parsed: dict[str, tuple[Any, str]] = field(default_factory=dict)
    #: Files not read because the budget was spent (``Bounds.tsconfigs``, named) or the time cap ran out.
    refused: set[str] = field(default_factory=set)
    timed_out: bool = False

    def get(self, rel: str) -> tuple[Any, str] | None:
        """``(document, problem)`` of *rel*, or ``None`` when it was not read (counted in :attr:`refused`)."""

        known = self.parsed.get(rel)
        if known is not None:
            return known
        if rel in self.refused:
            return None
        if self.expired():
            self.timed_out = True
            self.refused.add(rel)
            return None
        if len(self.parsed) >= self.budget:
            self.refused.add(rel)
            return None
        text = self.read(rel)
        result: tuple[Any, str]
        if text is None:
            result = (None, "could not be read")
        else:
            try:
                result = (parse_jsonc(text), "")
            except (ValueError, RecursionError):  # nesting deeper than the parser's stack is not valid JSON either
                result = (None, "is not valid JSON")
        self.parsed[rel] = result
        return result


def _chain(
    rel: str, configs: _Configs, notes: _Notes, visited: set[str], depth: int = 0
) -> tuple[str | None, dict[str, list[str]] | None, str]:
    """``(baseUrl, paths, directory paths resolve against)`` of one tsconfig after its ``extends``, root-relative.

    *visited* holds the files already followed for this root: a cycle or a file extended twice is followed once, so
    one root costs at most one visit per file. ``extends`` values are never quoted in a note (they are repository
    text); paths are, only when safe (:func:`_path_text`).
    """

    visited.add(rel)
    got = configs.get(rel)
    if got is None:
        return None, None, ""
    data, problem = got
    name = _path_text(rel)
    if problem:
        notes.add(f"{name} {problem}")
        return None, None, ""
    if not isinstance(data, dict):
        return None, None, ""
    here = posixpath.dirname(rel)
    base_url: str | None = None
    paths: dict[str, list[str]] | None = None
    paths_dir = ""
    raw_extends = data.get("extends")
    parents = [raw_extends] if isinstance(raw_extends, str) else raw_extends if isinstance(raw_extends, list) else []
    if len(parents) > _EXTENDS_LIST:
        notes.add(f"{name} extends more than {_EXTENDS_LIST} files: the rest were not followed")
        parents = parents[:_EXTENDS_LIST]
    for parent in parents:
        if not isinstance(parent, str) or not parent.startswith("."):
            notes.add(f"{name} extends a package: not followed")
            continue
        if depth >= _EXTENDS_DEPTH:
            notes.add(f"{name}: extends deeper than {_EXTENDS_DEPTH} files not followed")
            continue
        target = _in_tree(here, parent if parent.endswith(".json") else f"{parent}.json")
        if target is None:
            notes.add(f"{name} extends a file outside the repository: not followed")
            continue
        if target in visited:
            continue
        inherited = _chain(target, configs, notes, visited, depth + 1)
        base_url = inherited[0] if inherited[0] is not None else base_url
        if inherited[1] is not None:
            paths, paths_dir = inherited[1], inherited[2]
    options = data.get("compilerOptions")
    if isinstance(options, dict):
        if isinstance(options.get("baseUrl"), str):
            base_url = _in_tree(here, options["baseUrl"])
            if base_url is None:
                notes.add(f"{name}: baseUrl outside the repository ignored")
        raw_paths = options.get("paths")
        if isinstance(raw_paths, dict):
            paths = {
                str(alias): [str(item) for item in targets if isinstance(item, str)]
                for alias, targets in list(raw_paths.items())[:200]
                if isinstance(targets, list)
            }
            paths_dir = here
    return base_url, paths, paths_dir


def _flatten(
    read: Callable[[str], str | None],
    configs: Sequence[str],
    *,
    budget: int,
    expired: Callable[[], bool] = _never,
) -> tuple[dict[str, Any], list[str], _Configs]:
    notes = _Notes()
    files = _Configs(read, budget, expired)
    merged: dict[str, list[str]] = {}

    def add(alias: str, target: str) -> None:
        if not _ALIAS.fullmatch(alias) or not _ALIAS.fullmatch(target):
            notes.add("an alias or target with unexpected characters was dropped")
            return
        rows = merged.setdefault(alias, [])
        if target not in rows:
            rows.append(target)

    for rel in configs:
        base_url, paths, paths_dir = _chain(rel, files, notes, set())
        anchor = base_url if base_url is not None else paths_dir
        for alias, targets in (paths or {}).items():
            for target in targets:
                resolved = _in_tree(anchor, target)
                if resolved is None:
                    notes.add(f"{_path_text(rel)}: a path target outside the repository was dropped")
                    continue
                add(alias, resolved or "*")
        if base_url:  # a baseUrl below the root resolves bare imports from there
            add("*", f"{base_url}/*")
    options: dict[str, Any] = {"baseUrl": "."}
    if merged:
        options["paths"] = {alias: merged[alias] for alias in sorted(merged)}
    return {"compilerOptions": options}, notes.listed(), files


def flatten_tsconfig(
    read: Callable[[str], str | None], configs: Sequence[str], *, budget: int = DEFAULT_BOUNDS.tsconfigs
) -> tuple[dict[str, Any], list[str]]:
    """One root ``tsconfig.json`` mapping every alias of *configs* (after ``extends``) to root-relative targets.

    At most *budget* files are read, each once: each root in turn, then what it extends.
    """

    flattened, notes, _files = _flatten(read, configs, budget=budget)
    return flattened, notes


# --------------------------------------------------------------------------- #
# running the engine
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RunOutcome:
    """One engine run: its JSON document, or the named cap or failure that stopped it."""

    payload: dict[str, Any] | None = None
    cap: str = ""
    failure: str = ""


def engine_args(engine: Engine, *, rules: Path, output: Path, depth: int, seconds: int, bounds: Bounds) -> list[str]:
    """The scan, offline: local rules only, cross-file taint, no suppression comments, no version check."""

    return [
        str(engine.path),
        "scan",
        "--config",
        str(rules),
        "--enable-interfile",
        f"--interfile-depth={depth}",
        "--interfile-dedup-by=source-sink",
        f"--interfile-timeout={seconds}",
        "--dataflow-traces",
        "--json",
        f"--json-output={output}",
        "--quiet",
        "--disable-version-check",
        "--disable-nosem",
        "--no-rewrite-rule-ids",
        "--no-git-ignore",
        "--project-root=.",
        "--timeout=10",
        "--timeout-threshold=3",
        f"--max-target-bytes={bounds.file_bytes}",
        f"--max-memory={bounds.memory_mb}",
        "--jobs=2",
        ".",
    ]


def contained(args: Sequence[str], isolation: Isolation, *, work: Path, cwd: Path, output: Path) -> list[str]:
    """*args* wrapped in the host's sandbox: network denied, reads limited to *work*, writes to *output*'s folder.

    Any other *isolation* raises: the engine never runs unsandboxed.
    """

    if isolation.mode == "sandbox-exec":
        from lemoncrow.pro.environment.macos_sandbox import sandbox_command

        # Writable: only <work>/.lemoncrow-sandbox-home, which holds the output and the engine's HOME and TMPDIR.
        return sandbox_command(list(args), workspace=work, cwd=cwd)
    if isolation.mode == "bubblewrap":
        from lemoncrow.pro.capabilities.review.hermetic import offline_bwrap_prefix

        # lc-debt: this bubblewrap wrapping has not been run against the real engine (built on macOS); a Linux host
        # without working bubblewrap does not run the engine at all. Upgrade: run test_real_binary_* on a Linux
        # runner with bubblewrap and require it there.
        return [
            *offline_bwrap_prefix(),
            "--ro-bind",
            str(work),
            str(work),
            "--bind",
            str(output.parent),
            str(output.parent),
            "--chdir",
            str(cwd),
            "--",
            *args,
        ]
    raise ValueError(f"{_NEVER_UNSANDBOXED} (isolation {isolation.mode!r})")


def run_engine(
    engine: Engine,
    *,
    framework: Framework,
    staged: Staged,
    work: Path,
    seconds: int,
    bounds: Bounds,
    isolation: Isolation,
    env: Mapping[str, str],
    runner: Runner,
) -> RunOutcome:
    """One framework's rules over one staged side, under the time and memory ceilings."""

    from lemoncrow.pro.environment.sandbox_limits import SandboxCapacityBusy, limit_prefix_for, sandbox_slot

    # The only directory the sandboxed engine may write (its output, HOME and TMPDIR): fresh for every run, so nothing
    # one run leaves there (a config file, a link where the next output goes) reaches the next.
    home = work / ".lemoncrow-sandbox-home"
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(mode=0o700)
    output = home / f"{staged.root.name}-{framework.id}.json"
    args = engine_args(
        engine,
        rules=work / "rules" / framework.rules,
        output=output,
        depth=framework.depth,
        seconds=seconds,
        bounds=bounds,
    )
    command = [
        *limit_prefix_for(memory_mb=bounds.memory_mb, timeout=seconds),
        *contained(args, isolation, work=work, cwd=staged.root, output=output),
    ]
    # macOS: the memory monitor ends the run at `seconds` (exit 124) and kills its whole tree; the process-group
    # deadline here is only the backstop. Elsewhere the deadline here is the time cap.
    # lc-debt: if the macOS monitor itself hangs, the backstop kills its process group but not the engine, which the
    # monitor started in a session of its own. Upgrade: a kernel job limit, or the monitor's pid tree killed here.
    deadline = float(seconds + 15) if sys.platform == "darwin" else float(seconds)
    try:
        with sandbox_slot(float(seconds)):
            done = runner(command, staged.root, env, deadline)
    except TimeoutError:
        return RunOutcome(cap="run_seconds")
    except SandboxCapacityBusy:
        return RunOutcome(failure="the sandbox capacity was busy")
    except OSError as exc:
        return RunOutcome(failure=f"Opengrep could not start ({type(exc).__name__})")
    if done.returncode == 124:
        return RunOutcome(cap="run_seconds")
    if done.returncode in (137, -9):
        return RunOutcome(cap="memory_mb")
    data, over = _read_output(output, bounds.json_bytes)
    if over:
        return RunOutcome(cap="json_bytes")
    payload: Any = None
    if data is not None:
        try:
            payload = json.loads(data)
        except (ValueError, RecursionError):  # a nesting deeper than the parser's stack is unreadable output
            payload = None
    if done.returncode != 0:
        return RunOutcome(failure=f"Opengrep exited with code {done.returncode}{_first_error(payload)}")
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return RunOutcome(failure="Opengrep wrote no readable output")
    if len(payload["results"]) > bounds.results:
        return RunOutcome(cap="results")
    return RunOutcome(payload=payload)


def _read_output(path: Path, limit: int) -> tuple[bytes | None, bool]:
    """``(bytes, over limit)`` of the engine's JSON: a regular file opened without following a link or blocking on a
    pipe planted where it goes, or nothing."""

    try:
        handle = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None, False
    with os.fdopen(handle, "rb") as reader:
        info = os.fstat(reader.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None, False
        if info.st_size > limit:
            return None, True
        data = reader.read(limit + 1)
    return (None, True) if len(data) > limit else (data, False)


_ERROR_TYPE: Final[re.Pattern[str]] = re.compile(r"^[\w .:()-]{1,80}$")


def _error_type(row: object) -> str:
    """An engine error's ``type`` (a name, or ``[name, locations]``) when it is a plain name, else ``""``."""

    raw = row.get("type") if isinstance(row, dict) else None
    if isinstance(raw, list) and raw:
        raw = raw[0]
    text = raw if isinstance(raw, str) else ""
    return text if _ERROR_TYPE.fullmatch(text) else ""


def _first_error(payload: Any) -> str:
    """The first error type the engine's JSON names; never its stderr or messages (they quote repository text)."""

    errors = payload.get("errors") if isinstance(payload, dict) else None
    for row in errors if isinstance(errors, list) else ():
        kind = _error_type(row)
        if kind:
            return f": {kind}"
    return ""


# --------------------------------------------------------------------------- #
# flows: symbols from tree-sitter, keyed on source and sink
# --------------------------------------------------------------------------- #

_PARSER_BY_SUFFIX: Final[Mapping[str, str]] = {
    ".py": "python",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".java": "java",
}
_TS_FUNCTIONS: Final[frozenset[str]] = frozenset(
    {
        "function_declaration",
        "method_definition",
        "arrow_function",
        "function_expression",
        "function",
        "generator_function_declaration",
    }
)
_FUNCTION_KINDS: Final[Mapping[str, frozenset[str]]] = {
    "python": frozenset({"function_definition", "lambda"}),
    "typescript": _TS_FUNCTIONS,
    "tsx": _TS_FUNCTIONS,
    "javascript": _TS_FUNCTIONS,
    "java": frozenset({"method_declaration", "constructor_declaration", "lambda_expression"}),
}
_CLASS_KINDS: Final[frozenset[str]] = frozenset(
    {"class_definition", "class_declaration", "interface_declaration", "enum_declaration", "class"}
    | {"abstract_class_declaration"}
)
_ANONYMOUS: Final[frozenset[str]] = frozenset(
    {"lambda", "arrow_function", "function_expression", "function", "lambda_expression"}
)
_STRINGS: Final[frozenset[str]] = frozenset({"string", "template_string", "string_literal"})
#: A function, class or variable name from source kept as text: one plain name (``#`` for a private JS member).
_SOURCE_NAME: Final[re.Pattern[str]] = re.compile(r"^#?[\w$]{1,120}$")
_ROUTE_TEXT: Final[re.Pattern[str]] = re.compile(r"^[\w$.]{1,60}\([\"'`][\w/:.{}*@~+-]{0,100}[\"'`]\)$")
_EXPR_TEXT: Final[re.Pattern[str]] = re.compile(r"^[\w$.\[\]()\"', :-]{1,120}$")
_CALLEE_TEXT: Final[re.Pattern[str]] = re.compile(r"^(?:new )?[\w$.()]{1,160}$")
_LABEL_TEXT: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*){0,8}$")
#: One argument list whose own arguments are already empty: applied until nothing changes, it empties every list.
_ARGUMENTS: Final[re.Pattern[str]] = re.compile(r"\((?:[^()]|\(\))*\)")
_STEP_KINDS: Final[Mapping[str, str]] = {
    "source": "source",
    "through": "through",
    "through_call": "call",
    "sink_in_call": "call",
    "parameter": "parameter",
    "return": "return",
    "sink": "sink",
}


def _digest_text(prefix: str, text: str) -> str:
    return f"{prefix}#{hashlib.sha256(text.encode('utf-8', 'replace')).hexdigest()[:12]}"


def _flat(text: str) -> str:
    return " ".join(str(text or "").split())


def expression_text(text: str) -> str:
    """A source read as text when it is identifier-like (``request.data``), else a digest of it."""

    flat = _flat(text)
    return flat if _EXPR_TEXT.fullmatch(flat) else _digest_text("expr", flat)


def callee_text(text: str) -> str:
    """The callee of a sink with every argument list emptied (``Widget.objects.filter().update``)."""

    flat = _flat(text)
    if len(flat) > _CALLEE_INPUT:  # emptying argument lists costs length x nesting: a longer callee is a digest
        return _digest_text("call", flat)
    previous = None
    while previous != flat:
        previous, flat = flat, _ARGUMENTS.sub("()", flat)
    flat = flat.replace("( )", "()")
    return flat if _CALLEE_TEXT.fullmatch(flat) else _digest_text("call", flat)


def _node_text(source: bytes, node: Any) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8", "replace")


def symbol_name(text: str) -> str:
    """A function or class name from source: itself when it is a plain name (a string-named method without its
    quotes), else a digest. A string or computed name (``'<img src=x>'()``, ``[expr]()``) can hold any text."""

    flat = _flat(text)
    inner = flat[1:-1] if len(flat) >= 2 and flat[0] == flat[-1] and flat[0] in "\"'`" else flat
    return inner if _SOURCE_NAME.fullmatch(inner) else _digest_text("name", flat)


def _anonymous_name(source: bytes, node: Any, counter: list[int]) -> str:
    """A handler passed to ``post('/api/x', ...)`` is named by its route, through up to two wrapper calls."""

    parent = node.parent
    holder = parent
    for _ in range(3):
        if holder is None or holder.type not in ("arguments", "argument_list"):
            break
        call = holder.parent
        arguments = [child for child in holder.children if child.is_named]
        if arguments and arguments[0].type in _STRINGS and call is not None:
            function = call.child_by_field_name("function") or call.child_by_field_name("name")
            callee = _node_text(source, function).split(".")[-1] if function is not None else "call"
            route = f"{_flat(callee)}({_flat(_node_text(source, arguments[0]))})"
            if _ROUTE_TEXT.fullmatch(route):
                return route
            break
        holder = call.parent if call is not None else None
    if parent is not None and parent.type == "variable_declarator":
        name = parent.child_by_field_name("name")
        if name is not None and _SOURCE_NAME.fullmatch(_node_text(source, name)):
            return _node_text(source, name)
    if parent is not None and parent.type in ("pair", "assignment_expression", "public_field_definition"):
        key = parent.child_by_field_name("key") or parent.child_by_field_name("left")
        if key is not None:
            text = _node_text(source, key).split(".")[-1]
            if _SOURCE_NAME.fullmatch(text):
                return text
    counter[0] += 1
    return f"<anonymous{counter[0]}>"


def function_index(source: bytes, path: str) -> list[tuple[int, int, str]] | None:
    """``(first line, last line, qualified name)`` of every function in a file; ``None`` without a parser."""

    from lemoncrow.pro.capabilities.semantic_file_memory.treesitter_ast import tree_sitter_parser

    language = _PARSER_BY_SUFFIX.get(PurePosixPath(path).suffix.lower(), "")
    parser = tree_sitter_parser(language) if language else None
    if parser is None:
        return None
    try:
        tree = parser.parse(source)
    except Exception:  # a grammar failure is a file without names, never a failed run
        return None
    kinds = _FUNCTION_KINDS[language]
    out: list[tuple[int, int, str]] = []
    # Depth first in document order without recursion: each entry is (node, enclosing names, sibling counter).
    stack: list[tuple[Any, tuple[str, ...], list[int]]] = [(tree.root_node, (), [0])]
    while stack:
        node, prefix, counter = stack.pop()
        pending: list[tuple[Any, tuple[str, ...], list[int]]] = []
        for child in node.children:
            if not child.is_named:  # keyword tokens such as `function` and `class` share their node type names
                continue
            if child.type in _CLASS_KINDS:
                name_node = child.child_by_field_name("name")
                name = symbol_name(_node_text(source, name_node)) if name_node is not None else child.type
                pending.append((child, (*prefix, name), [0]))
            elif child.type in kinds:
                if child.type in _ANONYMOUS:
                    name = _anonymous_name(source, child, counter)
                else:
                    name_node = child.child_by_field_name("name")
                    name = symbol_name(_node_text(source, name_node)) if name_node is not None else child.type
                qualified = (*prefix, name)
                out.append((child.start_point[0] + 1, child.end_point[0] + 1, ".".join(qualified)))
                pending.append((child, qualified, [0]))
            else:
                pending.append((child, prefix, counter))
        stack.extend(reversed(pending))
    out.sort(key=lambda row: (row[0], -row[1], row[2]))
    return out


@dataclass(frozen=True, slots=True)
class Step:
    path: str
    line: int
    kind: str
    label: str = ""

    def to_wire(self) -> dict[str, Any]:
        row: dict[str, Any] = {"at": f"{self.path}:{self.line}", "kind": self.kind}
        if self.label:
            row["label"] = self.label
        return row


@dataclass(frozen=True, slots=True)
class Flow:
    """One flow on one side."""

    kind: str
    framework: str
    source_symbol: str
    sink_symbol: str
    entry_point: str
    source: Step
    sink: Step
    callee: str
    path: tuple[str, ...]
    steps: tuple[Step, ...]

    @property
    def key(self) -> tuple[str, str]:
        return (self.source_symbol, self.sink_symbol)

    def order(self) -> tuple[Any, ...]:
        return (self.entry_point, self.source_symbol, self.sink_symbol, self.path, self.kind)


class FunctionLines:
    """The innermost function holding each line of one file, looked up by bisection.

    Innermost is the smallest span that holds the line, a tie going to the earlier row of
    :func:`function_index`'s order. Built once per file by giving each line to the first row (smallest span first)
    that holds it, skipping lines already given (union-find), so it costs the file's lines plus its rows, never
    rows times lookups.
    """

    def __init__(self, rows: Sequence[tuple[int, int, str]]) -> None:
        self._starts: list[int] = []
        self._names: list[str | None] = []
        if not rows:
            return
        top = max(last for _first, last, _name in rows)
        owner = [-1] * (top + 2)
        following = list(range(top + 2))  # the next line from here that no row owns yet

        def free(line: int) -> int:
            while following[line] != line:
                following[line] = following[following[line]]
                line = following[line]
            return line

        for index in sorted(range(len(rows)), key=lambda at: (rows[at][1] - rows[at][0], at)):
            first, last, _name = rows[index]
            line = free(min(max(first, 1), top + 1))
            while line <= last:
                owner[line] = index
                following[line] = line + 1
                line = free(line + 1)
        previous = -2
        for line in range(1, top + 2):
            current = owner[line]
            if current != previous:
                self._starts.append(line)
                self._names.append(rows[current][2] if current >= 0 else None)
                previous = current

    def name(self, line: int) -> str | None:
        at = bisect.bisect_right(self._starts, line) - 1
        return self._names[at] if at >= 0 else None


class Symbols:
    """Enclosing-function names for one staged side, parsed once per file."""

    def __init__(self, staged: Staged) -> None:
        self._staged = staged
        self._index: dict[str, FunctionLines | None] = {}
        self.unnamed: set[str] = set()

    def function(self, path: str, line: int) -> str:
        if path not in self._index:
            data = _read_regular(self._staged.root / path, max(self._staged.files.get(path, 0), 1))
            rows = function_index(data, path) if data is not None else None
            self._index[path] = FunctionLines(rows) if rows is not None else None
        index = self._index[path]
        if index is None:
            self.unnamed.add(PurePosixPath(path).suffix.lower() or path)
            return f"{path}::<file>"
        return f"{path}::{index.name(line) or '<module>'}"


def _relative(raw: object, staged: Staged) -> str:
    text = str(raw or "")
    root = str(staged.root)
    if text.startswith(root + "/"):
        text = text[len(root) + 1 :]
    while text.startswith("./"):
        text = text[2:]
    return text if _safe_relative(text) and text in staged.files else ""


def _line(raw: object) -> int:
    start = raw.get("start") if isinstance(raw, dict) else None
    value = start.get("line") if isinstance(start, dict) else None
    return value if isinstance(value, int) and value > 0 else 0


def _trace(nodes: object, out: list[dict[str, Any]]) -> None:
    """The trace's nodes in order, children inline (iteratively: a hostile document cannot recurse deep).

    Unbounded here on purpose: the document is already within ``Bounds.json_bytes``, and what is shown of a long
    route is cut by ``Bounds.steps``, which the report names.
    """

    stack: list[Any] = list(reversed(nodes)) if isinstance(nodes, list) else []
    while stack:
        node = stack.pop()
        if not isinstance(node, dict):
            continue
        out.append(node)
        children = node.get("children")
        if isinstance(children, list):
            stack.extend(reversed(children))


class _OutOfTime(Exception):
    """The whole data path's time cap ran out while an engine document was read."""


def _located(raw: object, staged: Staged) -> tuple[str, int]:
    """``(staged path, line)`` of an engine location, or ``("", 0)`` outside the staged files or past a file's end."""

    if not isinstance(raw, dict):
        return "", 0
    path = _relative(raw.get("path"), staged)
    line = _line(raw)
    if not path or not line or line > staged.lines.get(path, 0):
        return "", 0
    return path, line


def flows_from(
    payload: Mapping[str, Any],
    *,
    framework: Framework,
    staged: Staged,
    symbols: Symbols,
    expired: Callable[[], bool] = _never,
) -> tuple[list[Flow], int]:
    """The flows of one engine document, keyed and named, and how many results of the rules were dropped.

    A result outside the staged files or past its file's last line is dropped (and counted); a step there is left
    out. *expired* (the whole time cap) is checked per result: past it this raises :class:`_OutOfTime`.
    """

    out: list[Flow] = []
    dropped = 0
    for result in payload.get("results") or ():
        if expired():
            raise _OutOfTime
        if not isinstance(result, dict):
            continue
        kind = _rule_kind(str(result.get("check_id") or ""), framework.id)
        if not kind:
            continue
        sink_path, sink_line = _located(result, staged)
        if not sink_path:
            dropped += 1
            continue
        raw_extra = result.get("extra")
        extra: dict[str, Any] = raw_extra if isinstance(raw_extra, dict) else {}
        raw_metavars = extra.get("metavars")
        metavars: dict[str, Any] = raw_metavars if isinstance(raw_metavars, dict) else {}
        nodes: list[dict[str, Any]] = []
        _trace(extra.get("dataflow_trace"), nodes)
        steps: list[Step] = []
        for node in nodes:
            path, line = _located(node.get("location"), staged)
            if not path:
                continue
            step_kind = _STEP_KINDS.get(str(node.get("kind") or ""), "step")
            content = _flat(str(node.get("content") or ""))
            label = content if len(content) <= 80 and _LABEL_TEXT.fullmatch(content) else ""
            if step_kind == "source":
                label = expression_text(content)
            steps.append(Step(path, line, step_kind, label))
        callee_raw = ""
        for name in ("$F", "$T"):
            value = metavars.get(name)
            if isinstance(value, dict) and value.get("abstract_content"):
                callee_raw = ("new " if name == "$T" else "") + str(value["abstract_content"])
                break
        callee = callee_text(callee_raw) if callee_raw else _digest_text("call", sink_path)
        sink = Step(sink_path, sink_line, "sink", callee)
        if steps and steps[-1].kind == "sink":
            steps[-1] = sink
        else:
            steps.append(sink)
        source = next((step for step in steps if step.kind == "source"), steps[0])
        source_fn = symbols.function(source.path, source.line)
        sink_fn = symbols.function(sink.path, sink.line)
        functions: list[str] = [source_fn]
        for step in steps:
            name = symbols.function(step.path, step.line)
            if functions[-1] != name:
                functions.append(name)
        if functions[-1] != sink_fn:
            functions.append(sink_fn)
        out.append(
            Flow(
                kind=kind,
                framework=framework.id,
                source_symbol=f"{source_fn}|{source.label or 'source'}",
                sink_symbol=f"{sink_fn}|{callee}",
                entry_point=source_fn,
                source=source,
                sink=sink,
                callee=callee,
                path=tuple(functions),
                steps=tuple(steps),
            )
        )
    return out, dropped


# --------------------------------------------------------------------------- #
# the delta
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Change:
    kind: str  # added | removed | rerouted
    base: Flow | None
    head: Flow | None

    @property
    def flow(self) -> Flow:
        flow = self.head if self.head is not None else self.base
        assert flow is not None
        return flow


def _by_key(flows: Iterable[Flow]) -> dict[tuple[str, str], list[Flow]]:
    out: dict[tuple[str, str], list[Flow]] = {}
    for flow in flows:
        out.setdefault(flow.key, []).append(flow)
    for rows in out.values():
        rows.sort(key=Flow.order)
    return out


def diff_flows(base: Iterable[Flow], head: Iterable[Flow]) -> tuple[list[Change], int]:
    """Added, removed and rerouted flows in a stable order, and how many keys were unchanged."""

    old, new = _by_key(base), _by_key(head)
    changes: list[Change] = []
    unchanged = 0
    for key in sorted(set(old) | set(new)):
        before, after = old.get(key), new.get(key)
        if before is None and after is not None:
            changes.append(Change("added", None, after[0]))
        elif after is None and before is not None:
            changes.append(Change("removed", before[0], None))
        elif before is not None and after is not None:
            old_paths, new_paths = {flow.path for flow in before}, {flow.path for flow in after}
            if old_paths == new_paths:
                unchanged += 1
                continue
            was = next(flow for flow in before if flow.path not in new_paths) if old_paths - new_paths else before[0]
            now = next(flow for flow in after if flow.path not in old_paths) if new_paths - old_paths else after[0]
            changes.append(Change("rerouted", was, now))
    changes.sort(key=lambda change: (CHANGE_KINDS.index(change.kind), change.flow.order()))
    return changes, unchanged


def _route(flow: Flow, steps_cap: int) -> dict[str, Any]:
    steps = list(flow.steps)
    omitted = max(0, len(steps) - steps_cap)
    if omitted:
        steps = [*steps[: max(steps_cap - 1, 0)], steps[-1]] if steps_cap > 0 else []
    return {"path": list(flow.path), "steps": [step.to_wire() for step in steps], "omitted_steps": omitted}


def change_wire(change: Change, *, depth: int, steps_cap: int) -> dict[str, Any]:
    flow = change.flow
    row: dict[str, Any] = {
        "change": change.kind,
        "kind": flow.kind,
        "framework": flow.framework,
        "entry_point": flow.entry_point,
        "source": {
            "symbol": flow.source_symbol,
            "at": f"{flow.source.path}:{flow.source.line}",
            "label": flow.source.label,
        },
        "sink": {"symbol": flow.sink_symbol, "at": f"{flow.sink.path}:{flow.sink.line}", "callee": flow.callee},
        "found_by": f"Opengrep {ENGINE_VERSION} taint, depth {depth}",
    }
    if change.base is not None:
        row["base"] = _route(change.base, steps_cap)
    if change.head is not None:
        row["head"] = _route(change.head, steps_cap)
    return row


# --------------------------------------------------------------------------- #
# the producer
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class _Report:
    bounds: Bounds
    not_measured: list[dict[str, str]] = field(default_factory=list)
    caps: list[dict[str, Any]] = field(default_factory=list)

    def missing(self, scope: str, reason: str) -> None:
        row = {"scope": scope, "reason": reason}
        if row not in self.not_measured:
            self.not_measured.append(row)

    def cap(self, name: str, **detail: Any) -> None:
        self.caps.append({"cap": name, "limit": getattr(self.bounds, name), **detail})


_CAP_TEXT: Final[Mapping[str, str]] = {
    "run_seconds": "the time cap of one engine run ({limit} s)",
    "total_seconds": "the time cap of the whole data path ({limit} s)",
    "memory_mb": "the memory cap ({limit} MiB)",
    "json_bytes": "the engine output cap ({limit} bytes)",
    "results": "the cap on engine results ({limit})",
}


def _language_gaps(changed_paths: Sequence[str], report: _Report) -> None:
    other: dict[str, int] = {}
    for path in changed_paths:
        language = _OTHER_LANGUAGES.get(PurePosixPath(path).suffix.lower())
        if language:
            other[language] = other.get(language, 0) + 1
    for language in sorted(other):
        report.missing(
            f"{language} ({other[language]} changed file{'s' if other[language] != 1 else ''})",
            f"language not covered: there are no data path rules for {language}",
        )


def _framework_gaps(
    changed_paths: Sequence[str], frameworks: Sequence[Framework], measured: set[str], report: _Report
) -> None:
    for item in FRAMEWORKS:
        if item.id in measured or item in frameworks:
            continue
        touched = sum(1 for path in changed_paths if PurePosixPath(path).suffix.lower() in item.suffixes)
        if touched:
            report.missing(
                f"{item.language} ({touched} changed file{'s' if touched != 1 else ''})",
                f"framework not covered: no {item.title} import found, and the {item.language} rules cover "
                f"{item.title} only",
            )


_EXCLUSION_SCOPE: Final[Mapping[str, str]] = {
    "cap files hit": "{side}: {count} source files past the first {files}: {names}",
    "cap file_bytes hit": "{side}: {count} source files over {file_bytes} bytes: {names}",
    "cap total_bytes hit": "{side}: {count} source files past {total_bytes} bytes in all: {names}",
}


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _staging_gaps(staged: Staged, side: str, report: _Report, frameworks: Sequence[Framework]) -> list[str]:
    """Name what staging left out of one side; returns what makes the report partial."""

    bounds = report.bounds
    partial: list[str] = []
    if staged.over_files:
        report.cap("files", side=side, skipped=staged.over_files)
    if staged.over_file_bytes:
        report.cap(
            "file_bytes",
            side=side,
            skipped=len(staged.over_file_bytes),
            paths=[_path_text(rel) for rel in staged.over_file_bytes[:5]],
        )
    if staged.over_total_bytes:
        report.cap("total_bytes", side=side, skipped=staged.over_total_bytes)
    groups: dict[str, list[str]] = {}
    for rel, reason in (*staged.excluded.items(), *staged.excluded_dirs.items()):
        groups.setdefault(reason, []).append(rel)
    for reason in sorted(groups):
        rels = sorted(groups[reason])
        template = _EXCLUSION_SCOPE.get(reason, "{side}: {count} paths: {names}")
        scope = template.format(
            side=side,
            count=len(rels),
            names=_names(rels),
            files=bounds.files,
            file_bytes=bounds.file_bytes,
            total_bytes=bounds.total_bytes,
        )
        report.missing(scope, reason)
    left_out = len(staged.excluded) + len(staged.excluded_dirs)
    if left_out:
        partial.append(f"{_plural(left_out, 'path')} on {side} not read")
    if staged.skipped:
        report.missing(
            f"{side}: {_plural(len(staged.skipped), 'path')} not read: {_names(staged.skipped)}",
            "vendored, generated or minified paths are not analysed (alike on both sides)",
        )
        partial.append(f"{_plural(len(staged.skipped), 'vendored or generated path')} on {side} not read")
    for note in staged.notes:
        report.missing(f"{side}: staging", note)
    if staged.over_tsconfigs:
        report.cap("tsconfigs", side=side, skipped=staged.over_tsconfigs)
        report.missing(
            f"{side}: {staged.over_tsconfigs} tsconfig files past the first {bounds.tsconfigs} read "
            "(root files and the files they extend)",
            "cap tsconfigs hit",
        )
        partial.append(f"tsconfig aliases cut on {side}")
    if any(item.id == "express" for item in frameworks) and staged.counts.get("express"):
        if not staged.tsconfigs:
            report.missing(
                f"{side}: TypeScript and JavaScript imports that are not relative",
                "no tsconfig: there is no tsconfig.json, so only relative imports were followed across files",
            )
        for note in staged.tsconfig_notes:
            report.missing(f"{side}: tsconfig", note)
        if staged.tsconfigs and not staged.tsconfig_written:  # a staged folder took the tsconfig.json name
            partial.append(f"tsconfig aliases not used on {side}")
    return partial


def _empty_report(state: str, summary: str, report: _Report, *, isolation: Isolation | None) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "entry": ENTRY,
        "state": state,
        "experimental": True,
        "summary": summary,
        "detail": "",
        "disclosure": DISCLOSURE,
        "engine": {"name": "Opengrep", "version": ENGINE_VERSION, "sha256": "", "asset": ""},
        "rules": {"digest": rules_digest(), "files": [path.name for path in rule_files()]},
        "isolation": {"mode": isolation.mode, "note": isolation.note} if isolation else {"mode": "", "note": ""},
        "coverage": {"languages": [], "frameworks": []},
        "counts": {"added": 0, "removed": 0, "rerouted": 0, "unchanged": 0},
        "flows": {kind: [] for kind in CHANGE_KINDS},
        "omitted": {kind: 0 for kind in CHANGE_KINDS},
        "not_measured": report.not_measured,
        "caps": report.caps,
        "limits": [],
        "setup": "",
        "bounds": asdict(report.bounds),
        "seconds": 0.0,
    }


def not_available(
    reason: str, *, setup: str = "", bounds: Bounds = DEFAULT_BOUNDS, isolation: Isolation | None = None
) -> dict[str, Any]:
    """The report when nothing could run: it says why and how to set it up, and claims nothing about flows."""

    report = _Report(bounds)
    report.missing("all flows", f"engine unavailable: {reason}")
    out = _empty_report("not_available", f"Data path not available: {reason}", report, isolation=isolation)
    out["setup"] = setup
    return out


def _failed(error: BaseException, *, bounds: Bounds, isolation: Isolation | None) -> dict[str, Any]:
    """The stored report of a run that raised: the error's type only (its text can quote repository content)."""

    kind = type(error).__name__
    reason = f"the run stopped on an unexpected error ({kind if _ERROR_TYPE.fullmatch(kind) else 'error'})"
    report = _Report(bounds)
    report.missing("all flows", f"not diffed: {reason}")
    return _empty_report("not_measured", f"Data path not measured: {reason}", report, isolation=isolation)


def sweep_stale_scratch(parent: Path, *, older_than: float, now: float | None = None) -> int:
    """Remove this producer's scratch directories under *parent* untouched for *older_than* seconds.

    Only directories named like :func:`run_data_path`'s, owned by this user, and never through a link: what a run
    killed with its server left behind (its ``finally`` never ran).
    """

    moment = time.time() if now is None else now
    owner = os.getuid() if hasattr(os, "getuid") else None
    removed = 0
    try:
        entries = list(os.scandir(parent))
    except OSError:
        return 0
    for entry in entries:
        if not entry.name.startswith(_SCRATCH_PREFIX):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not stat.S_ISDIR(info.st_mode) or (owner is not None and info.st_uid != owner):
            continue
        if moment - info.st_mtime < older_than:
            continue
        shutil.rmtree(entry.path, ignore_errors=True)
        removed += 1
    return removed


def run_data_path(
    old_tree: Path,
    new_tree: Path,
    *,
    changed_paths: Sequence[str] = (),
    environ: Mapping[str, str] | None = None,
    bounds: Bounds = DEFAULT_BOUNDS,
    runner: Runner = run_process,
    clock: Callable[[], float] = time.monotonic,
    system: str | None = None,
    machine: str | None = None,
    isolation: Isolation | None = None,
    scratch_root: Path | None = None,
) -> dict[str, Any]:
    """The data path report for one Snapshot's base and head trees.

    Never raises for what the engine or the repository does: an unexpected error is a ``not_measured`` report that
    names only its type. Without a sandbox nothing runs (``not_available`` with the setup line).
    """

    started = clock()
    env_source = os.environ if environ is None else environ
    system = system or platform.system()
    machine = machine or platform.machine()
    parent = Path(scratch_root) if scratch_root is not None else Path(tempfile.gettempdir())
    sweep_stale_scratch(parent, older_than=max(_STALE_SCRATCH_SECONDS, 4 * bounds.total_seconds))
    contained_by = isolation or detect_isolation()
    if contained_by.mode not in _CONTAINED:
        return not_available(contained_by.note, setup=contained_by.setup, bounds=bounds, isolation=contained_by)
    # Resolved: the macOS sandbox profile and the engine's reported paths use the real path (/private/var/...).
    work = Path(tempfile.mkdtemp(prefix=_SCRATCH_PREFIX, dir=str(parent))).resolve()
    try:
        try:
            engine, why = locate_engine(env_source, work, system=system, machine=machine)
            if engine is None:
                return not_available(why, setup=setup_hint(system, machine), bounds=bounds)
            report = _run(
                engine,
                old_tree,
                new_tree,
                work=work,
                changed_paths=changed_paths,
                environ=env_source,
                bounds=bounds,
                runner=runner,
                clock=clock,
                started=started,
                isolation=contained_by,
            )
        except Exception as exc:  # repository content ends as a stored report, never a failed request
            report = _failed(exc, bounds=bounds, isolation=contained_by)
        report["seconds"] = round(max(0.0, clock() - started), 3)
        return report
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _scanned(payload: Mapping[str, Any], staged: Staged) -> set[str] | None:
    """The staged files the engine says it scanned, or ``None`` when it does not say."""

    paths = payload.get("paths")
    rows = paths.get("scanned") if isinstance(paths, dict) else None
    if not isinstance(rows, list):
        return None
    return {rel for rel in (_relative(row, staged) for row in rows) if rel}


def _flow_files(flow: Flow) -> set[str]:
    return {flow.source.path, flow.sink.path, *(step.path for step in flow.steps)}


def _measurable(
    side_flows: Mapping[str, list[Flow]], staged: Mapping[str, Staged], blind: Mapping[str, set[str]]
) -> tuple[list[Flow], list[Flow], int]:
    """Base and head flows to diff, and how many keys were left out.

    A key with any flow through a file the other side has but did not read or scan is left out on both sides: base
    only would read as removed, head only as added, and neither is known.
    """

    def unseen(label: str) -> Callable[[str], bool]:
        files = set(staged[label].excluded) | blind[label]
        folders = tuple(staged[label].excluded_dirs)
        return lambda rel: rel in files or (bool(folders) and rel.startswith(folders))

    not_in_head, not_in_base = unseen("head"), unseen("base")
    unsure = {flow.key for flow in side_flows["base"] if any(map(not_in_head, _flow_files(flow)))}
    unsure |= {flow.key for flow in side_flows["head"] if any(map(not_in_base, _flow_files(flow)))}
    keep = {label: [flow for flow in side_flows[label] if flow.key not in unsure] for label in ("base", "head")}
    return keep["base"], keep["head"], len(unsure)


def _run(
    engine: Engine,
    old_tree: Path,
    new_tree: Path,
    *,
    work: Path,
    changed_paths: Sequence[str],
    environ: Mapping[str, str],
    bounds: Bounds,
    runner: Runner,
    clock: Callable[[], float],
    started: float,
    isolation: Isolation,
) -> dict[str, Any]:
    report = _Report(bounds)
    rules = work / "rules"
    rules.mkdir(mode=0o700)
    for path in rule_files():
        shutil.copyfile(path, rules / path.name)

    def expired() -> bool:
        return clock() - started >= bounds.total_seconds

    staged = {
        label: stage_side(tree, work / label, bounds, expired=expired)
        for (_side, label), tree in zip(_SIDES, (old_tree, new_tree), strict=True)
    }
    detected = staged["base"].detected | staged["head"].detected
    frameworks = [item for item in FRAMEWORKS if item.id in detected]
    partial: list[str] = []
    for label in ("base", "head"):
        partial.extend(_staging_gaps(staged[label], label, report, frameworks))
    _language_gaps(changed_paths, report)
    staging_stop = ""
    for label in ("base", "head"):
        if staged[label].timed_out:
            report.cap("total_seconds", side=label, stage="staging")
            staging_stop = f"{_CAP_TEXT['total_seconds'].format(limit=bounds.total_seconds)} was hit copying {label}"
            break
    home = work / ".lemoncrow-sandbox-home"
    home.mkdir(mode=0o700, exist_ok=True)
    env = _engine_env(environ, home)
    changes: list[tuple[Change, int]] = []
    unchanged = 0
    measured: set[str] = set()
    coverage: list[dict[str, Any]] = []
    unnamed: set[str] = set()
    stops: list[str] = []
    for item in frameworks:
        side_flows: dict[str, list[Flow]] = {}
        blind: dict[str, set[str]] = {"base": set(), "head": set()}
        stopped = staging_stop
        for label in () if stopped else ("base", "head"):
            side = staged[label]
            if not side.counts.get(item.id):
                side_flows[label] = []  # nothing of this language on this side: measured, and empty
                continue
            remaining = int(bounds.total_seconds - (clock() - started))
            if remaining <= 0:
                report.cap("total_seconds", framework=item.id, side=label)
                stopped = f"{_CAP_TEXT['total_seconds'].format(limit=bounds.total_seconds)} was hit before {label}"
                break
            seconds = min(bounds.run_seconds, remaining)
            outcome = run_engine(
                engine,
                framework=item,
                staged=side,
                work=work,
                seconds=seconds,
                bounds=bounds,
                isolation=isolation,
                env=env,
                runner=runner,
            )
            if outcome.cap:
                cap_name = (
                    "total_seconds" if outcome.cap == "run_seconds" and seconds < bounds.run_seconds else outcome.cap
                )
                report.cap(cap_name, framework=item.id, side=label)
                stopped = f"{_CAP_TEXT[cap_name].format(limit=getattr(bounds, cap_name))} was hit on {label}"
                break
            if outcome.failure or outcome.payload is None:
                stopped = f"{outcome.failure or 'Opengrep failed'} on {label}"
                break
            scanned = _scanned(outcome.payload, side)
            if scanned is None:
                stopped = f"Opengrep did not list the files it scanned on {label}"
                break
            expected = sorted(rel for rel in side.files if PurePosixPath(rel).suffix.lower() in item.suffixes)
            unscanned = [rel for rel in expected if rel not in scanned]
            if unscanned:
                report.missing(
                    f"{item.title} on {label}: {len(unscanned)} staged files: {_names(unscanned)}",
                    "the engine did not scan them",
                )
                partial.append(f"{_plural(len(unscanned), 'file')} on {label} not scanned")
            errored, errors = _engine_errors(outcome.payload, item, side, label, report)
            if errors:
                partial.append(f"engine errors on {label}")
            blind[label] = set(unscanned) | errored
            symbols = Symbols(side)
            try:
                flows, dropped = flows_from(
                    outcome.payload, framework=item, staged=side, symbols=symbols, expired=expired
                )
            except _OutOfTime:
                report.cap("total_seconds", framework=item.id, side=label, stage="flows")
                stopped = (
                    f"{_CAP_TEXT['total_seconds'].format(limit=bounds.total_seconds)} was hit reading the flows of "
                    f"{label}"
                )
                break
            if dropped:
                report.missing(
                    f"{item.title} on {label}: {_plural(dropped, 'engine result')}",
                    "dropped: outside the staged files or past a file's last line",
                )
                partial.append(f"{_plural(dropped, 'engine result')} on {label} dropped")
            side_flows[label] = flows
            unnamed |= symbols.unnamed
        files = {label: staged[label].counts.get(item.id, 0) for label in ("base", "head")}
        coverage.append(
            {
                "framework": item.id,
                "title": item.title,
                "language": item.language,
                "depth": item.depth,
                "files": files,
                "measured": not stopped,
            }
        )
        if stopped:
            report.missing(f"{item.title} flows", f"not diffed: {stopped}")
            stops.append(f"{item.title}: {stopped}")
            continue
        measured.add(item.id)
        base_flows, head_flows, unsure = _measurable(side_flows, staged, blind)
        if unsure:
            report.missing(
                f"{item.title}: {_plural(unsure, 'flow')} through files the other side did not read or scan",
                "not diffed: neither added nor removed is known",
            )
            partial.append(f"{_plural(unsure, 'flow')} not diffed")
        found, same = diff_flows(base_flows, head_flows)
        changes.extend((change, item.depth) for change in found)
        unchanged += same
    _framework_gaps(changed_paths, frameworks, measured, report)
    for suffix in sorted(unnamed):
        report.missing(f"function names in {suffix} files", "no tree-sitter parser: flows there are keyed by file")
    changes.sort(key=lambda row: (CHANGE_KINDS.index(row[0].kind), row[0].flow.order()))
    totals = {kind: sum(1 for change, _depth in changes if change.kind == kind) for kind in CHANGE_KINDS}
    shown: dict[str, list[dict[str, Any]]] = {kind: [] for kind in CHANGE_KINDS}
    omitted_steps = 0
    for change, depth in changes:
        rows = shown[change.kind]
        if len(rows) >= bounds.flows_per_kind:
            continue
        row = change_wire(change, depth=depth, steps_cap=bounds.steps)
        omitted_steps += sum(int(row[side]["omitted_steps"] > 0) for side in ("base", "head") if side in row)
        rows.append(row)
    omitted = {kind: totals[kind] - len(shown[kind]) for kind in CHANGE_KINDS}
    for kind in CHANGE_KINDS:
        if omitted[kind]:
            report.cap("flows_per_kind", change=kind, omitted=omitted[kind])
    if omitted_steps:
        report.cap("steps", routes=omitted_steps)
    if not frameworks:
        report.missing("all flows", "framework not covered: no Django, Express or Spring source found on either side")
    counted = " · ".join(f"{totals[kind]} {kind}" for kind in CHANGE_KINDS)
    if frameworks and not measured:
        # Every covered framework stopped: nothing was compared, so not even "no flows found" is claimed.
        state, summary, detail = "not_measured", f"Data path not measured: {stops[0]}", "; ".join(stops)
    elif partial or stops:
        # Something one side did not read, scan or finish: what was found stands, and the summary says it is partial.
        gaps = [*partial, *(f"{stop.split(':', 1)[0]} not measured" for stop in stops)]
        shown_gaps = "; ".join(gaps[:3]) + (f"; and {len(gaps) - 3} more" if len(gaps) > 3 else "")
        what = counted if any(totals.values()) else "no flows found in what was measured"
        state, summary, detail = "partial", f"Partial: {what} ({shown_gaps})", "; ".join([*gaps, *stops])
    elif any(totals.values()):
        state, summary, detail = "ran", counted, ""
    else:
        state, summary, detail = "ran", NO_FLOWS, NO_FLOWS_DETAIL
    out = _empty_report(state, summary, report, isolation=isolation)
    out["detail"] = detail
    out["engine"] = {"name": "Opengrep", "version": engine.version, "sha256": engine.sha256, "asset": engine.asset}
    out["coverage"] = {
        "languages": sorted({item.language for item in frameworks if item.id in measured}),
        "frameworks": coverage,
    }
    out["counts"] = {**totals, "unchanged": unchanged}
    out["flows"] = shown
    out["omitted"] = omitted
    out["limits"] = [f"{item.title}: {text}" for item in frameworks for text in item.limits]
    return out


def _engine_errors(
    payload: Mapping[str, Any], framework: Framework, staged: Staged, side: str, report: _Report
) -> tuple[set[str], int]:
    """Per-file stops inside a finished run (time, memory, parse): the run counts, those files do not.

    Returns the staged files the errors name (not measured on this side) and how many errors there were.
    """

    counts: dict[str, int] = {}
    files: set[str] = set()
    total = 0
    errors = payload.get("errors")
    for row in errors if isinstance(errors, list) else ():
        if not isinstance(row, dict):
            continue
        total += 1
        kind = _error_type(row) or "error"
        reason = (
            "Opengrep's per-file time limit (10 s)"
            if "timeout" in kind.lower()
            else "Opengrep's memory cap" if "memory" in kind.lower() else "Opengrep could not analyse them"
        )
        counts[reason] = counts.get(reason, 0) + 1
        rel = _relative(row.get("path"), staged)
        if rel:
            files.add(rel)
    for reason in sorted(counts):
        report.missing(f"{framework.title} on {side}: {counts[reason]} file or rule errors", reason)
    return files, total


# --------------------------------------------------------------------------- #
# storage and the API projection
# --------------------------------------------------------------------------- #


def store_report(store: Any, revision: Any, report: Mapping[str, Any]) -> None:
    """Keep the report as the Snapshot's derived ``data_path`` row (closed by the Review deletion cascade)."""

    from lemoncrow.pro.capabilities.review.store import DerivedResult

    store.put_derived_results(
        [
            DerivedResult(
                review_id=str(revision.review_id),
                revision_id=str(revision.id),
                kind=DERIVED_KIND,
                scope_key="current",
                stamp=stamp(),
                payload=dict(report),
            )
        ]
    )


def stored_report(store: Any, revision: Any) -> dict[str, Any] | None:
    """The report stored for this Snapshot with the current rules and engine, or ``None``."""

    row = store.derived_results(DERIVED_KIND, [str(revision.id)]).get((str(revision.id), "current"))
    if row is None or row[0] != stamp():
        return None
    return dict(row[1])


def projection(store: Any, revision: Any) -> dict[str, Any]:
    """The Review API's ``data_path`` field: the stored report, or ``not_run`` (it runs only on request)."""

    stored = stored_report(store, revision)
    if stored is not None:
        return stored
    return {
        "schema": SCHEMA_VERSION,
        "entry": ENTRY,
        "state": "not_run",
        "experimental": True,
        "summary": "Not run: the data path runs only when requested",
        "disclosure": DISCLOSURE,
    }


__all__ = [
    "BIN_ENV",
    "DEFAULT_BOUNDS",
    "DERIVED_KIND",
    "DISCLOSURE",
    "ENTRY",
    "FRAMEWORKS",
    "NO_FLOWS",
    "RULES_DIR",
    "Bounds",
    "Change",
    "Engine",
    "Flow",
    "FunctionLines",
    "Isolation",
    "Step",
    "detect_isolation",
    "diff_flows",
    "flatten_tsconfig",
    "locate_engine",
    "not_available",
    "parse_jsonc",
    "projection",
    "rules_digest",
    "run_data_path",
    "stage_side",
    "store_report",
    "stored_report",
    "sweep_stale_scratch",
    "symbol_name",
]
