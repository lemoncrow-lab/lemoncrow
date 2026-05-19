"""Local-only Zoekt search seam for large-repo text search routing."""

from .binary import ZoektBinaryResolution, discover_zoekt_binary
from .client import ZoektClient, ZoektClientMatch, ZoektFileResult
from .server import ZoektHealth, ZoektServer, get_zoekt_server, reset_zoekt_servers

__all__ = [
    "ZoektBinaryResolution",
    "ZoektClient",
    "ZoektClientMatch",
    "ZoektFileResult",
    "ZoektHealth",
    "ZoektServer",
    "discover_zoekt_binary",
    "get_zoekt_server",
    "reset_zoekt_servers",
]
