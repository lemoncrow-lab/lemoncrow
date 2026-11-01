"""Deterministic `/code` data from the open-source Runtime Lite index.

Without the private code-intelligence engine, `/code` reads the Runtime Lite
index of the selected worktree (``<repo>/.lemoncrow/workspace/``): the tracked
files, their line-anchored symbols and their parsed import statements. The
adapter presents them in the shape :mod:`code_map` and :mod:`code_surface`
already consume, so the deterministic views share one implementation with the
private engine.

Relationships here are import-level only. Every relationship payload says so
-- ``relationship_basis: "imports"``, ``degraded: true`` and the reason
``lite_import_level`` -- and an import is never labelled a call or a reference.
Precise references stay with the private engine (LemonCrow Hosted).

Runtime Lite never flows through the private provider seam
(``code_intelligence_provider``); :func:`code_map.get_map_engine` chooses this
adapter only when that seam reports the private engine unavailable.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import logging
import re
import sqlite3
import subprocess
import threading
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Final

from lemoncrow.core.service.code_relations import balanced_relationship_rows

if TYPE_CHECKING:
    from lemoncrow_client.lite.index import ImportResolver

__all__ = [
    "DEGRADED_REASON",
    "LITE_BACKEND",
    "RELATIONSHIP_BASIS",
    "LiteCodeMapEngine",
    "LiteCodeMapProvider",
    "lite_engine",
    "lite_index_present",
]

_LOG: Final[logging.Logger] = logging.getLogger("lemoncrow.code_map.lite")

LITE_BACKEND: Final[str] = "runtime_lite"
RELATIONSHIP_BASIS: Final[str] = "imports"
DEGRADED_REASON: Final[str] = "lite_import_level"

# lc-debt: repository-wide import links resolve at most this many import rows
# per index generation; past it the links are partial and every payload built
# from them says ``truncated``. Upgrade: resolve import targets inside the Lite
# index at write time so the read side only aggregates.
_MAX_LINK_IMPORT_ROWS: Final[int] = 200_000
# A symbol's importers are found among at most this many import rows naming it.
_MAX_NEIGHBORHOOD_IMPORTS: Final[int] = 2_000
# How often a dashboard request may pay for the Lite freshness check.
_REFRESH_INTERVAL_S: Final[float] = 5.0
_MAX_TEXT_MATCHES: Final[int] = 40
_TEXT_SEARCH_TIMEOUT_S: Final[float] = 4.0
_MAX_TEXT_CHARS: Final[int] = 240
_MAX_SEARCH_TOKENS: Final[int] = 4
_MAX_SOURCE_LINES: Final[int] = 400
_MAX_LINK_SYMBOLS: Final[int] = 4
_MAX_LINK_EVIDENCE: Final[int] = 4
_IDENTIFIER: Final[re.Pattern[str]] = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# The Lite schema, empty, for a worktree whose index cannot be opened: every
# view still answers, with nothing in it.
_EMPTY_SCHEMA: Final[str] = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE files (path TEXT PRIMARY KEY, identity TEXT, digest TEXT, size INTEGER, text_indexed INTEGER);
CREATE TABLE symbols (name TEXT COLLATE NOCASE, kind TEXT, path TEXT, line INTEGER, end_line INTEGER, signature TEXT);
CREATE TABLE imports (path TEXT, module TEXT, imported TEXT, line INTEGER);
"""
# The columns code_map reads, over the Lite tables. TEMP views shadow the Lite
# tables of the same name for unqualified SQL; this module reads the ``lite_*``
# views, which hold every visible row (own and inherited for a worktree overlay).
_COMPAT_VIEWS: Final[str] = """
CREATE TEMP VIEW symbols AS
    SELECT lite_symbol_id(path, line, name) AS symbol_id,
           path AS file_path,
           '' AS language,
           name AS symbol_name,
           name AS qualified_name,
           kind,
           line AS start_line,
           end_line,
           signature
    FROM lite_symbols;
CREATE TEMP VIEW files AS
    SELECT path AS file_path, digest AS content_hash, size FROM lite_files;
"""


