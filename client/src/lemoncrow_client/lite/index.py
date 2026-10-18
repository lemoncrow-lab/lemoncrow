"""Disk-first repository intelligence for Runtime Lite.

The index is intentionally boring infrastructure: one SQLite database under the
repository, WAL for concurrent readers, one serialized incremental writer, FTS5
for lexical retrieval, and small deterministic symbol/import tables. There are
no embeddings, ANN matrices, resident search services, or in-memory repository
graphs.

A writer holds SQLite's BEGIN IMMEDIATE lock while it computes and commits one
generation. WAL readers continue to observe the previous committed generation.
A second writer waits, then reuses the newly committed file identities rather
than rebuilding the repository.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import sqlite3
import stat
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from ..worktrees import WorktreeCatalog
from .imports import IMPORT_STATUSES, ImportResolution, ImportResolver, extract_imports, imported_names

# v3: import rows cover multi-line, relative, re-export, dynamic, Go block,
# Rust group/mod and Java/Kotlin statements (see lite/imports.py).
_SCHEMA_VERSION: Final[int] = 3


class LiteIndexUpgradeRequired(RuntimeError):
    """A newer index must never be modified by an older Runtime Lite process."""


# Never upgrade the database *used by an older, still-running MCP process*.
# Keep the pre-versioned path only as a read-only migration source.
_DB_NAME: Final[str] = f"runtime-lite-v{_SCHEMA_VERSION}.sqlite3"
_LEGACY_DB_NAME: Final[str] = "runtime-lite.sqlite3"
_INDEX_DIR: Final[str] = ".lemoncrow/workspace"
_MAX_INDEX_FILE_BYTES: Final[int] = 4 * 1024 * 1024
_MAX_QUERY_CACHE: Final[int] = 32
_REFRESH_INTERVAL_S: Final[float] = 1.0
#: Minimum gap between progress callbacks while walking the worktree.
_PROGRESS_INTERVAL_S: Final[float] = 0.1
_DEFAULT_RESULTS: Final[int] = 8
_MAX_RESULTS: Final[int] = 32
_SNIPPET_FILES: Final[int] = 3
_SNIPPET_LINES: Final[int] = 32
_MAX_SNIPPET_CHARS: Final[int] = 12_000
# lc-debt: relations(kind="imports") finds importers by name among at most this
# many import rows whose names contain the symbol's text; past it some are not
# listed. Upgrade: an imported-name column or table to match exactly in SQL.
_MAX_NAMED_IMPORT_ROWS: Final[int] = 2_000

_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "about",
        "after",
        "also",
        "and",
        "are",
        "can",
        "code",
        "does",
        "file",
        "for",
        "from",
        "how",
        "into",
        "its",
        "like",
        "method",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "what",
        "when",
        "where",
        "which",
        "with",
        "work",
        "works",
    }
)

_QUERY_NOISE: Final[frozenset[str]] = frozenset(
    {
        "async",
        "await",
        "class",
        "const",
        "def",
        "else",
        "fn",
        "func",
        "function",
        "if",
        "import",
        "let",
        "private",
        "public",
        "return",
        "var",
        "while",
    }
)

_SYMBOL_PATTERNS: Final[tuple[tuple[str, re.Pattern[str]], ...]] = (
    ("class", re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:public\s+)?class\s+([A-Za-z_]\w*)")),
    ("interface", re.compile(r"^\s*(?:export\s+)?interface\s+([A-Za-z_]\w*)")),
    ("type", re.compile(r"^\s*(?:export\s+)?(?:type|enum|trait|struct)\s+([A-Za-z_]\w*)")),
    ("function", re.compile(r"^\s*(?:export\s+(?:default\s+)?)?(?:async\s+)?function\s+([A-Za-z_]\w*)")),
    ("function", re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_]\w*)\s*\(")),
    ("function", re.compile(r"^\s*func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)\s*\(")),
    ("function", re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)\s*\(")),
    ("type", re.compile(r"^\s*type\s+([A-Za-z_]\w*)\b")),
    (
        "binding",
        re.compile(r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*(?::[^=]+)?="),
    ),
)

_PY_TOP_ASSIGN = re.compile(r"^([A-Za-z_]\w*)\s*(?::[^=]+)?=(?!=)")


@dataclass(frozen=True, slots=True)
class LiteIndexReport:
    path: Path
    generation: int
    files_seen: int
    files_changed: int
    files_deleted: int
    cold: bool
    elapsed_s: float

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": True,
            "index_path": str(self.path),
            "generation": self.generation,
            "files_seen": self.files_seen,
            "files_indexed": self.files_changed,
            "files_deleted": self.files_deleted,
            "cold": self.cold,
            "elapsed_s": self.elapsed_s,
        }


@dataclass(frozen=True, slots=True)
class LiteIndexProgress:
    """One observation of a running :meth:`LiteIndex.prepare`.

    ``phase`` is ``seed`` (reusing a linked checkout's rows), ``lock`` (waiting
    for another writer), ``scan`` (walking and indexing files), ``prune``
    (removing deleted files), ``commit`` or ``done``. ``total`` is the number
    of worktree entries to walk, or 0 while unknown.
    """

    phase: str
    done: int
    total: int
    changed: int
    elapsed_s: float


ProgressCallback = Callable[[LiteIndexProgress], None]


class _ProgressEmitter:
    """Throttle progress callbacks so a million-file walk stays cheap."""

    def __init__(self, callback: ProgressCallback | None, started: float) -> None:
        self._callback = callback
        self._started = started
        self._last = 0.0

    @property
    def active(self) -> bool:
        return self._callback is not None

    def emit(self, phase: str, done: int = 0, total: int = 0, changed: int = 0, *, now: bool = False) -> None:
        if self._callback is None:
            return
        moment = time.monotonic()
        if not now and moment - self._last < _PROGRESS_INTERVAL_S:
            return
        self._last = moment
        self._callback(LiteIndexProgress(phase, done, total, changed, moment - self._started))


def _stat_identity(info: os.stat_result) -> str:
    return f"{info.st_size} {info.st_mtime_ns} {info.st_ctime_ns} {info.st_ino} {info.st_dev} {info.st_mode}"


def _tokens(query: str) -> list[str]:
    raw_tokens = _IDENTIFIER.findall(query)
    meaningful = [raw for raw in raw_tokens if raw.lower() not in _STOPWORDS and raw.lower() not in _QUERY_NOISE]
    source = meaningful or [raw for raw in raw_tokens if raw.lower() not in _STOPWORDS] or raw_tokens
    seen: set[str] = set()
    tokens: list[str] = []
    for raw in source:
        token = raw.lower()
        if token in seen:
            continue
        seen.add(token)
        tokens.append(raw)
    return tokens[:12]


def _safe_fts_token(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def _code_identifier_token(token: str) -> bool:
    return "_" in token or any(char.isupper() for char in token[1:])


def _snippet_has_exact_identifier(snippet: str, token: str) -> bool:
    return bool(
        re.search(
            rf"(?<![A-Za-z0-9_]){re.escape(token)}(?![A-Za-z0-9_])",
            snippet,
            flags=re.IGNORECASE,
        )
    )


def _ensure_workspace_git_excluded(repo_root: Path) -> None:
    """Keep Runtime Lite state clone-local without editing the user's .gitignore."""

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if completed.returncode != 0 or not completed.stdout.strip():
        return
    exclude = Path(completed.stdout.strip())
    if not exclude.is_absolute():
        exclude = (repo_root / exclude).resolve()
    pattern = ".lemoncrow/workspace/"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        existing = exclude.read_text(encoding="utf-8", errors="replace") if exclude.exists() else ""
        if any(line.strip() == pattern for line in existing.splitlines()):
            return
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with exclude.open("a", encoding="utf-8") as handle:
            handle.write(prefix + pattern + "\n")
    except OSError:
        # Indexing still works if Git metadata is read-only; artifact state stays
        # under .lemoncrow and the repository's own ignore rules may cover it.
        return


