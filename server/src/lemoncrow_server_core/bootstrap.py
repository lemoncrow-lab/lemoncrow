"""Public loopback server composition."""

from __future__ import annotations

import os
import secrets
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

from .config import LocalServerConfig
from .dispatch import UnavailableDispatcher
from .index.deferred import DeferredIndexWorker, MemoryDeferredJobStore
from .index.repositories import build_repository_backend
from .local_app import LocalServerApp, build_local_app
from .local_maintenance import LocalMaintenance
from .server import LocalServer
from .wiring import build_backend, build_workspace

__all__ = ["build_local_server", "ensure_local_token", "read_local_token"]

_MAX_LOCAL_TOKEN_BYTES = 4096
_LOCAL_TOKEN_PREFIX = "lc_local_"


def _local_token_info(path: Path) -> os.stat_result:
    candidate = path.expanduser()
    try:
        info = candidate.lstat()
    except OSError as exc:
        raise RuntimeError(f"local server token file is not readable: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeError(f"local server token file must not be a symlink: {path}")
    if not stat.S_ISREG(info.st_mode):
        raise RuntimeError(f"local server token file must be a regular file: {path}")
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RuntimeError(f"local server token file must be owner-only (chmod 600): {path}")
    if hasattr(os, "getuid") and info.st_uid != os.getuid():
        raise RuntimeError(f"local server token file must be owned by the current user: {path}")
    if info.st_size > _MAX_LOCAL_TOKEN_BYTES:
        raise RuntimeError(f"local server token file is larger than {_MAX_LOCAL_TOKEN_BYTES} bytes: {path}")
    return info


def read_local_token(path: Path) -> str:
    candidate = path.expanduser()
    _local_token_info(candidate)
    try:
        token = candidate.read_text(encoding="utf-8", errors="strict").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"local server token file is not readable UTF-8: {path}") from exc
    if len(token) < 16:
        raise RuntimeError("local server token must be at least 16 characters")
    return token


def ensure_local_token(path: Path) -> str:
    """Return the durable machine credential, creating it once when absent.

    The file is the existing client/server contract at ``~/.lemoncrow/token``.
    Creation is exclusive so concurrent installer/server starts converge on one
    credential rather than racing two different secrets into the same path.
    """

    candidate = path.expanduser()
    if os.path.lexists(candidate):
        return read_local_token(candidate)

    candidate.parent.mkdir(parents=True, exist_ok=True)
    token = f"{_LOCAL_TOKEN_PREFIX}{secrets.token_urlsafe(32)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW

    try:
        fd = os.open(candidate, flags, 0o600)
    except FileExistsError:
        return read_local_token(candidate)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as stream:
            fd = -1
            stream.write(token + "\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            candidate.unlink()
        except OSError:
            pass
        raise
    return token


def build_local_server(config: LocalServerConfig, *, token: str | None = None) -> tuple[LocalServer, LocalServerApp]:
    assert config.state_root is not None
    config.state_root.mkdir(parents=True, exist_ok=True)
    index_path = config.resolved_index_path
    if index_path is not None:
        index_path.parent.mkdir(parents=True, exist_ok=True)
    workspace_root = config.resolved_workspace_root
    workspace_root.mkdir(parents=True, exist_ok=True)
    resolved_token = token
    if not config.local_no_auth and resolved_token is None:
        if config.token_file is None:
            raise RuntimeError("local server requires token_file unless local_no_auth is enabled")
        resolved_token = ensure_local_token(config.token_file)
    engine_config = replace(config, index_path=index_path, workspace_root=workspace_root)
    # The loopback server keeps one database per repository. A hosting
    # composition may instead keep one shared file.
    backend = (
        build_backend(engine_config)
        if index_path is None
        else build_repository_backend(
            index_path,
            content_size_cap=config.limits.max_indexed_content_bytes,
            inline_rebuild_budget=config.limits.inline_rebuild_budget,
            max_repository_bytes=config.limits.max_repository_index_bytes,
            max_wal_bytes=config.limits.max_index_wal_bytes,
        )
    )
    if backend.deferred_jobs is None:
        backend = replace(backend, deferred_jobs=MemoryDeferredJobStore())
    index_worker = DeferredIndexWorker(backend, max_files=config.limits.max_deferred_index_files)
    workspace = build_workspace(engine_config, backend)
    # Runtime tools run in the agent's own MCP process (Runtime Lite); the
    # local server serves Review and the Dashboard and never executes a tool.
    dispatcher = UnavailableDispatcher("Workspaces tools run in Workspaces Lite, not in the local server")
    state, app = build_local_app(
        config=engine_config,
        backend=backend,
        token=resolved_token,
        dispatcher=dispatcher,
        workspace=workspace,
    )
    if not engine_config.isolate_services:
        # Isolated, the Review worker runs the landing watch and the Dashboard
        # worker the session import and telemetry; the core keeps the index.
        if engine_config.dashboard_enabled:
            from lemoncrow.core.foundation.paths import default_store_root

            runtime_root = default_store_root()
        else:
            assert engine_config.state_root is not None
            runtime_root = engine_config.state_root / "runtime-disabled"
        assert engine_config.state_root is not None
        state.maintenance = cast(
            Any,
            LocalMaintenance(
                state.maintenance,
                runtime_root=runtime_root,
                state_root=engine_config.state_root,
                import_interval_s=30 * 60.0 if engine_config.dashboard_enabled else 0.0,
                flush_telemetry=engine_config.dashboard_enabled,
                # Merge-when-ready belongs to Review and stays active in Review-only mode.
                landing_watch=state.review_repository.reconcile_landings if engine_config.review_enabled else None,
            ),
        )
    cast(Any, state).index_worker = index_worker
    return LocalServer(state, app, config), state