class _ClosingConnection(sqlite3.Connection):
    """A per-call read connection that closes itself once its last cursor goes."""

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()


def _path_digest(path: str) -> str:
    return hashlib.sha1(path.encode("utf-8", errors="surrogateescape")).hexdigest()[:16]


def lite_symbol_id(path: str, line: int, name: str) -> str:
    """A short, stable id: Lite records at most one symbol per line."""

    return f"lite:{_path_digest(str(path))}:{int(line)}:{name}"


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _language(path: str) -> str:
    from lemoncrow.core.service.code_map import _file_metadata

    return _file_metadata(path)[1]


# --------------------------------------------------------------------------- #
# Import resolution                                                           #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _ImportLinks:
    key: tuple[int, int, int]
    rows: tuple[dict[str, Any], ...]
    truncated: bool
    resolver: ImportResolver
    #: Import rows per ``(language, resolution status)``; external rows are never missing.
    stats: Mapping[tuple[str, str], int]


def _build_links(repo_root: Path, connection: sqlite3.Connection, key: tuple[int, int, int]) -> _ImportLinks:
    from lemoncrow_client.lite.index import ImportResolver

    resolver = ImportResolver(repo_root, (str(row[0]) for row in connection.execute("SELECT path FROM lite_files")))
    aggregate: dict[tuple[str, str], dict[str, Any]] = {}
    stats: Counter[tuple[str, str]] = Counter()
    languages: dict[str, str] = {}
    read = 0
    truncated = False
    cursor = connection.execute(
        "SELECT path, module, imported, line FROM lite_imports ORDER BY path, line LIMIT ?",
        (_MAX_LINK_IMPORT_ROWS + 1,),
    )
    for row in cursor:
        if read >= _MAX_LINK_IMPORT_ROWS:
            truncated = True
            break
        read += 1
        source = str(row["path"] or "")
        line = int(row["line"] or 0)
        resolution = resolver.resolve(source, str(row["module"] or ""), str(row["imported"] or ""))
        if source not in languages:
            languages[source] = _language(source)
        stats[(languages[source], resolution.status)] += 1
        truncated = truncated or resolution.truncated
        for target, names in resolution.targets.items():
            edge = aggregate.get((source, target))
            if edge is None:
                edge = {"weight": 0, "symbols": Counter(), "evidence": []}
                aggregate[(source, target)] = edge
            edge["weight"] += 1
            edge["symbols"].update(names)
            if len(edge["evidence"]) < _MAX_LINK_EVIDENCE:
                edge["evidence"].append({"path": source, "line": line, "symbol": names[0] if names else ""})
    rows = tuple(
        {
            "source_path": source,
            "target_path": target,
            "kind": "imports",
            "relationship_basis": RELATIONSHIP_BASIS,
            "weight": int(edge["weight"]),
            "symbols": [
                {"name": name, "count": count}
                for name, count in sorted(edge["symbols"].items(), key=lambda item: (-item[1], item[0]))[
                    :_MAX_LINK_SYMBOLS
                ]
            ],
            "evidence": list(edge["evidence"]),
        }
        for (source, target), edge in sorted(aggregate.items())
    )
    return _ImportLinks(
        key=key,
        rows=rows,
        truncated=truncated or resolver.truncated,
        resolver=resolver,
        stats=dict(stats),
    )


# --------------------------------------------------------------------------- #
# Engine                                                                      #
# --------------------------------------------------------------------------- #


