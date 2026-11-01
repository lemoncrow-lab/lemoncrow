"""Compact optional semantic helper for public Runtime Lite.

The helper is a session-scoped stdio child process. It never listens on a
socket and never talks to a LemonCrow service. Repository state is read locally
and persisted under .lemoncrow/workspace/runtime-lite-semantic.

Retrieval uses the public MIT-licensed minishlab/potion-code-16M-v2 static code
embedding model. Repository vectors are symmetrically quantized to signed int8 values
(one byte per embedding dimension). Search is an exact brute-force scan in bounded blocks;
there is no ANN index, daemon, or resident repository service.
"""

from __future__ import annotations

import bisect
import contextlib
import json
import os
import sqlite3
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from lemoncrow_client.lite.index import _DB_NAME as _LITE_INDEX_DB_NAME
from lemoncrow_client.lite.index import open_index_reader

_MODEL_ID: Final[str] = "minishlab/potion-code-16M-v2"
# Changing the lexical database identity invalidates existing semantic generations.
_SCHEMA_VERSION: Final[int] = 5
_TARGET_CHARS: Final[int] = 750
_MIN_CHUNK_CHARS: Final[int] = 50
_MAX_RECURSION: Final[int] = 256
_MAX_FILE_BYTES: Final[int] = 4 * 1024 * 1024
_EMBED_BATCH: Final[int] = 2048
_SCAN_BLOCK: Final[int] = 8192
_SCALE: Final[int] = 127
_DIMENSIONS: Final[int] = 256
_CACHE_DIR: Final[str] = ".lemoncrow/workspace/runtime-lite-semantic"
_LITE_DB: Final[str] = f".lemoncrow/workspace/{_LITE_INDEX_DB_NAME}"

# Keep an explicitly enabled local helper quiet inside coding-host stderr.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")


@dataclass(frozen=True, slots=True)
class _Boundary:
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Chunk:
    content: str
    start_line: int
    end_line: int


def _optional_modules() -> tuple[Any, Any, Any]:
    try:
        import numpy as np
        from model2vec import StaticModel
        from tree_sitter_language_pack import get_parser
    except ImportError as exc:
        raise RuntimeError(
            "Runtime Lite semantic support requires the public lite-semantic extra "
            "(model2vec + numpy + tree-sitter-language-pack)"
        ) from exc
    return np, StaticModel, get_parser


def _language_for_path(path: Path) -> str | None:
    from lemoncrow.infra.code_intel.languages import language_for_path

    language = language_for_path(path)
    return language.parser_name if language is not None else None


def _merge_adjacent(boundaries: list[_Boundary], desired: int) -> list[_Boundary]:
    if not boundaries:
        return []
    merged: list[_Boundary] = []
    current_start = boundaries[0].start
    current_end = boundaries[0].end
    current_length = current_end - current_start
    for boundary in boundaries[1:]:
        length = boundary.end - boundary.start
        if current_length + length > desired:
            merged.append(_Boundary(current_start, current_end))
            current_start = boundary.start
            current_end = boundary.end
            current_length = length
            continue
        current_end = boundary.end
        current_length += length
    merged.append(_Boundary(current_start, current_end))
    return merged


def _split_node(node: Any, desired: int, depth: int) -> list[_Boundary]:
    children = list(node.children)
    if not children:
        return [_Boundary(int(node.start_byte), int(node.end_byte))]
    length = int(node.end_byte) - int(node.start_byte)
    if depth >= _MAX_RECURSION or length < _MIN_CHUNK_CHARS:
        return [_Boundary(int(node.start_byte), int(node.end_byte))]
    groups: list[_Boundary] = []
    index = 0
    while index < len(children):
        child = children[index]
        start = int(child.start_byte)
        end = int(child.end_byte)
        length = end - start
        index += 1
        if length > desired:
            groups.extend(_split_node(child, desired, depth + 1))
            continue
        while index < len(children):
            candidate = children[index]
            candidate_length = int(candidate.end_byte) - int(candidate.start_byte)
            if length + candidate_length > desired:
                break
            end = int(candidate.end_byte)
            length += candidate_length
            index += 1
        groups.append(_Boundary(start, end))
    return groups