def _worktree_signature(repo_root: Path) -> str | None:
    """Cheap freshness proof before paying for an incremental repository walk."""

    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=False,
            timeout=2.0,
            check=False,
        )
        status = subprocess.run(
            [
                "git",
                "status",
                "--porcelain=v1",
                "-z",
                "--untracked-files=all",
                "--ignore-submodules=dirty",
                "--",
                ".",
                ":(exclude).lemoncrow",
            ],
            cwd=repo_root,
            capture_output=True,
            text=False,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if status.returncode != 0:
        return None

    digest = hashlib.sha256()
    digest.update(head.stdout if head.returncode == 0 else b"<unborn-head>")
    digest.update(b"\0")
    digest.update(status.stdout)
    # Porcelain -z records keep paths verbatim. Stat dirty/untracked paths too,
    # so repeated edits remain observable even while their XY status is stable.
    records = status.stdout.decode("utf-8", errors="surrogateescape").split("\0")
    for record in records:
        if not record:
            continue
        spelled = record[3:] if len(record) >= 4 and record[2] == " " else record
        if not spelled or spelled.startswith(".lemoncrow/"):
            continue
        candidate = repo_root / spelled
        try:
            info = candidate.stat()
        except OSError:
            continue
        digest.update(spelled.encode("utf-8", errors="surrogateescape"))
        digest.update(b"\0")
        digest.update(_stat_identity(info).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _iter_git_paths(repo_root: Path) -> Iterator[str]:
    """Stream the worktree's tracked + non-ignored untracked file paths.

    Git is already a Runtime Lite prerequisite because lc with requires a Git
    worktree. Streaming avoids materializing a million-path repository in RAM.
    """

    command = [
        "git",
        "-c",
        "core.quotepath=false",
        "ls-files",
        "-co",
        "--exclude-standard",
        "-z",
        "--",
        ".",
        ":(exclude).lemoncrow",
    ]
    try:
        process = subprocess.Popen(
            command,
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise RuntimeError(f"could not enumerate Runtime Lite worktree: {exc}") from exc

    assert process.stdout is not None
    buffer = b""
    try:
        while True:
            chunk = process.stdout.read(64 * 1024)
            if not chunk:
                break
            buffer += chunk
            pieces = buffer.split(b"\x00")
            buffer = pieces.pop()
            for raw in pieces:
                if raw:
                    yield raw.decode("utf-8", errors="surrogateescape")
        if buffer:
            yield buffer.decode("utf-8", errors="surrogateescape")
    finally:
        process.stdout.close()

    stderr = b""
    if process.stderr is not None:
        stderr = process.stderr.read()
        process.stderr.close()
    return_code = process.wait()
    if return_code != 0:
        message = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"git ls-files failed for Runtime Lite: {message or return_code}")


def _count_git_paths(repo_root: Path) -> int:
    """How many entries :func:`_iter_git_paths` will yield, counted without keeping paths.

    Only progress reporting pays for this second ``git ls-files``; 0 means unknown.
    """

    try:
        process = subprocess.Popen(
            ["git", "ls-files", "-co", "--exclude-standard", "-z", "--", ".", ":(exclude).lemoncrow/workspace"],
            cwd=repo_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return 0
    assert process.stdout is not None
    count = 0
    trailing = b""
    with process.stdout:
        while chunk := process.stdout.read(64 * 1024):
            count += chunk.count(b"\x00")
            trailing = chunk[-1:]
    if process.wait() != 0:
        return 0
    return count + (1 if trailing and trailing != b"\x00" else 0)


def _git_value(repo_root: Path, *arguments: str) -> str | None:
    """One bounded Git scalar, or ``None`` when the checkout cannot answer."""

    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=5.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _git_paths(repo_root: Path, *arguments: str) -> frozenset[str] | None:
    """Return a NUL-delimited Git path result without ever guessing on failure."""

    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            timeout=10.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return frozenset(
        raw.decode("utf-8", errors="surrogateescape").replace("\\", "/") for raw in completed.stdout.split(b"\0") if raw
    )


def _worktree_changed_paths(repo_root: Path) -> frozenset[str] | None:
    """Tracked changes plus untracked files whose bytes are not proved by HEAD."""

    tracked = _git_paths(repo_root, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")
    untracked = _git_paths(repo_root, "ls-files", "--others", "--exclude-standard", "-z", "--")
    if tracked is None or untracked is None:
        return None
    return tracked | untracked


def _index_payload(path: Path, *, identity: str, size: int) -> tuple[str | None, str]:
    """Read at most one bounded file and return (text-for-FTS, durable digest)."""

    if size > _MAX_INDEX_FILE_BYTES:
        return None, f"stat:{identity}"
    try:
        data = path.read_bytes()
    except OSError:
        return None, f"stat:{identity}"
    digest = hashlib.sha256(data).hexdigest()
    if b"\x00" in data:
        return None, digest
    return data.decode("utf-8", errors="replace"), digest


def _read_text(path: Path) -> str | None:
    """Bounded source read for result snippets only."""

    try:
        info = path.stat()
    except OSError:
        return None
    text, _digest = _index_payload(path, identity=_stat_identity(info), size=int(info.st_size))
    return text


def _symbols(path: str, text: str) -> list[tuple[str, str, str, int, int, str]]:
    result: list[tuple[str, str, str, int, int, str]] = []
    python_source = Path(path).suffix.lower() in {".py", ".pyi"}
    for number, line in enumerate(text.splitlines(), start=1):
        if python_source and line == line.lstrip():
            assignment = _PY_TOP_ASSIGN.match(line)
            if assignment:
                result.append((assignment.group(1), "binding", path, number, number, line.strip()[:500]))
                continue
        for kind, pattern in _SYMBOL_PATTERNS:
            match = pattern.search(line)
            if match:
                result.append((match.group(1), kind, path, number, number, line.strip()[:500]))
                break
    return result


def _imports(path: str, text: str) -> list[tuple[str, str, str, int]]:
    return extract_imports(path, text)


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _import_target_candidates(source_path: str, module: str) -> tuple[str, ...]:
    """Return cheap exact path guesses for a parsed local import module."""

    raw = module.strip()
    if not raw:
        return ()
    source = Path(source_path)
    suffix = source.suffix.lower()
    candidates: list[str] = []

    def add(stem: Path | str, extensions: Sequence[str], *, package_indexes: Sequence[str] = ()) -> None:
        normalized = Path(stem).as_posix().lstrip("./")
        if not normalized or normalized.startswith("../"):
            return
        for extension in extensions:
            candidates.append(normalized + extension)
        for package_index in package_indexes:
            candidates.append(f"{normalized}/{package_index}")

    if suffix in {".py", ".pyi"} and raw.startswith("."):
        # A relative import names a module under the importing file's package.
        level = len(raw) - len(raw.lstrip("."))
        package = source.parent
        for _ in range(level - 1):
            if package == package.parent:
                return ()
            package = package.parent
        add(package / raw[level:].replace(".", "/"), (".py", ".pyi"), package_indexes=("__init__.py", "__init__.pyi"))
    elif suffix in {".py", ".pyi"}:
        # Parsed Python imports are dotted absolute modules. Also try the source
        # directory for small projects that use a flat local module layout.
        python_stem = raw.replace(".", "/")
        add(python_stem, (".py", ".pyi"), package_indexes=("__init__.py", "__init__.pyi"))
        if "." not in raw:
            add(source.parent / python_stem, (".py", ".pyi"), package_indexes=("__init__.py", "__init__.pyi"))
    elif suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"} and raw.startswith("."):
        # Relative JS/TS imports resolve from the importing file's directory.
        javascript_stem = source.parent / raw
        add(
            javascript_stem,
            ("", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"),
            package_indexes=("index.ts", "index.tsx", "index.js", "index.jsx"),
        )
    elif suffix == ".rs":
        rust_stem = raw.replace("::", "/")
        for prefix in ("crate/", "self/"):
            if rust_stem.startswith(prefix):
                rust_stem = rust_stem[len(prefix) :]
        add(rust_stem, (".rs",), package_indexes=("mod.rs",))
        add(Path("src") / rust_stem, (".rs",), package_indexes=("mod.rs",))
    # Preserve order but avoid redundant SQLite IN parameters.
    return tuple(dict.fromkeys(candidates))


def _normalized_path_parts(path: str) -> tuple[str, str]:
    lowered = path.lower().replace("\\", "/")
    return lowered, lowered.rsplit("/", 1)[-1]


def _is_test_path(path: str) -> bool:
    lowered, name = _normalized_path_parts(path)
    return (
        "/tests/" in f"/{lowered}"
        or "/test/" in f"/{lowered}"
        or name.startswith("test_")
        or name.startswith("test.")
        or ".test." in name
        or ".spec." in name
        or name.endswith("_test.py")
        or name.endswith("_test.go")
    )


def _is_docs_path(path: str) -> bool:
    lowered, name = _normalized_path_parts(path)
    return (
        lowered.startswith(("docs/", "doc/"))
        or "/docs/" in f"/{lowered}"
        or "/doc/" in f"/{lowered}"
        or name.endswith((".md", ".rst"))
    )


def _is_vendor_path(path: str) -> bool:
    lowered, name = _normalized_path_parts(path)
    return (
        "/vendor/" in f"/{lowered}"
        or "/third_party/" in f"/{lowered}"
        or "/node_modules/" in f"/{lowered}"
        or ".min." in name
    )


class LiteIndex:
    """One repository's durable, bounded-memory Runtime Lite index."""

    def __init__(self, repo_root: Path, *, stale_while_refresh: bool = False) -> None:
        self.repo_root = repo_root.expanduser().resolve()
        self.store_dir = self.repo_root / _INDEX_DIR
        self.db_path = self.store_dir / _DB_NAME
        self._query_cache: OrderedDict[tuple[str, tuple[str, ...], int, int], dict[str, Any]] = OrderedDict()
        self._last_refresh_check = 0.0
        self._known_generation = -1
        self._worktree_signature: str | None = None
        self._dirty = True
        self._must_refresh = False
        self._stale_while_refresh = stale_while_refresh
        self._state_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        self._refresh_lock = threading.Lock()
        self._freshness_lock = threading.Lock()
        self._timing_local = threading.local()

    def _timing_reset(self) -> None:
        self._timing_local.values = {
            "git_freshness_ms": 0.0,
            "index_refresh_ms": 0.0,
            "sqlite_wait_ms": 0.0,
            "sqlite_open_ms": 0.0,
            "query_ms": 0.0,
        }

    def _timing_add(self, key: str, elapsed_s: float) -> None:
        values = getattr(self._timing_local, "values", None)
        if isinstance(values, dict):
            values[key] = float(values.get(key, 0.0)) + max(0.0, elapsed_s * 1000.0)

    def _timing_set(self, key: str, value: int | float) -> None:
        values = getattr(self._timing_local, "values", None)
        if isinstance(values, dict):
            values[key] = max(0.0, float(value))

    def timing_snapshot(self) -> dict[str, float]:
        values = getattr(self._timing_local, "values", None)
        return dict(values) if isinstance(values, dict) else {}

    def _adopt_legacy_database(self) -> None:
        """Seed a versioned index from a transactionally consistent legacy copy.

        A previous Runtime Lite may still be reading/writing the old database.
        Never rename, delete or upgrade it: copy via SQLite online backup to a
        temporary file, then install with atomic no-overwrite hardlink. If a
        peer wins the race or the legacy index is unusable, rebuild normally.
        """
        legacy = self.store_dir / _LEGACY_DB_NAME
        if self.db_path.exists() or not legacy.is_file():
            return
        candidate: Path | None = None
        try:
            with contextlib.closing(sqlite3.connect(legacy, timeout=5.0)) as old:
                row = old.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if row is None or int(row[0]) not in (1, 2, _SCHEMA_VERSION):
                    return
                fd, name = tempfile.mkstemp(prefix=".lite-index-copy-", suffix=".sqlite3", dir=self.store_dir)
                os.close(fd)
                candidate = Path(name)
                with contextlib.closing(sqlite3.connect(candidate)) as fresh:
                    old.backup(fresh)
            try:
                os.link(candidate, self.db_path)
            except FileExistsError:
                pass  # A second session finished the same migration first.
        except (OSError, sqlite3.Error, ValueError):
            # Corrupt/missing legacy data is a cold-index condition, not a tool failure.
            return
        finally:
            if candidate is not None:
                with contextlib.suppress(OSError):
                    candidate.unlink()

    def _connect(self, *, initialize: bool = False, readonly: bool = False) -> sqlite3.Connection:
        opened = time.perf_counter()
        self.store_dir.mkdir(parents=True, exist_ok=True)
        if initialize:
            self._adopt_legacy_database()
        connection = sqlite3.connect(self.db_path, timeout=600.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=600000")
        if initialize:
            # WAL negotiation and schema DDL belong to the writer/bootstrap path,
            # never to hot read connections.
            deadline = time.monotonic() + 60.0
            while True:
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                        connection.close()
                        raise
                    if time.monotonic() >= deadline:
                        connection.close()
                        raise
                    time.sleep(0.05)
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA temp_store=FILE")
            try:
                self._ensure_schema(connection)
            except BaseException:
                connection.close()
                raise
        elif readonly:
            connection.execute("PRAGMA query_only=ON")
        self._timing_add("sqlite_open_ms", time.perf_counter() - opened)
        return connection

    @staticmethod
    def _ensure_schema(connection: sqlite3.Connection) -> None:
        # Reject a newer writer BEFORE executing any DDL. A stale MCP process
        # can coexist with an upgraded one; deleting/downgrading the shared
        # database would destroy the new process's durable index.
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():
            existing = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            if existing is not None and int(existing[0]) > _SCHEMA_VERSION:
                raise LiteIndexUpgradeRequired(
                    f"Runtime Lite index schema {existing[0]} is newer than this running tool "
                    f"(schema {_SCHEMA_VERSION}). Update LemonCrow and restart the coding-host "
                    "session to load the new MCP process. Do not delete the index."
                )
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY,
                identity TEXT NOT NULL,
                digest TEXT NOT NULL,
                size INTEGER NOT NULL,
                text_indexed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS symbols (
                name TEXT NOT NULL COLLATE NOCASE,
                kind TEXT NOT NULL,
                path TEXT NOT NULL,
                line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                signature TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS symbols_name_idx ON symbols(name COLLATE NOCASE);
            CREATE INDEX IF NOT EXISTS symbols_path_idx ON symbols(path);
            CREATE TABLE IF NOT EXISTS imports (
                path TEXT NOT NULL,
                module TEXT NOT NULL,
                imported TEXT NOT NULL,
                line INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS imports_module_idx ON imports(module);
            CREATE INDEX IF NOT EXISTS imports_path_idx ON imports(path);
            CREATE VIRTUAL TABLE IF NOT EXISTS file_fts USING fts5(
                path UNINDEXED,
                content,
                tokenize='porter unicode61'
            );
            """)
        schema = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if schema is None:
            connection.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(_SCHEMA_VERSION),),
            )
            connection.execute("INSERT INTO meta(key, value) VALUES('generation', '0')")
        else:
            version = int(schema["value"])
            if version == 2:
                # v3 changes what import rows hold. Keep serving the v2 rows and
                # make the next prepare() re-read every file once.
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if current is not None and int(current["value"]) == 2:
                    connection.execute("UPDATE files SET identity=''")
                    connection.execute(
                        "UPDATE meta SET value=? WHERE key='schema_version'",
                        (str(_SCHEMA_VERSION),),
                    )
                    connection.execute("UPDATE meta SET value='' WHERE key='worktree_signature'")
                connection.commit()
            elif version == 1:
                # v2 changes lexical tokenization and source-derived symbol rows.
                # Serialize the one-time rebuild so concurrent Runtime Lite
                # startups cannot race the migration.
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if current is not None and int(current["value"]) == 1:
                    connection.execute("DROP TABLE IF EXISTS file_fts")
                    connection.execute("""
                        CREATE VIRTUAL TABLE file_fts USING fts5(
                            path UNINDEXED,
                            content,
                            tokenize='porter unicode61'
                        )
                        """)
                    connection.execute("DELETE FROM symbols")
                    connection.execute("DELETE FROM imports")
                    connection.execute("DELETE FROM files")
                    connection.execute(
                        "UPDATE meta SET value=? WHERE key='schema_version'",
                        (str(_SCHEMA_VERSION),),
                    )
                    connection.execute("UPDATE meta SET value='0' WHERE key='generation'")
                    connection.execute("UPDATE meta SET value='' WHERE key='worktree_signature'")
                connection.commit()
            elif version != _SCHEMA_VERSION:
                raise RuntimeError(
                    f"Runtime Lite index schema {schema['value']} is unsupported "
                    f"(expected {_SCHEMA_VERSION}); update LemonCrow and restart the coding host. "
                    "Preserve the index for diagnosis."
                )
        connection.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('worktree_signature', '')")
        connection.commit()

    @staticmethod
    def _generation(connection: sqlite3.Connection) -> int:
        row = connection.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
        return int(row["value"]) if row is not None else 0

    def generation(self) -> int:
        if not self.db_path.exists():
            return 0
        try:
            with self._connect(readonly=True) as connection:
                return self._generation(connection)
        except sqlite3.OperationalError:
            return 0

    def mark_dirty(self) -> None:
        with self._state_lock:
            self._dirty = True
            self._must_refresh = True
            self._last_refresh_check = 0.0

    def _set_adopted_state(self, *, generation: int, signature: str, dirty: bool) -> None:
        with self._state_lock:
            self._known_generation = generation
            self._worktree_signature = signature or None
            self._dirty = dirty
            self._must_refresh = False
            self._last_refresh_check = time.monotonic()

    def refresh_needed(self) -> bool:
        """Whether a warm generation should be refreshed off the request path."""

        with self._state_lock:
            return bool(self._dirty and self._known_generation > 0 and self._stale_while_refresh)

    def refresh_if_needed(self) -> LiteIndexReport | None:
        """Refresh a warm dirty generation; safe to run in an owned worker pool."""

        if not self.refresh_needed():
            return None
        try:
            return self.prepare()
        except (OSError, RuntimeError, sqlite3.Error):
            # Keep the last committed WAL generation queryable and let a later
            # request retry. A refresh failure must never take Runtime Lite down.
            with self._state_lock:
                self._dirty = True
                self._last_refresh_check = 0.0
            return None

    def close(self) -> None:
        """Compatibility hook; LiteIndex owns no background worker or open handle."""

        return

    def adopt_prepared(self, *, progress: ProgressCallback | None = None) -> LiteIndexReport:
        """Adopt a durable generation; warm Runtime Lite validates it off the request path.

        With no usable generation yet it builds one, reporting to ``progress``.
        """

        started = time.monotonic()
        if not self.db_path.exists():
            return self.prepare(progress=progress)
        try:
            with self._connect(readonly=True) as connection:
                schema_row = connection.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                if schema_row is None or int(schema_row["value"]) != _SCHEMA_VERSION:
                    return self.prepare(progress=progress)
                generation = self._generation(connection)
                row = connection.execute("SELECT value FROM meta WHERE key='worktree_signature'").fetchone()
                stored = str(row["value"]) if row is not None else ""
                files_seen = int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
        except (sqlite3.OperationalError, ValueError):
            return self.prepare(progress=progress)

        if generation <= 0 or not stored:
            return self.prepare(progress=progress)

        if self._stale_while_refresh:
            # MCP initialize must not pay even the git-status freshness proof.
            # The previous WAL generation is immediately queryable; the owning
            # MCP refresh lane validates it after initialize returns.
            self._set_adopted_state(generation=generation, signature=stored, dirty=True)
        else:
            signature_started = time.perf_counter()
            current = _worktree_signature(self.repo_root)
            self._timing_add("git_freshness_ms", time.perf_counter() - signature_started)
            if current is None or current != stored:
                return self.prepare(progress=progress)
            self._set_adopted_state(generation=generation, signature=current, dirty=False)
        return LiteIndexReport(
            path=self.db_path,
            generation=generation,
            files_seen=files_seen,
            files_changed=0,
            files_deleted=0,
            cold=False,
            elapsed_s=time.monotonic() - started,
        )

    def _seed_candidates(self) -> tuple[tuple[Path, Path], ...]:
        """Fresh linked-checkout indexes that can cheaply seed this checkout."""

        target_head = _git_value(self.repo_root, "rev-parse", "--verify", "HEAD^{commit}") or ""
        ranked: list[tuple[int, float, Path, Path]] = []
        for entry in WorktreeCatalog(self.repo_root).entries():
            root = entry.root.resolve()
            if root == self.repo_root:
                continue
            db_path = root / _INDEX_DIR / _DB_NAME
            try:
                mtime = db_path.stat().st_mtime
            except OSError:
                continue
            source_head = _git_value(root, "rev-parse", "--verify", "HEAD^{commit}") or ""
            ranked.append((0 if target_head and source_head == target_head else 1, -mtime, root, db_path))
        ranked.sort(key=lambda item: (item[0], item[1], str(item[2])))
        return tuple((root, db_path) for _same_head, _mtime, root, db_path in ranked)

    def _seed_from_source(self, connection: sqlite3.Connection, source_root: Path, source_db: Path) -> int:
        """Reuse proved rows from another linked checkout without reading source bytes.

        A row is reusable only when the source index exactly matches its current
        checkout, both paths are clean relative to their own HEADs, and the two
        committed trees agree for that path. The copied analysis is therefore
        byte-identical; only the child checkout's stat identity is rewritten.
        """

        source_signature = _worktree_signature(source_root)
        source_head = _git_value(source_root, "rev-parse", "--verify", "HEAD^{commit}")
        target_head = _git_value(self.repo_root, "rev-parse", "--verify", "HEAD^{commit}")
        source_changed = _worktree_changed_paths(source_root)
        target_changed = _worktree_changed_paths(self.repo_root)
        if (
            not source_signature
            or not source_head
            or not target_head
            or source_changed is None
            or target_changed is None
        ):
            return 0
        revision_changed = _git_paths(
            self.repo_root,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            source_head,
            target_head,
            "--",
        )
        if revision_changed is None:
            return 0
        excluded = source_changed | target_changed | revision_changed

        attached = False
        try:
            connection.execute("ATTACH DATABASE ? AS lite_seed", (str(source_db),))
            attached = True
            connection.execute("BEGIN")
            version = connection.execute("SELECT value FROM lite_seed.meta WHERE key='schema_version'").fetchone()
            generation = connection.execute("SELECT value FROM lite_seed.meta WHERE key='generation'").fetchone()
            stored_signature = connection.execute(
                "SELECT value FROM lite_seed.meta WHERE key='worktree_signature'"
            ).fetchone()
            if (
                version is None
                or int(version[0]) != _SCHEMA_VERSION
                or generation is None
                or int(generation[0]) <= 0
                or stored_signature is None
                or str(stored_signature[0]) != source_signature
            ):
                connection.rollback()
                return 0

            connection.execute("DROP TABLE IF EXISTS temp.lite_seed_paths")
            connection.execute(
                "CREATE TEMP TABLE lite_seed_paths(path TEXT PRIMARY KEY, identity TEXT NOT NULL) WITHOUT ROWID"
            )
            batch: list[tuple[str, str]] = []
            seeded = 0
            for row in connection.execute("SELECT path FROM lite_seed.files ORDER BY path"):
                relpath = str(row[0])
                if relpath in excluded:
                    continue
                absolute = self.repo_root / relpath
                try:
                    info = absolute.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                batch.append((relpath, _stat_identity(info)))
                if len(batch) >= 1024:
                    connection.executemany("INSERT INTO lite_seed_paths(path, identity) VALUES(?, ?)", batch)
                    seeded += len(batch)
                    batch.clear()
            if batch:
                connection.executemany("INSERT INTO lite_seed_paths(path, identity) VALUES(?, ?)", batch)
                seeded += len(batch)
            if not seeded:
                connection.rollback()
                return 0

            connection.execute("""
                INSERT INTO files(path, identity, digest, size, text_indexed)
                SELECT f.path, p.identity, f.digest, f.size, f.text_indexed
                FROM lite_seed.files AS f
                JOIN lite_seed_paths AS p ON p.path=f.path
                """)
            connection.execute("""
                INSERT INTO symbols(name, kind, path, line, end_line, signature)
                SELECT s.name, s.kind, s.path, s.line, s.end_line, s.signature
                FROM lite_seed.symbols AS s
                JOIN lite_seed_paths AS p ON p.path=s.path
                """)
            connection.execute("""
                INSERT INTO imports(path, module, imported, line)
                SELECT i.path, i.module, i.imported, i.line
                FROM lite_seed.imports AS i
                JOIN lite_seed_paths AS p ON p.path=i.path
                """)
            connection.execute("""
                INSERT INTO file_fts(path, content)
                SELECT f.path, f.content
                FROM lite_seed.file_fts AS f
                JOIN lite_seed_paths AS p ON p.path=f.path
                """)
            if _worktree_signature(source_root) != source_signature:
                connection.rollback()
                return 0
            connection.execute("UPDATE meta SET value='' WHERE key='worktree_signature'")
            connection.commit()
            return seeded
        except (OSError, sqlite3.Error, ValueError):
            with contextlib.suppress(sqlite3.Error):
                connection.rollback()
            return 0
        finally:
            if attached:
                with contextlib.suppress(sqlite3.Error):
                    connection.execute("DETACH DATABASE lite_seed")

    def _seed_from_linked_worktree(self, connection: sqlite3.Connection) -> int:
        """Populate an empty index from the freshest compatible linked checkout."""

        if self._generation(connection) != 0 or int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]):
            return 0
        for source_root, source_db in self._seed_candidates():
            seeded = self._seed_from_source(connection, source_root, source_db)
            if seeded:
                return seeded
        return 0

    def prepare(self, *, force: bool = False, progress: ProgressCallback | None = None) -> LiteIndexReport:
        """Incrementally bring the durable index to the current worktree state.

        ``progress`` receives throttled :class:`LiteIndexProgress` observations.
        An exception it raises aborts the build and rolls the transaction back.

        lc-debt: a build is one transaction, so a process stopped mid-build (host
        session closed) keeps nothing and the next start walks from zero. Upgrade:
        commit file rows in batches under an unpublished generation.
        """

        with self._refresh_lock:
            return self._prepare_locked(force=force, progress=progress)

    def _prepare_locked(self, *, force: bool, progress: ProgressCallback | None) -> LiteIndexReport:
        if not (self.repo_root / ".git").exists():
            raise RuntimeError(f"Runtime Lite requires a Git worktree: {self.repo_root}")
        started = time.monotonic()
        reporter = _ProgressEmitter(progress, started)
        _ensure_workspace_git_excluded(self.repo_root)
        if not force:
            reporter.emit("seed", now=True)
        connection = self._connect(initialize=True)
        if not force:
            self._seed_from_linked_worktree(connection)
        files_seen = 0
        deleted = 0
        changed = 0
        walked = 0
        total = 0
        cold = False
        generation = 0
        stable_signature = ""
        try:
            reporter.emit("lock", now=True)
            # One writer owns a complete generation. WAL readers remain on the
            # last committed generation while this transaction is in progress.
            writer_wait_started = time.perf_counter()
            connection.execute("BEGIN IMMEDIATE")
            self._timing_add("sqlite_wait_ms", time.perf_counter() - writer_wait_started)
            generation = self._generation(connection)
            cold = generation == 0
            signature_started = time.perf_counter()
            signature_before = _worktree_signature(self.repo_root)
            self._timing_add("git_freshness_ms", time.perf_counter() - signature_started)
            total = _count_git_paths(self.repo_root) if reporter.active else 0
            reporter.emit("scan", 0, total, 0, now=True)

            # A competing Runtime Lite process may have refreshed this exact
            # worktree while we waited for BEGIN IMMEDIATE. Re-prove freshness
            # after taking the writer lock and skip the O(repo) walk entirely.
            stored_row = connection.execute("SELECT value FROM meta WHERE key='worktree_signature'").fetchone()
            stored_signature = str(stored_row["value"]) if stored_row is not None else ""
            if (
                not force
                and generation > 0
                and signature_before is not None
                and stored_signature
                and stored_signature == signature_before
            ):
                files_seen = int(connection.execute("SELECT COUNT(*) FROM files").fetchone()[0])
                connection.commit()
                self._set_adopted_state(
                    generation=generation,
                    signature=signature_before,
                    dirty=False,
                )
                self._timing_add("index_refresh_ms", time.monotonic() - started)
                return LiteIndexReport(
                    path=self.db_path,
                    generation=generation,
                    files_seen=files_seen,
                    files_changed=0,
                    files_deleted=0,
                    cold=False,
                    elapsed_s=time.monotonic() - started,
                )
            # Keep scan membership on SQLite temp storage, not in a Python set.
            # This is the key bounded-memory property for million-file repos.
            connection.execute("DROP TABLE IF EXISTS temp.lite_seen")
            connection.execute("CREATE TEMP TABLE lite_seen(path TEXT PRIMARY KEY) WITHOUT ROWID")

            for relpath in _iter_git_paths(self.repo_root):
                walked += 1
                reporter.emit("scan", walked, max(total, walked) if total else 0, changed)
                absolute = self.repo_root / relpath
                try:
                    info = absolute.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue

                files_seen += 1
                connection.execute(
                    "INSERT OR IGNORE INTO lite_seen(path) VALUES(?)",
                    (relpath,),
                )
                identity = _stat_identity(info)
                previous = connection.execute(
                    "SELECT identity FROM files WHERE path=?",
                    (relpath,),
                ).fetchone()
                if not force and previous is not None and str(previous["identity"]) == identity:
                    continue

                text, digest = _index_payload(
                    absolute,
                    identity=identity,
                    size=int(info.st_size),
                )
                connection.execute("DELETE FROM file_fts WHERE path=?", (relpath,))
                connection.execute("DELETE FROM symbols WHERE path=?", (relpath,))
                connection.execute("DELETE FROM imports WHERE path=?", (relpath,))
                text_indexed = 0
                if text is not None:
                    connection.execute(
                        "INSERT INTO file_fts(path, content) VALUES(?, ?)",
                        (relpath, text),
                    )
                    connection.executemany(
                        "INSERT INTO symbols(name, kind, path, line, end_line, signature) VALUES(?,?,?,?,?,?)",
                        _symbols(relpath, text),
                    )
                    connection.executemany(
                        "INSERT INTO imports(path, module, imported, line) VALUES(?,?,?,?)",
                        _imports(relpath, text),
                    )
                    text_indexed = 1
                connection.execute(
                    """
                    INSERT INTO files(path, identity, digest, size, text_indexed)
                    VALUES(?,?,?,?,?)
                    ON CONFLICT(path) DO UPDATE SET
                        identity=excluded.identity,
                        digest=excluded.digest,
                        size=excluded.size,
                        text_indexed=excluded.text_indexed
                    """,
                    (relpath, identity, digest, int(info.st_size), text_indexed),
                )
                changed += 1

            reporter.emit("scan", walked, max(total, walked), changed, now=True)
            reporter.emit("prune", walked, max(total, walked), changed, now=True)
            connection.execute("DROP TABLE IF EXISTS temp.lite_deleted")
            connection.execute("CREATE TEMP TABLE lite_deleted(path TEXT PRIMARY KEY) WITHOUT ROWID")
            connection.execute("""
                INSERT INTO lite_deleted(path)
                SELECT files.path
                FROM files
                LEFT JOIN lite_seen ON lite_seen.path=files.path
                WHERE lite_seen.path IS NULL
                """)
            deleted = int(connection.execute("SELECT COUNT(*) FROM lite_deleted").fetchone()[0])
            if deleted:
                connection.execute("DELETE FROM file_fts WHERE path IN (SELECT path FROM lite_deleted)")
                connection.execute("DELETE FROM symbols WHERE path IN (SELECT path FROM lite_deleted)")
                connection.execute("DELETE FROM imports WHERE path IN (SELECT path FROM lite_deleted)")
                connection.execute("DELETE FROM files WHERE path IN (SELECT path FROM lite_deleted)")

            if changed or deleted or cold:
                generation += 1
                connection.execute(
                    "UPDATE meta SET value=? WHERE key='generation'",
                    (str(generation),),
                )

            signature_started = time.perf_counter()
            signature_after = _worktree_signature(self.repo_root)
            self._timing_add("git_freshness_ms", time.perf_counter() - signature_started)
            stable_signature = (
                signature_after if signature_before is not None and signature_before == signature_after else ""
            )
            connection.execute(
                "UPDATE meta SET value=? WHERE key='worktree_signature'",
                (stable_signature,),
            )
            reporter.emit("commit", walked, max(total, walked), changed, now=True)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

        with self._state_lock:
            self._known_generation = generation
            self._dirty = not bool(stable_signature)
            self._must_refresh = False
            self._last_refresh_check = time.monotonic()
            self._worktree_signature = stable_signature or None
        with self._cache_lock:
            self._query_cache.clear()
        self._timing_add("index_refresh_ms", time.monotonic() - started)
        reporter.emit("done", walked, max(total, walked), changed, now=True)
        return LiteIndexReport(
            path=self.db_path,
            generation=generation,
            files_seen=files_seen,
            files_changed=changed,
            files_deleted=deleted,
            cold=cold,
            elapsed_s=time.monotonic() - started,
        )

    def maybe_refresh(self) -> LiteIndexReport | None:
        """Keep the index fresh without making warm concurrent readers queue."""

        now = time.monotonic()
        with self._state_lock:
            dirty = self._dirty
            must_refresh = self._must_refresh
            last_check = self._last_refresh_check
            known_generation = self._known_generation
            known_signature = self._worktree_signature

        if dirty:
            # Mutations performed through this MCP must be visible to the very
            # next indexed call unless the owning MCP has already started a
            # refresh. External edits may use the last committed WAL generation
            # while that owned refresh catches up.
            if must_refresh or known_generation <= 0 or not self._stale_while_refresh:
                return self.prepare()
            return None

        if now - last_check < _REFRESH_INTERVAL_S:
            return None

        if not self._freshness_lock.acquire(blocking=False):
            # One sibling request is already paying for git status. A warm WAL
            # generation is safe to serve immediately.
            if known_generation > 0:
                return None
            with self._freshness_lock:
                pass
            return self.maybe_refresh()

        try:
            # Double-check after winning singleflight; another request may have
            # completed a refresh while this one was waiting to run.
            now = time.monotonic()
            with self._state_lock:
                if not self._dirty and now - self._last_refresh_check < _REFRESH_INTERVAL_S:
                    return None
                known_signature = self._worktree_signature
                known_generation = self._known_generation

            signature_started = time.perf_counter()
            signature = _worktree_signature(self.repo_root)
            self._timing_add("git_freshness_ms", time.perf_counter() - signature_started)
            with self._state_lock:
                self._last_refresh_check = now
                if signature is not None and signature == known_signature:
                    self._dirty = False
                    return None
                self._dirty = True
                self._must_refresh = False

            if self._stale_while_refresh and known_generation > 0:
                return None
            return self.prepare()
        finally:
            self._freshness_lock.release()

    @staticmethod
    def _path_allowed(path: str, scopes: Sequence[str]) -> bool:
        if not scopes:
            return True
        normalized = path.replace("\\", "/")
        for scope in scopes:
            clean = scope.strip().replace("\\", "/").strip("/")
            if not clean or clean == ".":
                return True
            if normalized == clean or normalized.startswith(clean + "/"):
                return True
        return False

    def search(
        self,
        query: str,
        *,
        paths: str | Sequence[str] | None = None,
        limit: int = _DEFAULT_RESULTS,
    ) -> dict[str, Any]:
        self._timing_reset()
        query_started = time.perf_counter()
        self.maybe_refresh()
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        limit = max(1, min(int(limit or _DEFAULT_RESULTS), _MAX_RESULTS))
        scopes: tuple[str, ...]
        if isinstance(paths, str):
            scopes = (paths,)
        elif paths is None:
            scopes = ()
        else:
            scopes = tuple(str(path) for path in paths)

        tokens = _tokens(query)
        if not tokens:
            tokens = _IDENTIFIER.findall(query)[:4]
        natural_language = (
            len(tokens) >= 3 and bool(re.search(r"\s", query)) and not re.search(r"[|\\^$()\[\]{}=]|::|->", query)
        )
        symbol_base_score = 32.0 if natural_language else 170.0
        scores: dict[str, float] = {}
        channels: dict[str, set[str]] = {}
        lines: dict[str, int] = {}
        matched_terms: dict[str, set[str]] = {}
        seen_evidence: set[tuple[str, str, str]] = set()
        import_target_evidence: dict[str, set[str]] = {}

        def add(
            path: str,
            score: float,
            channel: str,
            line: int = 1,
            *,
            term: str | None = None,
        ) -> None:
            if not self._path_allowed(path, scopes):
                return
            normalized_term = term.lower() if term else ""
            if normalized_term:
                evidence_key = (path, channel, normalized_term)
                if evidence_key in seen_evidence:
                    return
                seen_evidence.add(evidence_key)
            scores[path] = scores.get(path, 0.0) + score
            channels.setdefault(path, set()).add(channel)
            if normalized_term:
                matched_terms.setdefault(path, set()).add(normalized_term)
            if line > 0 and (path not in lines or channel.startswith("symbol")):
                lines[path] = line

        with self._connect(readonly=True) as connection:
            generation = self._generation(connection)
            with self._state_lock:
                stale_served = bool(self._dirty and generation > 0)
            self._timing_set("index_generation", generation)
            self._timing_set("stale_served", 1 if stale_served else 0)
            key = (query, scopes, limit, generation)
            with self._cache_lock:
                cached = self._query_cache.get(key)
                if cached is not None:
                    self._query_cache.move_to_end(key)
            if cached is not None:
                self._timing_add("query_ms", time.perf_counter() - query_started)
                return cached

            for token_index, token in enumerate(tokens):
                rows = connection.execute(
                    """
                    SELECT name, kind, path, line
                    FROM symbols
                    WHERE name=? COLLATE NOCASE
                    ORDER BY CASE WHEN name=? THEN 0 ELSE 1 END, path, line
                    LIMIT 64
                    """,
                    (token, token),
                ).fetchall()
                for row in rows:
                    row_kind = str(row["kind"])
                    base_score = 24.0 if natural_language and row_kind == "binding" else symbol_base_score
                    if not natural_language and row_kind == "binding":
                        base_score = 120.0
                    add(
                        str(row["path"]),
                        base_score - token_index * 2.0,
                        "symbol",
                        int(row["line"]),
                        term=token,
                    )

            fuzzy_tokens = tokens[:8] if natural_language else tokens[:4]
            for token_index, token in enumerate(fuzzy_tokens):
                min_length = 3 if natural_language else 4
                if len(token) < min_length:
                    continue
                token_lower = token.lower()
                stem = token_lower[:4] if natural_language and len(token_lower) >= 7 else token_lower
                rows = connection.execute(
                    """
                    SELECT name, path, line
                    FROM symbols
                    WHERE lower(name) LIKE ?
                    ORDER BY
                        CASE WHEN lower(name) LIKE ? THEN 0 ELSE 1 END,
                        length(name), path, line
                    LIMIT 48
                    """,
                    (f"%{stem}%", f"%{token_lower}%"),
                ).fetchall()
                for rank, row in enumerate(rows):
                    full_match = token_lower in str(row["name"]).lower()
                    base_score = 24.0 if full_match else 9.0
                    add(
                        str(row["path"]),
                        base_score - token_index * 0.4 - min(rank, 40) * 0.15,
                        "symbol-part",
                        int(row["line"]),
                        term=token,
                    )

            for token_index, token in enumerate(tokens[:6]):
                if len(token) < 3:
                    continue
                needle = f"%{token.lower()}%"
                rows = connection.execute(
                    "SELECT path FROM files WHERE lower(path) LIKE ? ORDER BY length(path), path LIMIT 64",
                    (needle,),
                ).fetchall()
                for rank, row in enumerate(rows):
                    add(
                        str(row["path"]),
                        48.0 - token_index - rank * 0.15,
                        "path",
                        term=token,
                    )

            if tokens:
                expression = " OR ".join(_safe_fts_token(token) for token in tokens)
                try:
                    rows = connection.execute(
                        """
                        SELECT path, bm25(file_fts) AS rank
                        FROM file_fts
                        WHERE file_fts MATCH ?
                        ORDER BY rank, path
                        LIMIT ?
                        """,
                        (expression, max(64, limit * 8)),
                    ).fetchall()
                except sqlite3.OperationalError:
                    rows = []
                strengths = [max(0.0, -float(row["rank"] or 0.0)) for row in rows]
                best_strength = max(strengths, default=0.0)
                for position, (row, strength) in enumerate(zip(rows, strengths, strict=True)):
                    relative_strength = strength / best_strength if best_strength > 0.0 else 0.0
                    add(
                        str(row["path"]),
                        40.0 + 25.0 * relative_strength - min(position, 100) * 0.1,
                        "fts",
                    )

                # The broad OR query is efficient and remains the primary lexical
                # ordering, but its bounded candidate window can miss a file that
                # is strong for one rarer intent term. Pull a small top slice per
                # term and reward files corroborated by several distinct terms.
                # This stays disk-backed and bounded: at most 8 * 20 FTS rows.
                for token in tokens[:8]:
                    precise_identifier = not natural_language and _code_identifier_token(token)
                    match_snippet_sql = "snippet(file_fts, 1, '', '', '...', 24)" if precise_identifier else "''"
                    try:
                        term_rows = connection.execute(
                            f"""
                            SELECT
                                path,
                                bm25(file_fts) AS rank,
                                {match_snippet_sql} AS match_snippet
                            FROM file_fts
                            WHERE file_fts MATCH ?
                            ORDER BY rank, path
                            LIMIT 20
                            """,
                            (_safe_fts_token(token),),
                        ).fetchall()
                    except sqlite3.OperationalError:
                        term_rows = []
                    for rank, row in enumerate(term_rows):
                        path = str(row["path"])
                        add(
                            path,
                            8.0 - min(rank, 16) * 0.25,
                            "fts",
                            term=token,
                        )
                        if precise_identifier and _snippet_has_exact_identifier(str(row["match_snippet"] or ""), token):
                            add(path, 12.0, "identifier-exact", term=token)

            for token in tokens[:6]:
                rows = connection.execute(
                    """
                    SELECT path, module, imported, line FROM imports
                    WHERE lower(module)=lower(?)
                       OR lower(module) LIKE ?
                       OR (length(?) >= 4 AND lower(imported) LIKE ?)
                    ORDER BY path, line LIMIT 64
                    """,
                    (token, f"%.{token.lower()}", token, f"%{token.lower()}%"),
                ).fetchall()
                token_lower = token.lower()
                for row in rows:
                    source_path = str(row["path"])
                    module = str(row["module"])
                    module_lower = module.lower()
                    module_match = module_lower == token_lower or module_lower.endswith("." + token_lower)
                    add(
                        source_path,
                        30.0 if module_match else 16.0,
                        "import",
                        int(row["line"]),
                        term=token,
                    )
                    for candidate in _import_target_candidates(source_path, module):
                        import_target_evidence.setdefault(candidate, set()).add(token_lower)

            if import_target_evidence:
                target_paths = list(import_target_evidence)[:192]
                placeholders = ",".join("?" for _ in target_paths)
                target_rows = connection.execute(
                    f"SELECT path FROM files WHERE path IN ({placeholders})",
                    target_paths,
                ).fetchall()
                for row in target_rows:
                    target_path = str(row["path"])
                    for term in import_target_evidence[target_path]:
                        add(target_path, 34.0, "import-target", term=term)

            # Code-shaped queries that land on tests often name behavior rather
            # than the implementation file. Follow one parsed local import from
            # the strongest test candidates only; this stays query-local and
            # avoids perturbing ordinary symbol/content retrieval.
            seed_paths = [
                path for path in sorted(scores, key=lambda path: (-scores[path], path))[:10] if _is_test_path(path)
            ][:4]
            if seed_paths and not natural_language:
                placeholders = ",".join("?" for _ in seed_paths)
                import_rows = connection.execute(
                    f"""
                    SELECT path, module
                    FROM imports
                    WHERE path IN ({placeholders})
                    ORDER BY path, line
                    LIMIT 64
                    """,
                    seed_paths,
                ).fetchall()
                seed_rank = {path: rank for rank, path in enumerate(seed_paths)}
                neighbor_scores: dict[str, float] = {}
                for row in import_rows:
                    source_path = str(row["path"])
                    module = str(row["module"])
                    score = 14.0 - min(seed_rank.get(source_path, 3), 3) * 2.0
                    for candidate in _import_target_candidates(source_path, module):
                        if candidate == source_path:
                            continue
                        neighbor_scores[candidate] = max(neighbor_scores.get(candidate, 0.0), score)
                if neighbor_scores:
                    neighbor_paths = list(neighbor_scores)[:128]
                    placeholders = ",".join("?" for _ in neighbor_paths)
                    neighbor_rows = connection.execute(
                        f"SELECT path FROM files WHERE path IN ({placeholders})",
                        neighbor_paths,
                    ).fetchall()
                    for row in neighbor_rows:
                        neighbor_path = str(row["path"])
                        add(neighbor_path, neighbor_scores[neighbor_path], "test-import-neighbor")

            related_symbols = self._related_symbols_on_connection(connection, query, limit=12)

        token_count = max(1, len(tokens))
        for path, terms in matched_terms.items():
            coverage = len(terms)
            if coverage < 2:
                continue
            # Multi-term corroboration is especially useful for natural-language
            # and session-style queries. Cap the bonus so exact symbols and strong
            # lexical ranking remain authoritative.
            scores[path] += min(24.0, (coverage - 1) * 6.0 + 10.0 * coverage / token_count)

        if natural_language:
            query_terms = {token.lower() for token in _IDENTIFIER.findall(query)}
            wants_tests = bool(query_terms & {"test", "tests", "testing", "spec", "specs", "fixture", "fixtures"})
            wants_docs = bool(query_terms & {"doc", "docs", "documentation", "readme", "tutorial", "guide", "manual"})
            wants_vendor = bool(query_terms & {"vendor", "vendored", "thirdparty", "jquery", "minified"})
            for path in scores:
                if _is_test_path(path) and not wants_tests:
                    scores[path] -= 6.0
                if _is_docs_path(path) and not wants_docs:
                    scores[path] -= 5.0
                if _is_vendor_path(path) and not wants_vendor:
                    scores[path] -= 12.0

        candidate_limit = min(_MAX_RESULTS, max(16, limit * 3))
        ordered = sorted(scores, key=lambda path: (-scores[path], path))[:candidate_limit]
        files: list[dict[str, Any]] = []
        remaining_chars = _MAX_SNIPPET_CHARS
        for rank, path in enumerate(ordered[:limit], start=1):
            line = max(1, lines.get(path, 1))
            item: dict[str, Any] = {
                "path": path,
                "score": round(scores[path], 3),
                "channels": sorted(channels.get(path, ())),
                "line": line,
                "end_line": line,
            }
            if rank <= _SNIPPET_FILES and remaining_chars > 0:
                snippet, start_line, end_line = self._snippet(path, query, line)
                if snippet:
                    snippet = snippet[:remaining_chars]
                    remaining_chars -= len(snippet)
                    item["source"] = snippet
                    item["line"] = start_line
                    item["end_line"] = end_line
            files.append(item)

        result = {
            "query": query,
            "generation": generation,
            "files": files,
            "candidate_files": ordered[limit:],
            "related_symbols": related_symbols,
            "lite": True,
        }
        with self._cache_lock:
            self._query_cache[key] = result
            while len(self._query_cache) > _MAX_QUERY_CACHE:
                self._query_cache.popitem(last=False)
        self._timing_add("query_ms", time.perf_counter() - query_started)
        return result

    def _snippet(self, relpath: str, query: str, preferred_line: int) -> tuple[str, int, int]:
        text = _read_text(self.repo_root / relpath)
        if text is None:
            return "", preferred_line, preferred_line
        source_lines = text.splitlines()
        if not source_lines:
            return "", 1, 1
        hit = preferred_line
        if preferred_line <= 1:
            lowered = query.lower()
            tokens = [token.lower() for token in _tokens(query)]
            for number, line in enumerate(source_lines, start=1):
                line_lower = line.lower()
                if lowered in line_lower or any(token in line_lower for token in tokens):
                    hit = number
                    break
        half = _SNIPPET_LINES // 2
        start = max(1, hit - half)
        end = min(len(source_lines), start + _SNIPPET_LINES - 1)
        numbered = "\n".join(f"{number}\t{source_lines[number - 1]}" for number in range(start, end + 1))
        return numbered, start, end

    @staticmethod
    def _exact_symbols_on_connection(
        connection: sqlite3.Connection,
        requested: Sequence[str],
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for name in requested:
            rows = connection.execute(
                """
                SELECT name, kind, path, line, end_line, signature
                FROM symbols WHERE name=? COLLATE NOCASE
                ORDER BY path, line LIMIT ?
                """,
                (name, limit),
            ).fetchall()
            result.extend(dict(row) for row in rows)
        return result[:limit]

    def exact_symbols(self, names: str | Sequence[str], *, limit: int = 32) -> list[dict[str, Any]]:
        self.maybe_refresh()
        requested = [names] if isinstance(names, str) else [str(name) for name in names]
        with self._connect(readonly=True) as connection:
            return self._exact_symbols_on_connection(connection, requested, limit=limit)

    @staticmethod
    def _related_symbols_on_connection(
        connection: sqlite3.Connection,
        query: str,
        *,
        limit: int,
    ) -> list[dict[str, Any]]:
        tokens = _tokens(query)
        if not tokens:
            return []
        result: list[dict[str, Any]] = []
        seen: set[tuple[str, str, int]] = set()
        for token in tokens[:4]:
            rows = connection.execute(
                """
                SELECT name, kind, path, line FROM symbols
                WHERE name LIKE ? COLLATE NOCASE
                ORDER BY CASE WHEN lower(name)=lower(?) THEN 0 ELSE 1 END, length(name), path, line
                LIMIT ?
                """,
                (f"%{token}%", token, limit),
            ).fetchall()
            for row in rows:
                key = (str(row["name"]), str(row["path"]), int(row["line"]))
                if key in seen:
                    continue
                seen.add(key)
                result.append(dict(row))
                if len(result) >= limit:
                    return result
        return result

    def related_symbols(self, query: str, *, limit: int = 12, refresh: bool = True) -> list[dict[str, Any]]:
        if refresh:
            self.maybe_refresh()
        with self._connect(readonly=True) as connection:
            return self._related_symbols_on_connection(connection, query, limit=limit)

    def relations(self, symbol: str, *, kind: str = "usages", limit: int = 20) -> dict[str, Any]:
        self.maybe_refresh()
        limit = max(1, min(int(limit or 20), 100))
        if kind == "self":
            return {"symbol": symbol, "kind": kind, "items": self.exact_symbols(symbol, limit=limit), "lite": True}

        if kind == "imports":
            wanted = symbol.lower()
            with self._connect(readonly=True) as connection:
                rows = connection.execute(
                    """
                    SELECT path, module, imported, line FROM imports
                    WHERE lower(module)=lower(?) OR lower(module) LIKE ?
                    ORDER BY path, line LIMIT ?
                    """,
                    (symbol, f"%.{wanted}", limit),
                ).fetchall()
                items = [dict(row) for row in rows]
                if len(items) < limit and wanted:
                    # After every module match, the statements that import it by
                    # name (``from m import X``, ``import X from "m"``).
                    seen = {(item["path"], item["line"], item["module"]) for item in items}
                    named = connection.execute(
                        """
                        SELECT path, module, imported, line FROM imports
                        WHERE lower(imported) LIKE ? ESCAPE '\\'
                        ORDER BY path, line LIMIT ?
                        """,
                        (f"%{_like_escape(wanted)}%", _MAX_NAMED_IMPORT_ROWS),
                    ).fetchall()
                    for row in named:
                        key = (row["path"], row["line"], row["module"])
                        if key in seen or wanted not in {name.lower() for name in imported_names(row["imported"])}:
                            continue
                        seen.add(key)
                        items.append(dict(row))
                        if len(items) >= limit:
                            break
            return {"symbol": symbol, "kind": kind, "items": items, "lite": True}

        search = self.search(symbol, limit=limit)
        items = [
            {
                "path": item["path"],
                "line": item.get("line", 1),
                "end_line": item.get("end_line", item.get("line", 1)),
                "channels": item.get("channels", []),
            }
            for item in search["files"]
        ]
        return {
            "symbol": symbol,
            "kind": kind,
            "items": items,
            "relation_quality": "basic_lexical" if kind in {"callers", "callees"} else "usage",
            "lite": True,
        }

    def repo_map(self, *, kind: str = "map", path: str = "", limit: int = 50) -> dict[str, Any]:
        self.maybe_refresh()
        limit = max(1, min(int(limit or 50), 200))
        prefix = path.strip().strip("/")
        with self._connect(readonly=True) as connection:
            if kind == "imports":
                if prefix:
                    rows = connection.execute(
                        """
                        SELECT path, module, imported, line FROM imports
                        WHERE path=? OR path LIKE ?
                        ORDER BY path, line LIMIT ?
                        """,
                        (prefix, prefix + "/%", limit),
                    ).fetchall()
                else:
                    rows = connection.execute(
                        "SELECT path, module, imported, line FROM imports ORDER BY path, line LIMIT ?",
                        (limit,),
                    ).fetchall()
                return {"kind": kind, "items": [dict(row) for row in rows], "lite": True}

            if prefix:
                rows = connection.execute(
                    """
                    SELECT f.path,
                           COUNT(DISTINCT s.rowid) AS symbols,
                           COUNT(DISTINCT i.rowid) AS imports
                    FROM files f
                    LEFT JOIN symbols s ON s.path=f.path
                    LEFT JOIN imports i ON i.path=f.path
                    WHERE f.path=? OR f.path LIKE ?
                    GROUP BY f.path
                    ORDER BY symbols DESC, imports DESC, f.path
                    LIMIT ?
                    """,
                    (prefix, prefix + "/%", limit),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT f.path,
                           COUNT(DISTINCT s.rowid) AS symbols,
                           COUNT(DISTINCT i.rowid) AS imports
                    FROM files f
                    LEFT JOIN symbols s ON s.path=f.path
                    LEFT JOIN imports i ON i.path=f.path
                    GROUP BY f.path
                    ORDER BY symbols DESC, imports DESC, f.path
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        return {"kind": "map", "items": [dict(row) for row in rows], "lite": True}


def prepare_lite_workspace(repo_root: Path) -> dict[str, object]:
    """Public synchronous preparation used before a host is launched."""

    return LiteIndex(repo_root).prepare().to_dict()


__all__ = [
    "IMPORT_STATUSES",
    "ImportResolution",
    "ImportResolver",
    "LiteIndex",
    "LiteIndexProgress",
    "LiteIndexReport",
    "ProgressCallback",
    "extract_imports",
    "imported_names",
    "prepare_lite_workspace",
]
