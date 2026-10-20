"""Deployment-neutral server-engine configuration plus the public local posture."""

from __future__ import annotations

import ipaddress
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ErrorCode, ServerError
from .limits import Limits

__all__ = ["LOCAL_PRODUCTS", "EngineServerConfig", "LocalServerConfig", "canonical_products"]

#: Products the local server can compose. Runtime tools are not among them:
#: Runtime Lite runs them in the agent's own MCP process.
LOCAL_PRODUCTS: frozenset[str] = frozenset({"review", "dashboard"})
# Before Runtime Lite the Dashboard shipped under the Runtime product's name.
_LEGACY_PRODUCTS = {"runtime": "dashboard"}


def canonical_products(names: Iterable[str]) -> frozenset[str]:
    """Map legacy product names onto the products the local server composes."""

    return frozenset(_LEGACY_PRODUCTS.get(name, name) for name in names)


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
    # A View lives while its session is active: session activity renews its
    # lease. An idle one lapses and its client reopens it on next use; a
    # day-long lease only kept abandoned Views' index state (graphs, ANN) alive.
    view_lease_s: float = 3600.0
    # The longest lease a client may ask for, for a View nothing will renew: a
    # Review snapshot is read long after the CLI that captured it has exited.
    view_lease_max_s: float = 86_400.0
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
        if self.view_lease_max_s < self.view_lease_s:
            raise ServerError(ErrorCode.NOT_CONFIGURED, "view lease ceiling must not be below the default lease")
        if self.blob_retention_s < 0 or self.maintenance_interval_s < 0:
            raise ServerError(
                ErrorCode.NOT_CONFIGURED,
                "blob retention and maintenance interval must not be negative",
            )

    @property
    def loopback_only(self) -> bool:
        return _is_loopback(self.listen_host)


@dataclass(frozen=True)
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
    # Review and Dashboard run in worker processes of their own, so their CPU
    # work never holds this process's GIL while a coding agent's call waits.
    # Off by default (tests compose in-process); `up` turns it on.
    isolate_services: bool = False
    # Product composition for this process. Core session/view/blob routes are
    # always present; Review and the Dashboard are independently optional.
    products: frozenset[str] = LOCAL_PRODUCTS

    def __post_init__(self) -> None:
        super().__post_init__()
        if not self.loopback_only:
            raise ServerError(ErrorCode.NOT_CONFIGURED, "the public local server may only bind loopback")
        if self.state_root is None:
            raise ServerError(ErrorCode.NOT_CONFIGURED, "the public local server requires a state root")
        products = canonical_products(self.products)
        if not products:
            raise ServerError(
                ErrorCode.NOT_CONFIGURED,
                "local server requires at least one product: review or dashboard",
            )
        unknown = products - LOCAL_PRODUCTS
        if unknown:
            raise ServerError(
                ErrorCode.NOT_CONFIGURED,
                "local server products must be review and/or dashboard",
                details={"unknown_products": sorted(unknown)},
            )
        object.__setattr__(self, "products", products)

    @property
    def review_enabled(self) -> bool:
        return "review" in self.products

    @property
    def dashboard_enabled(self) -> bool:
        return "dashboard" in self.products

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