def _line_boundaries(raw: bytes, desired: int) -> list[_Boundary]:
    if not raw.strip():
        return []
    boundaries: list[_Boundary] = []
    start = 0
    current_start = 0
    current_length = 0
    for line in raw.splitlines(keepends=True):
        length = len(line)
        if current_length and current_length + length > desired:
            boundaries.append(_Boundary(current_start, start))
            current_start = start
            current_length = 0
        start += length
        current_length += length
    if start > current_start:
        boundaries.append(_Boundary(current_start, start))
    return boundaries


def _chunks(source: str, path: str, get_parser: Any) -> list[_Chunk]:
    if not source.strip():
        return []
    raw = source.encode("utf-8", errors="replace")
    language = _language_for_path(Path(path))
    boundaries: list[_Boundary] | None = None
    if language:
        try:
            parser = get_parser(language)
            root = parser.parse(raw).root_node
            boundaries = _merge_adjacent(_split_node(root, _TARGET_CHARS, 0), _TARGET_CHARS)
        except Exception:
            boundaries = None
    if boundaries is None:
        boundaries = _line_boundaries(raw, _TARGET_CHARS)

    newline_offsets = [index for index, value in enumerate(raw) if value == 10]
    chunks: list[_Chunk] = []
    for boundary in boundaries:
        start = max(0, min(boundary.start, len(raw)))
        end = max(start, min(boundary.end, len(raw)))
        if end <= start:
            continue
        content = raw[start:end].decode("utf-8", errors="replace")
        if not content.strip():
            continue
        start_line = bisect.bisect_left(newline_offsets, start) + 1
        end_line = bisect.bisect_left(newline_offsets, max(start, end - 1)) + 1
        chunks.append(_Chunk(content=content, start_line=start_line, end_line=max(start_line, end_line)))
    return chunks


