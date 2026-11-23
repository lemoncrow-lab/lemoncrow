"""Optional local semantic retrieval for Runtime Lite.

This module is deliberately Python-standard-library only. The public
lemoncrow-client package keeps its zero-dependency contract and talks to an
optional helper process owned by the full public LemonCrow installation.

The helper is opt-in. Missing helpers, missing model dependencies, build errors,
or process failures all fail open to deterministic lexical Runtime Lite.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_TRUE: Final[frozenset[str]] = frozenset({"1", "true", "yes", "on"})
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
_CODE_SHAPE = re.compile(r"[|\\^$()\[\]{}=]|::|->")
_HELPER_MODULE: Final[str] = "lemoncrow.infra.runtime.lite_semantic_helper"


@dataclass(frozen=True, slots=True)
class SemanticMatch:
    """One semantic file hit with the best matching chunk location."""

    path: str
    line: int
    end_line: int


def semantic_enabled(environment: Mapping[str, str]) -> bool:
    """Return whether the optional local semantic channel is explicitly enabled."""

    return environment.get("LEMONCROW_LITE_SEMANTIC", "").strip().lower() in _TRUE


def is_semantic_intent_query(query: str) -> bool:
    """Use semantics only for prose-like retrieval intent, not code-shaped queries."""

    return len(_IDENTIFIER.findall(query)) >= 3 and bool(re.search(r"\s", query)) and _CODE_SHAPE.search(query) is None


def fuse_ranked_paths(primary: Sequence[str], semantic: Sequence[str]) -> list[str]:
    """Fuse semantic recall without allowing it to displace lexical rank one."""

    anchor = next((path for path in primary if path), None)
    primary_tail = [path for path in primary if path and path != anchor]
    semantic_tail = [path for path in semantic if path and path != anchor]
    primary_rank = {path: rank for rank, path in enumerate(primary_tail, start=1)}
    semantic_rank = {path: rank for rank, path in enumerate(semantic_tail, start=1)}
    union = set(primary_rank) | set(semantic_rank)
    tail = sorted(
        union,
        key=lambda path: (
            -(1.0 / (60 + primary_rank.get(path, 10_000)) + 1.0 / (60 + semantic_rank.get(path, 10_000))),
            path,
        ),
    )
    return ([anchor] if anchor is not None else []) + tail


class LiteSemanticClient:
    """Session-owned stdio client for the optional compact semantic helper."""

    def __init__(self, environment: Mapping[str, str]) -> None:
        self.environment = dict(environment)
        self._process: subprocess.Popen[str] | None = None
        self._request_lock = threading.Lock()
        self._ready: set[Path] = set()
        self._errors: dict[Path, str] = {}
        self._next_id = 1
        self._disabled_reason = ""
        self._closed = False

    @property
    def active(self) -> bool:
        return semantic_enabled(self.environment)

    def error(self, repo_root: Path) -> str:
        return self._errors.get(repo_root.resolve(), self._disabled_reason)

    def ready(self, repo_root: Path) -> bool:
        return repo_root.resolve() in self._ready

    def _command(self) -> list[str] | None:
        explicit = self.environment.get("LEMONCROW_LITE_SEMANTIC_HELPER", "").strip()
        if explicit:
            return shlex.split(explicit)
        try:
            if importlib.util.find_spec(_HELPER_MODULE) is None:
                return None
        except (ImportError, ModuleNotFoundError, ValueError):
            return None
        return [sys.executable, "-m", _HELPER_MODULE]

    def _ensure_process(self) -> subprocess.Popen[str] | None:
        if self._closed or not self.active:
            return None
        process = self._process
        if process is not None and process.poll() is None:
            return process
        command = self._command()
        if not command:
            self._disabled_reason = (
                "optional Workspaces Lite semantic helper is unavailable; "
                "install the full LemonCrow lite-semantic extra"
            )
            return None
        env = dict(os.environ)
        env.update(self.environment)
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            self._disabled_reason = f"{type(exc).__name__}: {exc}"
            return None
        self._process = process
        return process

    def _request(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        with self._request_lock:
            process = self._ensure_process()
            if process is None or process.stdin is None or process.stdout is None:
                raise RuntimeError(self._disabled_reason or "semantic helper unavailable")
            request_id = self._next_id
            self._next_id += 1
            body = dict(payload)
            body["id"] = request_id
            try:
                process.stdin.write(json.dumps(body, separators=(",", ":")) + "\n")
                process.stdin.flush()
                line = process.stdout.readline()
            except (BrokenPipeError, OSError) as exc:
                self._process = None
                raise RuntimeError(f"semantic helper failed: {exc}") from exc
            if not line:
                code = process.poll()
                self._process = None
                raise RuntimeError(f"semantic helper exited unexpectedly (status={code})")
            try:
                response = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError("semantic helper returned invalid JSON") from exc
            if not isinstance(response, Mapping) or response.get("id") != request_id:
                raise RuntimeError("semantic helper returned an invalid response")
            if not bool(response.get("ok")):
                raise RuntimeError(str(response.get("error") or "semantic helper request failed"))
            result = response.get("result")
            return result if isinstance(result, Mapping) else {}

    def adopt_prepared(self, repo_root: Path) -> bool:
        """Adopt a matching warm cache without rebuilding or loading the model."""

        root = repo_root.resolve()
        if not self.active:
            return False
        try:
            result = self._request({"op": "status", "repo_root": str(root)})
        except Exception as exc:
            self._errors[root] = f"{type(exc).__name__}: {exc}"
            self._ready.discard(root)
            return False
        if not bool(result.get("ready")):
            self._ready.discard(root)
            return False
        self._errors.pop(root, None)
        self._ready.add(root)
        return True

    def warm(self, repo_root: Path) -> bool:
        """Load a matching warm cache/model in the helper without rebuilding."""

        root = repo_root.resolve()
        if not self.active or not self.ready(root):
            return False
        try:
            result = self._request({"op": "warm", "repo_root": str(root)})
        except Exception as exc:
            self._errors[root] = f"{type(exc).__name__}: {exc}"
            self._ready.discard(root)
            return False
        if not bool(result.get("ready")):
            self._ready.discard(root)
            return False
        self._errors.pop(root, None)
        return True

    def prepare(self, repo_root: Path, *, force: bool = False) -> bool:
        root = repo_root.resolve()
        if not self.active:
            return False
        try:
            result = self._request({"op": "prepare", "repo_root": str(root), "force": bool(force)})
        except Exception as exc:
            self._errors[root] = f"{type(exc).__name__}: {exc}"
            self._ready.discard(root)
            return False
        if not bool(result.get("ready")):
            self._errors[root] = str(result.get("error") or "semantic index not ready")
            self._ready.discard(root)
            return False
        self._errors.pop(root, None)
        self._ready.add(root)
        return True

    def search(
        self,
        repo_root: Path,
        query: str,
        *,
        paths: str | Sequence[str] | None = None,
        limit: int = 100,
    ) -> list[SemanticMatch]:
        """Search only a ready cache; never put a cold build on the tool path."""

        root = repo_root.resolve()
        if not self.active:
            return []
        if not self.ready(root) and not self.adopt_prepared(root):
            return []
        if isinstance(paths, str):
            scopes = [paths]
        elif paths is None:
            scopes = []
        else:
            scopes = [str(path) for path in paths]
        try:
            result = self._request(
                {
                    "op": "search",
                    "repo_root": str(root),
                    "query": query,
                    "paths": scopes,
                    "limit": max(1, min(int(limit), 200)),
                }
            )
        except Exception as exc:
            self._errors[root] = f"{type(exc).__name__}: {exc}"
            self._ready.discard(root)
            return []
        raw = result.get("matches")
        if not isinstance(raw, list):
            return []
        matches: list[SemanticMatch] = []
        seen: set[str] = set()
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            path = str(item.get("path") or "").replace("\\", "/")
            if not path or path in seen:
                continue
            seen.add(path)
            line = max(1, int(item.get("line") or 1))
            end_line = max(line, int(item.get("end_line") or line))
            matches.append(SemanticMatch(path=path, line=line, end_line=end_line))
        return matches

    def close(self) -> None:
        self._closed = True
        process, self._process = self._process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=2)


def prepare_lite_semantic_workspace(
    repo_root: Path,
    environment: Mapping[str, str] | None = None,
    *,
    force: bool = False,
) -> dict[str, object]:
    """Synchronously prepare the optional semantic cache for foreground commands."""

    client = LiteSemanticClient(environment or os.environ)
    try:
        if not client.active:
            return {"semantic_enabled": False, "semantic_ready": False}
        ready = client.prepare(repo_root, force=force)
        payload: dict[str, object] = {"semantic_enabled": True, "semantic_ready": ready}
        if not ready and client.error(repo_root):
            payload["semantic_error"] = client.error(repo_root)
        return payload
    finally:
        client.close()


__all__ = [
    "LiteSemanticClient",
    "SemanticMatch",
    "fuse_ranked_paths",
    "is_semantic_intent_query",
    "prepare_lite_semantic_workspace",
    "semantic_enabled",
]
