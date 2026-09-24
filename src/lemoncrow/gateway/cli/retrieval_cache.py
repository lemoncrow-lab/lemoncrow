"""Content-bound identities for local retrieval evaluation results."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_RUNTIME_ROOTS = ("src", "client/src", "server/src", "enterprise/server/src", "benchmarks/codebench")
_PACKAGES = ("lemoncrow", "lemoncrow_client", "lemoncrow_server_core", "lemoncrow_server")
_SOURCE_SUFFIXES = {".py", ".sql", ".json", ".toml", ".yaml", ".yml"}
_LOCK_FILES = ("uv.lock", "pyproject.toml", "client/pyproject.toml", "server/pyproject.toml")


def source_fingerprint(root: Path, env: Mapping[str, str]) -> str:
    """Hash actual source bytes, including dirty files and PYTHONPATH overrides."""
    trees = {root / part for part in _RUNTIME_ROOTS}
    for entry in env.get("PYTHONPATH", "").split(os.pathsep):
        if entry:
            base = Path(entry)
            if not base.is_absolute():
                base = root / base
            trees.update(base / package for package in _PACKAGES)
    files = {root / name for name in _LOCK_FILES}
    for tree in trees:
        if tree.is_dir():
            files.update(
                path
                for path in tree.rglob("*")
                if path.is_file()
                and path.suffix in _SOURCE_SUFFIXES
                and "__pycache__" not in path.parts
                and ("benchmarks" not in path.parts or path.suffix == ".py")
            )
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(str(path.resolve()).encode())
        digest.update(b"\0")
        try:
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
        except FileNotFoundError:
            digest.update(b"<missing>")
    return digest.hexdigest()


def cache_identity(root: Path, command: list[str], env: Mapping[str, str], golds: list[Path], *, source: str) -> str:
    """No credentials are persisted: configuration contributes only to the hash."""
    corpus = []
    for path in golds:
        resolved = path if path.is_absolute() else root / path
        corpus.append((str(resolved.resolve()), hashlib.sha256(resolved.read_bytes()).hexdigest()))
    config = {
        key: value
        for key, value in env.items()
        if key.startswith(("LEMONCROW_", "EVAL_", "FITNESS_")) or key in {"PYTHONPATH", "PATH", "VIRTUAL_ENV"}
    }
    payload = {"schema": 1, "source": source, "command": command, "config": config, "corpus": corpus}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def cached_result(raw: Any, identity: str) -> dict[str, Any] | None:
    """Legacy/unverifiable files and mismatched runs must never certify a release."""
    if not isinstance(raw, dict) or raw.get("cache_schema") != 1 or raw.get("identity") != identity:
        return None
    result = raw.get("result")
    return result if isinstance(result, dict) else None