class _CompactSemanticIndex:
    def __init__(self) -> None:
        self._np: Any | None = None
        self._StaticModel: Any | None = None
        self._get_parser: Any | None = None
        self._model: Any | None = None
        self._repo_root: Path | None = None
        self._generation = 0
        self._lite_generation = 0
        self._vectors: Any | None = None
        self._start_lines: Any | None = None
        self._end_lines: Any | None = None
        self._paths: list[str] = []
        self._starts: Any | None = None
        self._counts: Any | None = None

    def _modules(self) -> tuple[Any, Any, Any]:
        if self._np is None:
            self._np, self._StaticModel, self._get_parser = _optional_modules()
        return self._np, self._StaticModel, self._get_parser

    def _model_instance(self) -> Any:
        _np, StaticModel, _get_parser = self._modules()
        if self._model is None:
            model_id = os.environ.get("LEMONCROW_LITE_SEMANTIC_MODEL", _MODEL_ID).strip() or _MODEL_ID
            self._model = StaticModel.from_pretrained(model_id, force_download=False)
        return self._model

    @staticmethod
    def _cache_root(repo_root: Path) -> Path:
        return repo_root / _CACHE_DIR

    @staticmethod
    def _lite_db(repo_root: Path) -> Path:
        return repo_root / _LITE_DB

    def _state(self, repo_root: Path) -> sqlite3.Connection:
        root = self._cache_root(repo_root)
        root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(root / "state.sqlite3", timeout=120)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID")
        connection.execute("""
            CREATE TABLE IF NOT EXISTS files(
                path TEXT PRIMARY KEY,
                identity TEXT NOT NULL,
                start_row INTEGER NOT NULL,
                row_count INTEGER NOT NULL
            ) WITHOUT ROWID
            """)
        return connection

    @staticmethod
    def _lite_snapshot(repo_root: Path) -> tuple[int, list[tuple[str, str]]]:
        db_path = _CompactSemanticIndex._lite_db(repo_root)
        if not db_path.is_file():
            raise RuntimeError("Runtime Lite lexical index is not prepared")
        # A worktree overlay's files include those it inherits from its base.
        connection = open_index_reader(db_path, timeout=30)
        try:
            generation_row = connection.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
            generation = int(generation_row[0]) if generation_row else 0
            rows = connection.execute("SELECT path, identity FROM files WHERE text_indexed=1 ORDER BY path").fetchall()
            return generation, [(str(path), str(identity)) for path, identity in rows]
        finally:
            connection.close()

    @staticmethod
    def _lite_scopes(repo_root: Path) -> dict[str, tuple[list[int], list[str]]]:
        """Load structural scope names from the existing public Lite index."""

        db_path = _CompactSemanticIndex._lite_db(repo_root)
        if not db_path.is_file():
            return {}
        connection = open_index_reader(db_path, timeout=30)
        try:
            rows = connection.execute("""
                SELECT path, line, name
                FROM symbols
                WHERE kind IN ('function','class','type','interface')
                  AND name <> ''
                GROUP BY path, line, name
                ORDER BY path, line, name
                """).fetchall()
        finally:
            connection.close()

        scopes: dict[str, tuple[list[int], list[str]]] = {}
        current_path = ""
        lines: list[int] = []
        names: list[str] = []
        for raw_path, raw_line, raw_name in rows:
            path = str(raw_path)
            if path != current_path:
                if current_path:
                    scopes[current_path] = (lines, names)
                current_path = path
                lines = []
                names = []
            lines.append(max(1, int(raw_line)))
            names.append(str(raw_name))
        if current_path:
            scopes[current_path] = (lines, names)
        return scopes

    @staticmethod
    def _scope_name(
        scopes: tuple[list[int], list[str]] | None,
        start_line: int,
    ) -> str:
        if not scopes:
            return ""
        lines, names = scopes
        index = bisect.bisect_right(lines, max(1, start_line)) - 1
        return names[index] if index >= 0 else ""

    @staticmethod
    def _lite_generation_now(repo_root: Path) -> int:
        db_path = _CompactSemanticIndex._lite_db(repo_root)
        if not db_path.is_file():
            return 0
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        try:
            row = connection.execute("SELECT value FROM meta WHERE key='generation'").fetchone()
            return int(row[0]) if row else 0
        finally:
            connection.close()

    @staticmethod
    def _meta(connection: sqlite3.Connection, key: str, default: str = "") -> str:
        row = connection.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return str(row[0]) if row is not None else default

    @staticmethod
    def _set_meta(connection: sqlite3.Connection, key: str, value: object) -> None:
        connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    def _generation_paths(self, repo_root: Path, generation: int) -> tuple[Path, Path, Path]:
        root = self._cache_root(repo_root)
        return (
            root / f"vectors.{generation}.i8",
            root / f"starts.{generation}.i32",
            root / f"ends.{generation}.i32",
        )

    def status(self, repo_root: Path) -> dict[str, object]:
        """Return whether a complete semantic generation matches current Lite state."""

        root = repo_root.resolve()
        try:
            lite_generation = self._lite_generation_now(root)
            connection = self._state(root)
            try:
                schema = int(self._meta(connection, "schema_version", "0") or 0)
                generation = int(self._meta(connection, "active_generation", "0") or 0)
                semantic_lite_generation = int(self._meta(connection, "lite_generation", "0") or 0)
                rows = int(self._meta(connection, "rows", "0") or 0)
                dimensions = int(self._meta(connection, "dimensions", "0") or 0)
                scale = int(self._meta(connection, "scale", "0") or 0)
            finally:
                connection.close()
        except (OSError, sqlite3.Error, ValueError):
            return {"ready": False}

        if (
            schema != _SCHEMA_VERSION
            or generation <= 0
            or semantic_lite_generation != lite_generation
            or dimensions != _DIMENSIONS
            or scale != _SCALE
        ):
            return {"ready": False}

        vector_path, start_path, end_path = self._generation_paths(root, generation)
        expected_vector_bytes = rows * _DIMENSIONS
        expected_line_bytes = rows * 4
        try:
            complete = (
                vector_path.is_file()
                and start_path.is_file()
                and end_path.is_file()
                and vector_path.stat().st_size == expected_vector_bytes
                and start_path.stat().st_size == expected_line_bytes
                and end_path.stat().st_size == expected_line_bytes
            )
        except OSError:
            complete = False
        if not complete:
            return {"ready": False}
        return {
            "ready": True,
            "generation": generation,
            "rows": rows,
            "lite_generation": lite_generation,
        }

    def _cleanup_generations(self, repo_root: Path, active_generation: int) -> None:
        """Prune stale disk generations; mapped readers keep their open inode."""

        root = self._cache_root(repo_root)
        candidates = tuple(root.glob("vectors.*.i8")) + tuple(root.glob("vectors.*.q6i8"))
        for candidate in candidates:
            try:
                generation = int(candidate.name.split(".")[1])
            except (IndexError, ValueError):
                continue
            is_active = generation == active_generation and candidate.suffixes[-1:] == [".i8"]
            if is_active:
                continue
            with contextlib.suppress(OSError):
                candidate.unlink()
            if generation != active_generation:
                for old_path in (
                    root / f"starts.{generation}.i32",
                    root / f"ends.{generation}.i32",
                ):
                    with contextlib.suppress(OSError):
                        old_path.unlink()

        # Remove artifacts produced by earlier public prototypes. They are
        # incompatible with schema v1 and otherwise linger beside the compact
        # generation after upgrades.
        for pattern in ("vectors-*.i8", "centroids-*.f32", "manifest-*.json"):
            for obsolete in root.glob(pattern):
                with contextlib.suppress(OSError):
                    obsolete.unlink()
        legacy_meta = root / "meta.sqlite3"
        with contextlib.suppress(OSError):
            legacy_meta.unlink()

    def warm(self, repo_root: Path) -> dict[str, object]:
        """Load a valid compact generation and static model without rebuilding."""

        root = repo_root.resolve()
        status = self.status(root)
        if not bool(status.get("ready")):
            return status
        self._load(root)
        self._model_instance()
        return dict(status)

    def prepare(self, repo_root: Path, *, force: bool = False) -> dict[str, object]:
        np, _StaticModel, get_parser = self._modules()
        root = repo_root.resolve()
        if not (root / ".git").exists():
            raise RuntimeError(f"Runtime Lite semantic helper requires a Git worktree: {root}")

        for attempt in range(2):
            lite_generation, current_files = self._lite_snapshot(root)
            scope_symbols = self._lite_scopes(root)
            connection = self._state(root)
            temp_paths: list[Path] = []
            try:
                connection.execute("BEGIN IMMEDIATE")
                schema = int(self._meta(connection, "schema_version", "0") or 0)
                model_id = os.environ.get("LEMONCROW_LITE_SEMANTIC_MODEL", _MODEL_ID).strip() or _MODEL_ID
                old_model = self._meta(connection, "model_id")
                old_generation = int(self._meta(connection, "active_generation", "0") or 0)
                old_rows = int(self._meta(connection, "rows", "0") or 0)
                compatible = not force and schema == _SCHEMA_VERSION and old_model == model_id and old_generation > 0
                old_files = {
                    str(row["path"]): (
                        str(row["identity"]),
                        int(row["start_row"]),
                        int(row["row_count"]),
                    )
                    for row in connection.execute("SELECT path,identity,start_row,row_count FROM files ORDER BY path")
                }
                old_lite_generation = int(self._meta(connection, "lite_generation", "0") or 0)
                current_identity = {path: identity for path, identity in current_files}
                if (
                    compatible
                    and old_lite_generation == lite_generation
                    and len(old_files) == len(current_identity)
                    and all(
                        path in old_files and old_files[path][0] == identity
                        for path, identity in current_identity.items()
                    )
                ):
                    connection.commit()
                    self._cleanup_generations(root, old_generation)
                    self._unload()
                    self._load(root)
                    return {
                        "ready": True,
                        "generation": old_generation,
                        "rows": old_rows,
                        "files": len(current_files),
                        "changed_files": 0,
                        "reused_files": len(current_files),
                        "skipped_large_files": 0,
                        "vector_bytes": old_rows * _DIMENSIONS,
                    }

                old_vectors = old_starts = old_ends = None
                if compatible:
                    old_vector_path, old_start_path, old_end_path = self._generation_paths(root, old_generation)
                    if old_vector_path.is_file() and old_start_path.is_file() and old_end_path.is_file():
                        if old_rows:
                            old_vectors = np.memmap(
                                old_vector_path,
                                dtype=np.int8,
                                mode="r",
                                shape=(old_rows, _DIMENSIONS),
                            )
                            old_starts = np.memmap(
                                old_start_path,
                                dtype=np.int32,
                                mode="r",
                                shape=(old_rows,),
                            )
                            old_ends = np.memmap(
                                old_end_path,
                                dtype=np.int32,
                                mode="r",
                                shape=(old_rows,),
                            )
                    else:
                        compatible = False

                new_generation = old_generation + 1
                vector_path, start_path, end_path = self._generation_paths(root, new_generation)
                vector_tmp = vector_path.with_suffix(vector_path.suffix + ".tmp")
                start_tmp = start_path.with_suffix(start_path.suffix + ".tmp")
                end_tmp = end_path.with_suffix(end_path.suffix + ".tmp")
                temp_paths.extend((vector_tmp, start_tmp, end_tmp))
                vector_file = vector_tmp.open("wb")
                start_file = start_tmp.open("wb")
                end_file = end_tmp.open("wb")
                logical_row = 0
                changed_files = 0
                reused_files = 0
                skipped_large = 0
                new_rows: list[tuple[str, str, int, int]] = []
                pending_text: list[str] = []
                pending_starts: list[int] = []
                pending_ends: list[int] = []

                def flush_pending(
                    texts: list[str] = pending_text,
                    starts: list[int] = pending_starts,
                    ends: list[int] = pending_ends,
                    vectors_out: Any = vector_file,
                    starts_out: Any = start_file,
                    ends_out: Any = end_file,
                ) -> None:
                    if not texts:
                        return
                    model = self._model_instance()
                    vectors = model.encode(texts)
                    if int(vectors.shape[1]) != _DIMENSIONS:
                        raise RuntimeError(
                            f"semantic model dimension changed: expected {_DIMENSIONS}, got {vectors.shape[1]}"
                        )
                    quantized = np.clip(np.rint(vectors * _SCALE), -_SCALE, _SCALE).astype(np.int8)
                    vectors_out.write(quantized.tobytes(order="C"))
                    starts_out.write(np.asarray(starts, dtype=np.int32).tobytes(order="C"))
                    ends_out.write(np.asarray(ends, dtype=np.int32).tobytes(order="C"))
                    texts.clear()
                    starts.clear()
                    ends.clear()

                for path, identity in current_files:
                    previous = old_files.get(path) if compatible else None
                    if (
                        previous is not None
                        and previous[0] == identity
                        and old_vectors is not None
                        and old_starts is not None
                        and old_ends is not None
                    ):
                        flush_pending()
                        _old_identity, old_start, count = previous
                        new_rows.append((path, identity, logical_row, count))
                        if count:
                            vector_file.write(old_vectors[old_start : old_start + count].tobytes(order="C"))
                            start_file.write(old_starts[old_start : old_start + count].tobytes(order="C"))
                            end_file.write(old_ends[old_start : old_start + count].tobytes(order="C"))
                        logical_row += count
                        reused_files += 1
                        continue

                    changed_files += 1
                    absolute = root / path
                    try:
                        size = absolute.stat().st_size
                    except OSError:
                        new_rows.append((path, identity, logical_row, 0))
                        continue
                    if size > _MAX_FILE_BYTES:
                        skipped_large += 1
                        new_rows.append((path, identity, logical_row, 0))
                        continue
                    try:
                        source = absolute.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        new_rows.append((path, identity, logical_row, 0))
                        continue
                    chunks = _chunks(source, path, get_parser)
                    count = len(chunks)
                    new_rows.append((path, identity, logical_row, count))
                    logical_row += count
                    file_scopes = scope_symbols.get(path)
                    for chunk in chunks:
                        scope_name = self._scope_name(file_scopes, chunk.start_line)
                        prefix = f"file {path}\n"
                        if scope_name:
                            prefix += f"scope {scope_name}\n"
                        pending_text.append(prefix + chunk.content)
                        pending_starts.append(chunk.start_line)
                        pending_ends.append(chunk.end_line)
                        if len(pending_text) >= _EMBED_BATCH:
                            flush_pending()
                flush_pending()
                vector_file.close()
                start_file.close()
                end_file.close()

                after_generation, _after_files = self._lite_snapshot(root)
                if after_generation != lite_generation:
                    connection.rollback()
                    for temp_path in temp_paths:
                        with contextlib.suppress(OSError):
                            temp_path.unlink()
                    if attempt == 0:
                        continue
                    raise RuntimeError("Runtime Lite lexical index changed during semantic preparation")

                os.replace(vector_tmp, vector_path)
                os.replace(start_tmp, start_path)
                os.replace(end_tmp, end_path)
                temp_paths.clear()

                connection.execute("DELETE FROM files")
                connection.executemany(
                    "INSERT INTO files(path,identity,start_row,row_count) VALUES(?,?,?,?)",
                    new_rows,
                )
                self._set_meta(connection, "schema_version", _SCHEMA_VERSION)
                self._set_meta(connection, "model_id", model_id)
                self._set_meta(connection, "active_generation", new_generation)
                self._set_meta(connection, "rows", logical_row)
                self._set_meta(connection, "dimensions", _DIMENSIONS)
                self._set_meta(connection, "scale", _SCALE)
                self._set_meta(connection, "lite_generation", lite_generation)
                self._set_meta(connection, "updated_at", int(time.time()))
                connection.commit()

                self._cleanup_generations(root, new_generation)

                self._unload()
                self._load(root)
                return {
                    "ready": True,
                    "generation": new_generation,
                    "rows": logical_row,
                    "files": len(current_files),
                    "changed_files": changed_files,
                    "reused_files": reused_files,
                    "skipped_large_files": skipped_large,
                    "vector_bytes": logical_row * _DIMENSIONS,
                }
            except Exception:
                for stream in (
                    locals().get("vector_file"),
                    locals().get("start_file"),
                    locals().get("end_file"),
                ):
                    if stream is not None:
                        with contextlib.suppress(OSError):
                            stream.close()
                with contextlib.suppress(sqlite3.Error):
                    connection.rollback()
                for temp_path in temp_paths:
                    with contextlib.suppress(OSError):
                        temp_path.unlink()
                raise
            finally:
                connection.close()
        raise AssertionError("unreachable")

    def _unload(self) -> None:
        self._repo_root = None
        self._generation = 0
        self._lite_generation = 0
        self._vectors = None
        self._start_lines = None
        self._end_lines = None
        self._paths = []
        self._starts = None
        self._counts = None

    def _load(self, repo_root: Path) -> None:
        np, _StaticModel, _get_parser = self._modules()
        root = repo_root.resolve()
        connection = self._state(root)
        try:
            generation = int(self._meta(connection, "active_generation", "0") or 0)
            lite_generation = int(self._meta(connection, "lite_generation", "0") or 0)
            rows = int(self._meta(connection, "rows", "0") or 0)
            dimensions = int(self._meta(connection, "dimensions", "0") or 0)
            scale = int(self._meta(connection, "scale", "0") or 0)
            if generation <= 0 or dimensions != _DIMENSIONS or scale != _SCALE:
                raise RuntimeError("semantic index is not prepared")
            vector_path, start_path, end_path = self._generation_paths(root, generation)
            if not vector_path.is_file() or not start_path.is_file() or not end_path.is_file():
                raise RuntimeError("semantic index generation is incomplete")
            file_rows = connection.execute(
                "SELECT path,start_row,row_count FROM files WHERE row_count>0 ORDER BY start_row"
            ).fetchall()
        finally:
            connection.close()

        self._unload()
        self._repo_root = root
        self._generation = generation
        self._lite_generation = lite_generation
        self._vectors = (
            np.memmap(vector_path, dtype=np.int8, mode="r", shape=(rows, dimensions))
            if rows
            else np.empty((0, dimensions), dtype=np.int8)
        )
        self._start_lines = (
            np.memmap(start_path, dtype=np.int32, mode="r", shape=(rows,)) if rows else np.empty((0,), dtype=np.int32)
        )
        self._end_lines = (
            np.memmap(end_path, dtype=np.int32, mode="r", shape=(rows,)) if rows else np.empty((0,), dtype=np.int32)
        )
        self._paths = [str(row["path"]) for row in file_rows]
        self._starts = np.asarray([int(row["start_row"]) for row in file_rows], dtype=np.int64)
        self._counts = np.asarray([int(row["row_count"]) for row in file_rows], dtype=np.int32)

    @staticmethod
    def _allowed(path: str, scopes: list[str]) -> bool:
        if not scopes:
            return True
        normalized = path.replace("\\", "/")
        for raw in scopes:
            scope = raw.strip().replace("\\", "/").strip("/")
            if not scope or scope == ".":
                return True
            if normalized == scope or normalized.startswith(scope + "/"):
                return True
        return False

    def search(
        self,
        repo_root: Path,
        query: str,
        *,
        paths: list[str],
        limit: int,
    ) -> dict[str, object]:
        np, _StaticModel, _get_parser = self._modules()
        root = repo_root.resolve()
        if self._repo_root != root:
            self._load(root)
        if self._lite_generation_now(root) != self._lite_generation:
            raise RuntimeError("semantic index is stale; lexical index advanced")
        assert self._vectors is not None
        assert self._start_lines is not None
        assert self._end_lines is not None
        assert self._starts is not None
        assert self._counts is not None

        if not self._paths:
            return {"matches": []}
        model = self._model_instance()
        query_vector = model.encode([query])[0].astype(np.float32)
        query_q6 = np.clip(np.rint(query_vector * _SCALE), -_SCALE, _SCALE).astype(np.int8)
        scores = np.empty(len(self._vectors), dtype=np.int32)
        for start in range(0, len(self._vectors), _SCAN_BLOCK):
            end = min(len(self._vectors), start + _SCAN_BLOCK)
            scores[start:end] = np.sum(
                np.multiply(self._vectors[start:end], query_q6, dtype=np.int16),
                axis=1,
                dtype=np.int32,
            )
        file_scores = np.maximum.reduceat(scores, self._starts)
        if paths:
            for index, path in enumerate(self._paths):
                if not self._allowed(path, paths):
                    file_scores[index] = -(2**31)

        wanted = max(1, min(int(limit), len(self._paths)))
        if wanted < len(self._paths):
            candidate_ids = np.argpartition(-file_scores, wanted - 1)[:wanted]
        else:
            candidate_ids = np.arange(len(self._paths))
        ordered = sorted(
            (int(index) for index in candidate_ids if int(file_scores[int(index)]) > -(2**31)),
            key=lambda index: (-int(file_scores[index]), self._paths[index]),
        )

        matches: list[dict[str, object]] = []
        for file_index in ordered[:wanted]:
            start = int(self._starts[file_index])
            count = int(self._counts[file_index])
            local = scores[start : start + count]
            row = start + int(np.argmax(local))
            matches.append(
                {
                    "path": self._paths[file_index],
                    "line": int(self._start_lines[row]),
                    "end_line": int(self._end_lines[row]),
                    "score": int(file_scores[file_index]),
                }
            )
        return {"matches": matches, "generation": self._generation}


