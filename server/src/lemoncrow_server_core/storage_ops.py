"""Low-cardinality operational metrics for authoritative and workspace storage."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

__all__ = ["StorageConfig", "WorkspaceStorage", "render_storage_metrics"]


class StorageConfig(Protocol):
    state_root: Path | None
    index_path: Path | None


class WorkspaceStorage(Protocol):
    @property
    def root(self) -> Path: ...

    @property
    def quota_bytes(self) -> int: ...

    @property
    def bytes_on_disk(self) -> int: ...

    @property
    def live_views(self) -> int: ...


def _filesystem(path: Path | None) -> tuple[int, int, int, int]:
    if path is None:
        return 0, 0, 0, 0
    try:
        stat = os.statvfs(path)
    except OSError:
        return 0, 0, 0, 0
    block = int(stat.f_frsize or stat.f_bsize)
    capacity = max(0, block * int(stat.f_blocks))
    free = max(0, block * int(stat.f_bavail))
    return 1, capacity, max(0, capacity - free), free


def _sqlite_family_bytes(path: Path | None) -> int:
    if path is None:
        return 0
    total = 0
    for candidate in (path, Path(str(path) + "-wal"), Path(str(path) + "-shm")):
        try:
            if candidate.is_file():
                total += max(0, candidate.stat().st_size)
        except OSError:
            continue
    return total


def render_storage_metrics(*, config: StorageConfig, workspace: WorkspaceStorage | None) -> str:
    state_available, state_capacity, state_used, state_free = _filesystem(config.state_root)
    workspace_root = None if workspace is None else workspace.root
    ws_available, ws_capacity, ws_used, ws_free = _filesystem(workspace_root)
    ws_materialized = 0 if workspace is None else max(0, int(workspace.bytes_on_disk))
    ws_quota = 0 if workspace is None else max(0, int(workspace.quota_bytes))
    ws_views = 0 if workspace is None else max(0, int(workspace.live_views))
    lines = [
        "# HELP lemoncrow_storage_plane_info Fixed identity and durability contract of a LemonCrow storage plane.",
        "# TYPE lemoncrow_storage_plane_info gauge",
        'lemoncrow_storage_plane_info{durability="persistent",plane="state",role="authoritative"} 1',
        'lemoncrow_storage_plane_info{durability="ephemeral",plane="workspace",role="materialized-cache"} 1',
        "# HELP lemoncrow_storage_plane_configured Whether a storage plane has an explicit runtime root.",
        "# TYPE lemoncrow_storage_plane_configured gauge",
        f'lemoncrow_storage_plane_configured{{plane="state"}} {int(config.state_root is not None)}',
        f'lemoncrow_storage_plane_configured{{plane="workspace"}} {int(workspace is not None)}',
        "# HELP lemoncrow_storage_plane_available Whether filesystem statistics for the plane can be read.",
        "# TYPE lemoncrow_storage_plane_available gauge",
        f'lemoncrow_storage_plane_available{{plane="state"}} {state_available}',
        f'lemoncrow_storage_plane_available{{plane="workspace"}} {ws_available}',
        "# HELP lemoncrow_storage_capacity_bytes Filesystem capacity visible to the storage plane.",
        "# TYPE lemoncrow_storage_capacity_bytes gauge",
        f'lemoncrow_storage_capacity_bytes{{plane="state"}} {state_capacity}',
        f'lemoncrow_storage_capacity_bytes{{plane="workspace"}} {ws_capacity}',
        "# HELP lemoncrow_storage_used_bytes Filesystem bytes unavailable for new writes on the storage plane.",
        "# TYPE lemoncrow_storage_used_bytes gauge",
        f'lemoncrow_storage_used_bytes{{plane="state"}} {state_used}',
        f'lemoncrow_storage_used_bytes{{plane="workspace"}} {ws_used}',
        "# HELP lemoncrow_storage_free_bytes Filesystem bytes available to the server on the storage plane.",
        "# TYPE lemoncrow_storage_free_bytes gauge",
        f'lemoncrow_storage_free_bytes{{plane="state"}} {state_free}',
        f'lemoncrow_storage_free_bytes{{plane="workspace"}} {ws_free}',
        "# HELP lemoncrow_storage_index_sqlite_bytes SQLite index database plus WAL/shared-memory bytes.",
        "# TYPE lemoncrow_storage_index_sqlite_bytes gauge",
        f"lemoncrow_storage_index_sqlite_bytes {_sqlite_family_bytes(config.index_path)}",
        "# HELP lemoncrow_storage_workspace_materialized_bytes Bytes in currently materialized source trees.",
        "# TYPE lemoncrow_storage_workspace_materialized_bytes gauge",
        f"lemoncrow_storage_workspace_materialized_bytes {ws_materialized}",
        "# HELP lemoncrow_storage_workspace_quota_bytes Server-enforced materialized-tree byte quota.",
        "# TYPE lemoncrow_storage_workspace_quota_bytes gauge",
        f"lemoncrow_storage_workspace_quota_bytes {ws_quota}",
        "# HELP lemoncrow_storage_workspace_live_views Number of materialized workspace views currently tracked.",
        "# TYPE lemoncrow_storage_workspace_live_views gauge",
        f"lemoncrow_storage_workspace_live_views {ws_views}",
    ]
    return "\n".join(lines) + "\n"
