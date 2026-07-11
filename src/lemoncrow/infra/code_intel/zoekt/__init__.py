"""Managed Zoekt search seam for large-repo text search routing."""

from .adapter import (
    ZoektBackendHealth,
    ZoektSupervisor,
    get_zoekt_supervisor,
    reset_zoekt_supervisors,
)
from .binary import ZoektBinaryResolution, discover_zoekt_binary
from .client import ZoektClient, ZoektClientMatch, ZoektFileResult
from .indexer import ZoektIndexer
from .server import ZoektHealth, ZoektServer, get_zoekt_server, reset_zoekt_servers

__all__ = [
    "ZoektBackendHealth",
    "ZoektBinaryResolution",
    "ZoektClient",
    "ZoektClientMatch",
    "ZoektFileResult",
    "ZoektHealth",
    "ZoektIndexer",
    "ZoektServer",
    "ZoektSupervisor",
    "discover_zoekt_binary",
    "get_zoekt_server",
    "get_zoekt_supervisor",
    "reset_zoekt_servers",
    "reset_zoekt_supervisors",
]