class LiteCodeMapEngine:
    """The read surface :mod:`code_map` uses, over one worktree's Runtime Lite index.

    Every :meth:`connection` is a fresh read-only SQLite connection with TEMP
    views that present the Lite tables under the columns ``code_map`` queries.
    Reads first let the index catch up with the worktree (incrementally, at
    most every few seconds), the same refresh every Runtime Lite tool call pays.
    """

    backend: Final[str] = LITE_BACKEND

    def __init__(self, repo_root: Path) -> None:
        from lemoncrow_client.lite.index import LiteIndex

        self.repo_root = repo_root.expanduser().resolve()
        self._index = LiteIndex(self.repo_root)
        self.db_path: Path = self._index.db_path
        self.repo_id = f"lite:{self.repo_root}"
        self._refresh_lock = threading.Lock()
        self._links_lock = threading.Lock()
        self._refreshed_at = 0.0
        self.refresh_error = ""
        self._links: _ImportLinks | None = None

    # -- index state --------------------------------------------------------

    def refresh(self) -> None:
        """Bring the Lite index to the worktree; never raises."""

        with self._refresh_lock:
            if self._refreshed_at and time.monotonic() - self._refreshed_at < _REFRESH_INTERVAL_S:
                return
            try:
                self._index.maybe_refresh()
                self.refresh_error = ""
            except Exception as exc:
                self.refresh_error = f"{type(exc).__name__}: {exc}"[:300]
                _LOG.debug("runtime lite refresh failed for %s: %s", self.repo_root, self.refresh_error)
            self._refreshed_at = time.monotonic()

    def prepare(self, progress: Any = None) -> None:
        """Bring the index current now, reporting ``progress`` (``LiteIndexProgress``); raises on failure."""

        with self._refresh_lock:
            self._index.prepare(progress=progress)
            self.refresh_error = ""
            self._refreshed_at = time.monotonic()

    def connection(self) -> sqlite3.Connection:
        self.refresh()
        connection: sqlite3.Connection | None = None
        if self.db_path.is_file():
            try:
                connection = sqlite3.connect(
                    f"{self.db_path.as_uri()}?mode=ro", uri=True, timeout=30.0, factory=_ClosingConnection
                )
                self._prepare(connection)
            except sqlite3.Error as exc:
                _LOG.debug("runtime lite index unreadable at %s: %s", self.db_path, exc)
                if connection is not None:
                    connection.close()
                connection = None
        if connection is None:
            connection = sqlite3.connect(":memory:", factory=_ClosingConnection)
            connection.executescript(_EMPTY_SCHEMA)
            self._prepare(connection)
        return connection

    @staticmethod
    def _prepare(connection: sqlite3.Connection) -> None:
        from lemoncrow_client.lite.index import present_index_views

        connection.row_factory = sqlite3.Row
        connection.create_function("lite_symbol_id", 3, lite_symbol_id, deterministic=True)
        present_index_views(connection, prefix="lite_")
        connection.executescript(_COMPAT_VIEWS)

    def available(self) -> bool:
        return self.db_path.is_file()

    def _links_key(self, connection: sqlite3.Connection) -> tuple[int, int, int]:
        try:
            row = connection.execute("SELECT value FROM main.meta WHERE key='generation'").fetchone()
            generation = int(row[0]) if row is not None else 0
        except (sqlite3.Error, ValueError):
            generation = -1
        try:
            stat = self.db_path.stat()
            return generation, int(stat.st_ino), int(stat.st_dev)
        except OSError:
            return generation, 0, 0

    def import_links(self, connection: sqlite3.Connection | None = None) -> _ImportLinks:
        """File-to-file import links of the current index generation (cached per generation)."""

        if connection is None:
            connection = self.connection()
        key = self._links_key(connection)
        with self._links_lock:
            cached = self._links
            if cached is not None and cached.key == key:
                return cached
            links = _build_links(self.repo_root, connection, key)
            self._links = links
            return links

    # -- the CodeContextEngine subset code_map reads -------------------------

    def lite_symbol_row(self, symbol_id: str) -> dict[str, Any]:
        """One symbol by id, by its indexed name and line rather than a table scan."""

        parts = symbol_id.split(":", 3)
        if len(parts) != 4 or parts[0] != "lite" or not parts[2].isdigit():
            raise LookupError(f"symbol not found: {symbol_id}")
        _prefix, digest, line, name = parts
        rows = (
            self.connection()
            .execute(
                "SELECT name, kind, path, line, end_line FROM lite_symbols WHERE name = ? AND line = ?",
                (name, int(line)),
            )
            .fetchall()
        )
        for row in rows:
            path = str(row["path"])
            if str(row["name"]) == name and _path_digest(path) == digest:
                return {
                    "id": symbol_id,
                    "label": name,
                    "qualified_name": name,
                    "path": path,
                    "kind": str(row["kind"]),
                    "line": int(row["line"]),
                    "end_line": int(row["end_line"]),
                }
        raise LookupError(f"symbol not found: {symbol_id}")

    def search_symbols(
        self,
        query: str,
        *,
        limit: int = 20,
        mode: str = "lexical",
        snippet: str = "none",
        auto_index: bool = False,
    ) -> list[SimpleNamespace]:
        del mode, snippet, auto_index
        clean = query.strip()
        if not clean:
            return []
        bounded = max(1, min(int(limit), 40))
        tokens = list(dict.fromkeys(_IDENTIFIER.findall(clean)))[:_MAX_SEARCH_TOKENS] or [clean]
        connection = self.connection()
        records: list[SimpleNamespace] = []
        seen: set[tuple[str, int]] = set()
        for token in tokens:
            escaped = _like_escape(token)
            rows = connection.execute(
                """
                SELECT name, kind, path, line, end_line FROM lite_symbols
                WHERE name LIKE ? ESCAPE '\\'
                ORDER BY CASE WHEN name = ? THEN 0 WHEN name LIKE ? ESCAPE '\\' THEN 1 ELSE 2 END,
                         length(name), path, line
                LIMIT ?
                """,
                (f"%{escaped}%", token, f"{escaped}%", bounded),
            ).fetchall()
            for row in rows:
                name, path, line = str(row["name"]), str(row["path"]), int(row["line"])
                if (path, line) in seen:
                    continue
                seen.add((path, line))
                lowered, wanted = name.lower(), token.lower()
                score = 1.0 if lowered == wanted else 0.8 if lowered.startswith(wanted) else 0.5
                records.append(
                    SimpleNamespace(
                        symbol_id=lite_symbol_id(path, line, name),
                        symbol_name=name,
                        qualified_name=name,
                        file_path=path,
                        kind=str(row["kind"]),
                        language=_language(path),
                        start_line=line,
                        end_line=int(row["end_line"]),
                        score=score,
                    )
                )
        records.sort(key=lambda record: (-float(record.score), len(record.symbol_name), record.file_path))
        return records[:bounded]

    def search_text(
        self, query: str, *, path: str = ".", limit: int = 12, ignore_case: bool = True, whole_word: bool = False
    ) -> list[SimpleNamespace]:
        """Literal text matches in the worktree's tracked and unignored files (``git grep``).

        ``whole_word`` matches ``query`` only between non-word characters, the
        way a name is mentioned rather than contained.
        """

        del path
        clean = query.strip()
        if not clean:
            return []
        bounded = max(1, min(int(limit), _MAX_TEXT_MATCHES))
        command = ["git", "-c", "core.quotepath=false", "grep", "--untracked", "-I", "-n", "--column", "-z", "-F"]
        if ignore_case:
            command.append("-i")
        if whole_word:
            command.append("-w")
        command += ["-e", clean, "--", ".", ":(exclude).lemoncrow"]
        try:
            process = subprocess.Popen(command, cwd=self.repo_root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return []
        matches: list[SimpleNamespace] = []
        # A slow search ends with what it found so far, never with a hung request.
        # lc-debt: past _TEXT_SEARCH_TIMEOUT_S text matches are partial and the
        # search payload has no field to say so (like its result limit). Upgrade:
        # answer text search from the Lite FTS table and report partial results.
        timer = threading.Timer(_TEXT_SEARCH_TIMEOUT_S, process.kill)
        timer.start()
        try:
            assert process.stdout is not None
            for raw in process.stdout:
                parts = raw.rstrip(b"\n").split(b"\0", 3)
                if len(parts) == 4 and parts[1].isdigit() and parts[2].isdigit():
                    matches.append(
                        SimpleNamespace(
                            file_path=parts[0].decode("utf-8", errors="surrogateescape"),
                            line=int(parts[1]),
                            column=int(parts[2]),
                            text=parts[3].decode("utf-8", errors="replace")[:_MAX_TEXT_CHARS],
                        )
                    )
                if len(matches) >= bounded:
                    break
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            if process.stdout is not None:
                process.stdout.close()
        return matches

    def get_symbol(self, *, symbol_id: str, auto_index: bool = False) -> dict[str, Any]:
        del auto_index
        row = self.lite_symbol_row(symbol_id)
        path, line, end_line = str(row["path"]), int(row["line"]), int(row["end_line"])
        source = ""
        with contextlib.suppress(OSError):
            lines = (self.repo_root / path).read_text(encoding="utf-8", errors="replace").splitlines()
            source = "\n".join(lines[line - 1 : min(end_line, line + _MAX_SOURCE_LINES - 1)])
        return {
            "id": symbol_id,
            "name": row["label"],
            "qualified_name": row["qualified_name"],
            "path": path,
            "kind": row["kind"],
            "language": _language(path),
            "line": line,
            "end_line": end_line,
            "signature": source.splitlines()[0].strip() if source else "",
            "source": source,
            "provenance": LITE_BACKEND,
        }

    def provider(self) -> LiteCodeMapProvider:
        return LiteCodeMapProvider(self)


# --------------------------------------------------------------------------- #
# Relationship provider                                                       #
# --------------------------------------------------------------------------- #


class LiteCodeMapProvider:
    """Import-level relationships for :mod:`code_surface`, labelled as such."""

    __slots__ = ("_engine",)

    relationship_basis: Final[str] = RELATIONSHIP_BASIS

    def __init__(self, engine: LiteCodeMapEngine) -> None:
        self._engine = engine

    def _labelled(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            **payload,
            "degraded": True,
            "degraded_reason": DEGRADED_REASON,
            "relationship_basis": RELATIONSHIP_BASIS,
        }

    def readiness(self) -> Any:
        from lemoncrow.pro.capabilities.review.code_intelligence import (
            CodeIntelligenceLayerReadiness,
            CodeIntelligenceReadiness,
        )

        engine = self._engine
        engine.refresh()
        semantic = CodeIntelligenceLayerReadiness(
            "unavailable", reason="lite_semantic_unavailable", native_state=LITE_BACKEND
        )
        if not engine.available():
            reason = "lite_index_unavailable"
            missing = CodeIntelligenceLayerReadiness("unavailable", reason=reason, native_state=LITE_BACKEND)
            return CodeIntelligenceReadiness(
                backend=LITE_BACKEND,
                lexical=missing,
                structural=missing,
                graph=missing,
                semantic=semantic,
                reason=reason,
                degraded_reasons=(DEGRADED_REASON,),
            )
        return CodeIntelligenceReadiness(
            backend=LITE_BACKEND,
            lexical=CodeIntelligenceLayerReadiness("ready", reason="lite_lexical", native_state=LITE_BACKEND),
            structural=CodeIntelligenceLayerReadiness(
                "degraded", reason="lite_line_symbols", native_state=LITE_BACKEND
            ),
            graph=CodeIntelligenceLayerReadiness("degraded", reason=DEGRADED_REASON, native_state=LITE_BACKEND),
            semantic=semantic,
            reason=DEGRADED_REASON,
            degraded_reasons=(DEGRADED_REASON,),
        )

    def repository_file_links(self) -> Mapping[str, Any]:
        links = self._engine.import_links()
        return self._labelled({"edges": list(links.rows), "truncated": links.truncated})

    def file_relationships(self, paths: tuple[str, ...], *, limit: int = 320) -> Mapping[str, Any]:
        seeds = {path for path in paths if path}
        links = self._engine.import_links()
        rows = [row for row in links.rows if row["source_path"] in seeds or row["target_path"] in seeds]
        bounded = max(1, min(int(limit), 640))
        return self._labelled(
            {
                "edges": balanced_relationship_rows(rows, seeds, bounded),
                "truncated": links.truncated or len(rows) > bounded,
            }
        )

    def neighborhood(self, symbol: Mapping[str, Any], *, depth: int = 1, limit: int = 40) -> Mapping[str, Any]:
        """The files that import this symbol by name. Nothing a symbol calls is known here."""

        del depth
        path = str(symbol.get("path") or "")
        name = str(symbol.get("label") or "")
        focus_id = str(symbol.get("id") or "")
        focus = {
            "id": focus_id,
            "label": name,
            "path": path,
            "kind": str(symbol.get("kind") or "symbol"),
            "line": int(symbol.get("line") or 0),
            "end_line": int(symbol.get("end_line") or symbol.get("line") or 0),
        }
        if not path or not name:
            return self._labelled({"nodes": [focus], "edges": [], "truncated": False})
        from lemoncrow_client.lite.index import imported_names

        connection = self._engine.connection()
        links = self._engine.import_links(connection)
        rows = connection.execute(
            """
            SELECT path, module, imported, line FROM lite_imports
            WHERE imported LIKE ? ESCAPE '\\'
            ORDER BY path, line
            LIMIT ?
            """,
            (f"%{_like_escape(name)}%", _MAX_NEIGHBORHOOD_IMPORTS + 1),
        ).fetchall()
        truncated = links.truncated or len(rows) > _MAX_NEIGHBORHOOD_IMPORTS
        importers: dict[str, dict[str, int]] = {}
        for row in rows[:_MAX_NEIGHBORHOOD_IMPORTS]:
            source = str(row["path"] or "")
            imported = str(row["imported"] or "")
            if source == path or name not in imported_names(imported):
                continue
            targets = links.resolver.resolve(source, str(row["module"] or ""), imported).targets
            if name not in targets.get(path, ()):
                continue
            seen = importers.setdefault(source, {"line": int(row["line"] or 0), "weight": 0})
            seen["weight"] += 1
        bounded = max(1, min(int(limit), 40))
        ordered = sorted(importers.items(), key=lambda item: (-item[1]["weight"], item[0]))
        truncated = truncated or len(ordered) > bounded
        nodes: list[dict[str, Any]] = [focus]
        edges: list[dict[str, Any]] = []
        for source, seen in ordered[:bounded]:
            node_id = "file:" + source
            # The importer is a file: its label is its path, so no caller name is implied.
            nodes.append({"id": node_id, "label": source, "path": source, "kind": "file", "line": seen["line"]})
            edges.append({"source": node_id, "target": focus_id, "kind": "imports", "weight": seen["weight"]})
        return self._labelled({"nodes": nodes, "edges": edges, "truncated": truncated})


# --------------------------------------------------------------------------- #
# Entry points                                                                #
# --------------------------------------------------------------------------- #


@functools.lru_cache(maxsize=8)
def lite_engine(project_root: str) -> LiteCodeMapEngine:
    """Keep a small set of Lite map engines for the UI process, like ``code_map.get_engine``."""

    return LiteCodeMapEngine(Path(project_root))


def lite_index_present(project_root: Path) -> bool:
    from lemoncrow_client.lite.index import LiteIndex

    return LiteIndex(project_root).db_path.is_file()