def _response(request_id: object, *, result: Mapping[str, object] | None = None, error: str = "") -> None:
    payload: dict[str, object] = {"id": request_id, "ok": not bool(error)}
    if error:
        payload["error"] = error
    else:
        payload["result"] = dict(result or {})
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def main() -> int:
    runtime = _CompactSemanticIndex()
    request: dict[str, object] | None = None
    for raw in sys.stdin:
        try:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict):
                raise ValueError("request must be a JSON object")
            request = parsed
            request_id = request.get("id")
            operation = str(request.get("op") or "")
            if operation == "close":
                _response(request_id, result={"closed": True})
                return 0
            repo_root = Path(str(request.get("repo_root") or "")).expanduser().resolve()
            if operation == "status":
                result = runtime.status(repo_root)
            elif operation == "warm":
                result = runtime.warm(repo_root)
            elif operation == "prepare":
                result = runtime.prepare(repo_root, force=bool(request.get("force")))
            elif operation == "search":
                query = str(request.get("query") or "")
                scopes = request.get("paths")
                paths = [str(item) for item in scopes] if isinstance(scopes, list) else []
                raw_limit = request.get("limit", 100)
                if isinstance(raw_limit, bool) or not isinstance(raw_limit, (int, str)):
                    search_limit = 100
                else:
                    search_limit = int(raw_limit)
                result = runtime.search(
                    repo_root,
                    query,
                    paths=paths,
                    limit=max(1, min(search_limit, 200)),
                )
            else:
                raise ValueError(f"unknown semantic helper operation: {operation}")
            _response(request_id, result=result)
        except Exception as exc:
            _response(
                request.get("id") if request is not None else None,
                error=f"{type(exc).__name__}: {exc}",
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
