"""Deployment-neutral server-engine configuration plus the public local posture."""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ErrorCode, ServerError
from .limits import Limits

__all__ = ["EngineServerConfig", "LocalServerConfig"]


def _is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@dataclass(frozen=True, slots=True)
class EngineServerConfig:
    """Fields and invariants required by the shared session/view/index engine."""

    listen_host: str = "127.0.0.1"
    listen_port: int = 7420
    shutdown_timeout_s: float = 20.0
    session_ttl_s: float = 3600.0
    view_lease_s: float = 86_400.0
    allow_local_fs: bool = False
    blob_retention_s: float = 7 * 24 * 60 * 60.0
    maintenance_interval_s: float = 300.0
    state_root: Path | None = None
    index_path: Path | None = None
    workspace_root: Path | None = None
    frontend_dir: Path | None = None
    limits: Limits = field(default_factory=Limits)

    def __post_init__(self) -> None:
        if not 0 <= self.listen_port <= 65_535:
            raise ServerError(
                ErrorCode.NOT_CONFIGURED,
                "listen port must be between 0 and 65535",
                details={"port": self.listen_port},
            )
        if self.shutdown_timeout_s <= 0:
            raise ServerError(ErrorCode.NOT_CONFIGURED, "shutdown timeout must be positive")
        if self.session_ttl_s <= 0 or self.view_lease_s <= 0:
            raise ServerError(ErrorCode.NOT_CONFIGURED, "session ttl and view lease must be positive")
        if self.blob_retention_s < 0 or self.maintenance_interval_s < 0:
            raise ServerError(
                ErrorCode.NOT_CONFIGURED,
                "blob retention and maintenance interval must not be negative",
            )

    @property
    def loopback_only(self) -> bool:
        return _is_loopback(self.listen_host)


@dataclass(frozen=True, slots=True)
class LocalServerConfig(EngineServerConfig):
    """Single-user public composition, permanently constrained to loopback."""

    allow_local_fs: bool = True
    state_root: Path | None = Path(".lemoncrow-server")
    token_file: Path | None = None
    local_no_auth: bool = False
    # Explicitly disposable topology for benchmarks/tests. The public local
    # server remains durable SQLite by default; ephemeral mode selects the
    # contract-equivalent in-memory index and intentionally loses it on exit.
    ephemeral: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.loopback_only:
            raise ServerError(ErrorCode.NOT_CONFIGURED, "the public local server may only bind loopback")
        if self.state_root is None:
            raise ServerError(ErrorCode.NOT_CONFIGURED, "the public local server requires a state root")

    @property
    def resolved_index_path(self) -> Path | None:
        assert self.state_root is not None
        if self.ephemeral:
            return None
        return self.index_path or self.state_root / "index.sqlite3"

    @property
    def resolved_workspace_root(self) -> Path:
        assert self.state_root is not None
        return self.workspace_root or self.state_root / "workspaces"

    @property
    def resolved_review_root(self) -> Path:
        assert self.state_root is not None
        return self.state_root / "reviews"
