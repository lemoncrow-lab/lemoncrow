"""Hermetic three-stage execution environment for reviewed code (Phase 9R item 3).

Stages, each isolated from the next:

``fetch``
    Package-registry-only egress and **no repository-controlled code at all**.
    LemonCrow itself reads the reviewed lockfile and downloads every tarball
    from a registry on the trusted-base/organization allowlist: exact
    scheme/host/port/path match, no URL userinfo, no ambient proxies, DNS
    answers pinned and checked (private addresses refused unless the registry
    rule says ``private_network``), redirects followed only to another
    allowlisted registry, integrity (sha256/384/512 SRI) verified, package
    name bound to the tarball path, and scoped/pinned names bound to their
    pinned registry (dependency confusion). Lifecycle scripts cannot run
    because nothing from the repository executes; they are reported as
    blocked.
``build``
    Offline. Tarballs are unpacked by a fixed extractor inside bubblewrap with
    every namespace unshared (no network), symlinks/devices dropped, paths
    confined, sizes bounded and the unpacked ``package.json`` name/version
    checked against the plan. The project build then runs offline with the
    tree mounted read-only.
``run``
    Offline, default-deny egress, plus **recording sinks**: fake SMTP,
    HTTP/webhook proxy, queue and object-store endpoints inside the run's
    private network namespace capture outbound side effects instead of
    delivering them.

Authority: :func:`load_hermetic_policy` reads only trusted-base configuration
(and organization policy); a head-side ``execution:`` block is never read.
Optional sidecars and seed fixtures come from that same trusted policy.

Registry allowlist, per ecosystem (``npm``, ``pypi``): the public registry of
an ecosystem is allowed only while the trusted base declares no registry for
that ecosystem. An organization registry list is a ceiling (an ecosystem it
does not list has no registry), and ``deny_all: true`` from either layer
allows none. Every lockfile entry whose resolved URL is not on the effective
allowlist is refused.

Ecosystems: npm ``package-lock.json`` (v2/v3) here; Python ``uv.lock`` and
hash-pinned ``requirements*.txt`` in
:mod:`~lemoncrow.pro.capabilities.review.hermetic_python`. Any other lockfile
(pnpm, yarn, bun, Poetry, Pipenv, Cargo, Go, Bundler, Composer) is refused
with a visible reason by :func:`unsupported_lockfiles`; nothing falls back to
the live checkout.

Caches are write-isolated by source trust (:func:`~lemoncrow.pro.capabilities.
review.execution.cache_namespace`): an untrusted head run writes only its own
namespace and nothing it writes is readable by a trusted or base run.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import ipaddress
import json
import os
import platform
import posixpath
import re
import selectors
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from typing import Any, Final, cast
from urllib.parse import unquote, urljoin, urlsplit

import yaml

from lemoncrow.pro.capabilities.review.execution import (
    DEFAULT_UNVERIFIED_INTEGRATIONS,
    PARTIAL_IMAGE_PREFIX,
    UNKNOWN_ENVIRONMENT_VALUES,
    ArtifactRef,
    CacheNamespace,
    ContentAccess,
    EnvironmentFingerprint,
    ExecutableIdentity,
    ExecutionRecipe,
    ExecutionResult,
    ExecutionSubject,
    ExecutorClass,
    SinkCapture,
    SinkKind,
    StageOutcome,
    UnverifiedIntegration,
    VerificationEnvironment,
    canonical_digest,
    readable_cache_namespaces,
)
from lemoncrow.pro.capabilities.review.run_evidence import (
    DEFAULT_RAW_RETENTION_SECONDS,
    RedactionPolicy,
    RunArtifactStore,
    protect_bytes,
    protect_text,
)

HERMETIC_POLICY_VERSION: Final[int] = 1
EXTRACTOR_VERSION: Final[int] = 1
DEFAULT_NPM_REGISTRY: Final[str] = "registry.npmjs.org"
DEFAULT_PYPI_INDEX: Final[str] = "pypi.org"
DEFAULT_PYPI_FILES: Final[str] = "files.pythonhosted.org"
ECOSYSTEMS: Final[tuple[str, ...]] = ("npm", "pypi")
MAX_PACKAGES: Final[int] = 5000
MAX_TARBALL_BYTES: Final[int] = 128 * 1024 * 1024
MAX_TOTAL_FETCH_BYTES: Final[int] = 2 * 1024 * 1024 * 1024
MAX_REDIRECTS: Final[int] = 3
FETCH_TIMEOUT_SECONDS: Final[float] = 60.0
INSTALL_TIMEOUT_SECONDS: Final[int] = 600
FETCH_CONCURRENCY: Final[int] = 8
KEEP_TREES_PER_NAMESPACE: Final[int] = 8
TREE_IDLE_SECONDS: Final[float] = 7 * 24 * 3600
TARBALL_IDLE_SECONDS: Final[float] = 30 * 24 * 3600
_PRUNE_EVERY_SECONDS: Final[float] = 600.0
SINK_MOUNT: Final[str] = "/lemoncrow-sinks"
SINK_MAX_CAPTURES: Final[int] = 256
SINK_MAX_PAYLOAD_BYTES: Final[int] = 256 * 1024
_PREVIEW_CHARS: Final[int] = 2048
_HOST_RE = re.compile(r"^(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$")
_SCOPE_RE = re.compile(r"^@[a-z0-9][a-z0-9._-]{0,213}$")
_PACKAGE_NAME_RE = re.compile(r"^(?:@[a-z0-9][a-z0-9._~-]*/)?[a-z0-9._~-][a-z0-9._~-]{0,213}$")
_PYPI_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,198}[a-z0-9])?$")


def pypi_normalize(name: str) -> str:
    """PEP 503 normalized project name."""

    return re.sub(r"[-_.]+", "-", name).lower()


_SRI_ALGORITHMS: Final[tuple[str, ...]] = ("sha512", "sha384", "sha256")
_LOCKFILE: Final[str] = "package-lock.json"
_LIFECYCLE: Final[tuple[str, ...]] = ("preinstall", "install", "postinstall", "prepare", "prepublish")
_RUNTIME_MOUNTS: Final[tuple[str, ...]] = ("/usr", "/bin", "/lib", "/lib64", "/sbin")
_SANDBOX_PATH: Final[str] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class HermeticRefused(RuntimeError):
    """Policy refused this input (for example a non-registry URL); never retried."""


class HermeticUnavailable(RuntimeError):
    """The host cannot provide a hermetic stage (no bubblewrap, registry down, ...)."""


# --------------------------------------------------------------------------- #
# Trusted policy
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RegistryRule:
    """One allowlisted package registry. Matching is exact; there are no wildcards."""

    host: str
    scheme: str = "https"
    port: int | None = None
    path_prefix: str = "/"
    scopes: tuple[str, ...] = ()
    packages: tuple[str, ...] = ()
    private_network: bool = False
    ecosystem: str = "npm"

    def __post_init__(self) -> None:
        if self.ecosystem not in ECOSYSTEMS:
            raise ValueError(f"registry ecosystem must be one of {', '.join(ECOSYSTEMS)}")
        if self.ecosystem == "pypi" and self.scopes:
            raise ValueError("pypi registries pin package names with packages:, not scopes")
        if not _HOST_RE.match(self.host):
            raise ValueError(f"registry host {self.host!r} must be a lowercase DNS name without wildcards")
        if self.scheme not in {"https", "http"}:
            raise ValueError("registry scheme must be https (or http for a private_network registry)")
        if self.scheme == "http" and not self.private_network:
            raise ValueError("plain-http registries are allowed only with private_network: true")
        if self.port is not None and not 1 <= self.port <= 65535:
            raise ValueError("registry port is out of range")
        if not self.path_prefix.startswith("/") or ".." in self.path_prefix.split("/"):
            raise ValueError("registry path_prefix must be an absolute URL path")
        for scope in self.scopes:
            if not _SCOPE_RE.match(scope):
                raise ValueError(f"registry scope {scope!r} must look like @scope")
        for name in self.packages:
            valid = (
                _PYPI_NAME_RE.match(pypi_normalize(name)) if self.ecosystem == "pypi" else _PACKAGE_NAME_RE.match(name)
            )
            if not valid:
                raise ValueError(f"pinned package name {name!r} is invalid")

    @property
    def effective_port(self) -> int:
        return self.port or (443 if self.scheme == "https" else 80)

    def matches(self, url: str) -> bool:
        try:
            parts = urlsplit(url)
            port = parts.port
        except ValueError:
            return False
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            return False
        return (
            parts.scheme == self.scheme
            and (parts.hostname or "") == self.host
            and (port or (443 if parts.scheme == "https" else 80)) == self.effective_port
            and unquote(parts.path).startswith(self.path_prefix)
            and ".." not in unquote(parts.path).split("/")
        )

    def pins(self, name: str) -> bool:
        if self.ecosystem == "pypi":
            return pypi_normalize(name) in {pypi_normalize(item) for item in self.packages}
        scope = name.split("/", 1)[0] if name.startswith("@") else ""
        return name in self.packages or bool(scope and scope in self.scopes)

    @property
    def key(self) -> tuple[str, str, str, int]:
        return (self.ecosystem, self.host, self.scheme, self.effective_port)

    def to_wire(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "scheme": self.scheme,
            "port": self.port,
            "path_prefix": self.path_prefix,
            "scopes": list(self.scopes),
            "packages": list(self.packages),
            "private_network": self.private_network,
            "ecosystem": self.ecosystem,
        }

    @classmethod
    def from_config(cls, raw: Mapping[str, Any]) -> RegistryRule:
        unknown = set(raw) - {
            "host",
            "scheme",
            "port",
            "path_prefix",
            "scopes",
            "packages",
            "private_network",
            "ecosystem",
        }
        if unknown:
            raise ValueError(f"registry rule has unknown fields: {', '.join(sorted(unknown))}")
        port = raw.get("port")
        if port is not None and (not isinstance(port, int) or isinstance(port, bool)):
            raise ValueError("registry port must be an integer")
        private = raw.get("private_network", False)
        if not isinstance(private, bool):
            raise ValueError("registry private_network must be boolean")
        return cls(
            host=str(raw.get("host") or "").strip().lower(),
            scheme=str(raw.get("scheme") or "https").strip().lower(),
            port=port,
            path_prefix=str(raw.get("path_prefix") or "/"),
            scopes=tuple(str(item).strip().lower() for item in _seq(raw.get("scopes"))),
            packages=tuple(str(item).strip() for item in _seq(raw.get("packages"))),
            private_network=private,
            ecosystem=str(raw.get("ecosystem") or "npm").strip().lower(),
        )


DEFAULT_REGISTRIES: Final[tuple[RegistryRule, ...]] = (
    RegistryRule(DEFAULT_NPM_REGISTRY),
    RegistryRule(DEFAULT_PYPI_INDEX, path_prefix="/simple", ecosystem="pypi"),
    RegistryRule(DEFAULT_PYPI_FILES, path_prefix="/packages/", ecosystem="pypi"),
)


def _with_public_defaults(declared: Sequence[RegistryRule]) -> tuple[RegistryRule, ...]:
    """Declared rules plus the public registry of every ecosystem the declaration does not mention."""

    mentioned = {rule.ecosystem for rule in declared}
    return (*declared, *(rule for rule in DEFAULT_REGISTRIES if rule.ecosystem not in mentioned))


@dataclass(frozen=True, slots=True)
class SidecarSpec:
    """A service started inside the run stage's private network (trusted policy only)."""

    id: str
    command: tuple[str, ...]
    port: int
    ready_timeout: float = 10.0

    def to_wire(self) -> dict[str, Any]:
        return {"id": self.id, "command": list(self.command), "port": self.port, "ready_timeout": self.ready_timeout}


@dataclass(frozen=True, slots=True)
class FixtureSpec:
    """A seed fixture copied read-only from the trusted base, never from the head."""

    path: str

    def to_wire(self) -> dict[str, Any]:
        return {"path": self.path}


def _seq(value: object) -> Sequence[object]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return value
    raise ValueError("expected a list")


def _safe_rel(value: str, *, field_name: str) -> str:
    raw = value.strip().replace("\\", "/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must stay inside the repository")
    return path.as_posix()


@dataclass(frozen=True, slots=True)
class HermeticPolicy:
    """Effective hermetic policy. Built only from trusted-base and organization layers."""

    registries: tuple[RegistryRule, ...] = DEFAULT_REGISTRIES
    sidecars: tuple[SidecarSpec, ...] = ()
    fixtures: tuple[FixtureSpec, ...] = ()
    not_verified: tuple[UnverifiedIntegration, ...] = ()
    redaction: RedactionPolicy = field(default_factory=RedactionPolicy)
    retention_seconds: int = DEFAULT_RAW_RETENTION_SECONDS
    source: str = "default"
    deny_all: bool = False
    version: int = HERMETIC_POLICY_VERSION

    def registry_for(self, url: str, ecosystem: str | None = None) -> RegistryRule | None:
        if self.deny_all:
            return None
        return next(
            (
                rule
                for rule in self.registries
                if (ecosystem is None or rule.ecosystem == ecosystem) and rule.matches(url)
            ),
            None,
        )

    def pinned_registry(self, name: str, ecosystem: str = "npm") -> RegistryRule | None:
        return next((rule for rule in self.registries if rule.ecosystem == ecosystem and rule.pins(name)), None)

    def registries_for(self, ecosystem: str) -> tuple[RegistryRule, ...]:
        return () if self.deny_all else tuple(rule for rule in self.registries if rule.ecosystem == ecosystem)

    def require_fetch(self, ecosystem: str) -> None:
        """Refuse up front when policy allows no registry for *ecosystem*."""

        if self.deny_all:
            raise HermeticRefused(f"dependency fetch is denied by {self.source} policy (deny_all)")
        if not self.registries_for(ecosystem):
            raise HermeticRefused(f"no {ecosystem} registry is allowlisted by {self.source} policy")

    @property
    def unverified(self) -> tuple[UnverifiedIntegration, ...]:
        return (*DEFAULT_UNVERIFIED_INTEGRATIONS, *self.not_verified)

    def to_wire(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "source": self.source,
            "registries": [rule.to_wire() for rule in self.registries],
            "sidecars": [item.to_wire() for item in self.sidecars],
            "fixtures": [item.to_wire() for item in self.fixtures],
            "not_verified": [item.to_wire() for item in self.not_verified],
            "redaction": self.redaction.to_wire(),
            "retention_seconds": self.retention_seconds,
            "deny_all": self.deny_all,
        }

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_wire())

    @property
    def comparable_digest(self) -> str:
        """Digest of what governs execution, without the ``source`` label (reproduction comparison)."""

        wire = self.to_wire()
        wire.pop("source", None)
        return canonical_digest(wire)


def _parse_execution_block(raw: Mapping[str, Any], *, source: str, public_defaults: bool = True) -> HermeticPolicy:
    allowed = {
        "deny_all",
        "registries",
        "sidecars",
        "fixtures",
        "not_verified",
        "redact",
        "redact_literals",
        "mask",
        "retention_days",
    }
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError(f"execution policy has unknown fields: {', '.join(sorted(unknown))}")
    registries = tuple(
        RegistryRule.from_config(item) for item in _seq(raw.get("registries")) if isinstance(item, Mapping)
    )
    sidecars: list[SidecarSpec] = []
    for item in _seq(raw.get("sidecars")):
        if not isinstance(item, Mapping):
            raise ValueError("execution.sidecars entries must be mappings")
        command = tuple(str(part) for part in _seq(item.get("command")))
        port = item.get("port")
        sidecar_id = str(item.get("id") or "").strip()
        if not sidecar_id or not command or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise ValueError("execution.sidecars entries need id, command and a port in 1024-65535")
        sidecars.append(SidecarSpec(sidecar_id, command, port, float(item.get("ready_timeout", 10.0))))
    fixtures = tuple(
        FixtureSpec(_safe_rel(str(item), field_name="execution.fixtures"))
        for item in _seq(raw.get("fixtures"))
        if str(item).strip()
    )
    not_verified = tuple(
        UnverifiedIntegration(str(item)[:200], "declared by trusted-base policy as not exercisable in the sandbox")
        for item in _seq(raw.get("not_verified"))
        if str(item).strip()
    )
    days = raw.get("retention_days")
    retention = DEFAULT_RAW_RETENTION_SECONDS
    if days is not None:
        if not isinstance(days, (int, float)) or isinstance(days, bool) or days <= 0:
            raise ValueError("execution.retention_days must be a positive number")
        retention = int(float(days) * 24 * 3600)
    deny_all = raw.get("deny_all", False)
    if not isinstance(deny_all, bool):
        raise ValueError("execution.deny_all must be boolean")
    if deny_all:
        effective: tuple[RegistryRule, ...] = ()
    elif public_defaults:
        effective = _with_public_defaults(registries)
    else:
        effective = registries
    return HermeticPolicy(
        registries=effective,
        deny_all=deny_all,
        sidecars=tuple(sidecars),
        fixtures=fixtures,
        not_verified=not_verified,
        redaction=RedactionPolicy.from_config(raw),
        retention_seconds=retention,
        source=source,
    )


def load_hermetic_policy(
    trusted_review_config: str | None,
    *,
    organization: Mapping[str, Any] | None = None,
) -> HermeticPolicy:
    """Resolve the hermetic policy from trusted-base configuration text only.

    ``trusted_review_config`` is the trusted base's ``.lemoncrow/review.yaml``
    content (``None``/empty when the base has none). Head content must never be
    passed here. When ``organization`` declares ``registries``, the organization
    list is the registry ceiling: a trusted-base registry not also allowed by
    the organization is dropped, and an ecosystem the organization does not
    list has no registry. ``deny_all`` from either layer allows none.
    """

    base: HermeticPolicy = HermeticPolicy()
    if trusted_review_config:
        try:
            decoded = yaml.safe_load(trusted_review_config)
        except yaml.YAMLError as exc:
            raise ValueError(f"trusted-base review config is not valid YAML: {exc}") from exc
        block = decoded.get("execution") if isinstance(decoded, Mapping) else None
        if block is not None:
            if not isinstance(block, Mapping):
                raise ValueError("trusted-base execution policy must be a mapping")
            base = _parse_execution_block(block, source="trusted_base")
    if not organization:
        return base
    org = _parse_execution_block(organization, source="organization", public_defaults=False)
    deny_all = org.deny_all or base.deny_all
    if deny_all:
        registries: tuple[RegistryRule, ...] = ()
    elif org.registries:
        allowed = {rule.key for rule in org.registries}
        merged: list[RegistryRule] = []
        for ecosystem in ECOSYSTEMS:
            kept = [rule for rule in base.registries if rule.ecosystem == ecosystem and rule.key in allowed]
            merged.extend(kept or [rule for rule in org.registries if rule.ecosystem == ecosystem])
        registries = tuple(merged)
    else:
        registries = base.registries
    return HermeticPolicy(
        registries=registries,
        deny_all=deny_all,
        sidecars=base.sidecars,
        fixtures=base.fixtures,
        not_verified=(*org.not_verified, *base.not_verified),
        redaction=RedactionPolicy(
            patterns=(*org.redaction.patterns, *base.redaction.patterns),
            literals=(*org.redaction.literals, *base.redaction.literals),
            mask_regions=(*org.redaction.mask_regions, *base.redaction.mask_regions),
        ),
        retention_seconds=min(org.retention_seconds, base.retention_seconds),
        source="organization+" + base.source,
    )


def hermetic_policy_from_wire(raw: Mapping[str, Any]) -> HermeticPolicy:
    """Rebuild a recorded policy (reproduction). Literal secrets are not recorded."""

    if int(raw.get("version") or 0) != HERMETIC_POLICY_VERSION:
        raise ValueError("recorded hermetic policy version is not supported")
    redaction = raw.get("redaction") if isinstance(raw.get("redaction"), Mapping) else {}
    block: dict[str, Any] = {
        "registries": [item for item in _seq(raw.get("registries")) if isinstance(item, Mapping)],
        "sidecars": [item for item in _seq(raw.get("sidecars")) if isinstance(item, Mapping)],
        "fixtures": [str(item.get("path")) for item in _seq(raw.get("fixtures")) if isinstance(item, Mapping)],
        "not_verified": [str(item.get("name")) for item in _seq(raw.get("not_verified")) if isinstance(item, Mapping)],
        "redact": list(_seq(cast(Mapping[str, Any], redaction).get("patterns"))),
        "mask": list(_seq(cast(Mapping[str, Any], redaction).get("mask"))),
        "retention_days": max(1, int(raw.get("retention_seconds") or DEFAULT_RAW_RETENTION_SECONDS)) / 86400,
        "deny_all": raw.get("deny_all") is True,
    }
    # The recorded registry list is already the effective allowlist: no defaults are re-added.
    policy = _parse_execution_block(block, source=str(raw.get("source") or "recorded"), public_defaults=False)
    return policy


# --------------------------------------------------------------------------- #
# npm lockfile plan
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class PlannedPackage:
    path: str
    name: str
    version: str
    url: str
    integrity: str
    has_install_script: bool = False

    @property
    def strongest(self) -> tuple[str, bytes]:
        return _strongest_integrity(self.integrity)


@dataclass(frozen=True, slots=True)
class InstallPlan:
    """Validated, policy-checked npm install plan; the only input of fetch/build."""

    packages: tuple[PlannedPackage, ...]
    lockfile_digest: str
    skipped: tuple[str, ...] = ()
    blocked_scripts: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        return canonical_digest(
            {
                "extractor": EXTRACTOR_VERSION,
                "packages": [[pkg.path, pkg.name, pkg.version, pkg.integrity] for pkg in self.packages],
            }
        )


def _strongest_integrity(sri: str) -> tuple[str, bytes]:
    found: dict[str, bytes] = {}
    for token in sri.split():
        algorithm, _, value = token.partition("-")
        if algorithm not in _SRI_ALGORITHMS or not value:
            continue
        try:
            found[algorithm] = base64.b64decode(value.split("?", 1)[0], validate=True)
        except (binascii.Error, ValueError):
            continue
    for algorithm in _SRI_ALGORITHMS:
        if algorithm in found and len(found[algorithm]) == hashlib.new(algorithm).digest_size:
            return algorithm, found[algorithm]
    raise HermeticRefused("package integrity must include sha512, sha384 or sha256; sha1-only is refused")


_LOCKFILES: Final[tuple[str, ...]] = (
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
    "bun.lock",
    "pyproject.toml",
    "uv.lock",
    "poetry.lock",
    "Pipfile.lock",
    "pdm.lock",
    "requirements.txt",
    "Cargo.lock",
    "go.sum",
    "Gemfile.lock",
    "composer.lock",
)
REPRODUCE_COMMAND: Final[str] = "lc review --reproduce <evidence-id|record.json>"


def lockfile_digest(project: Path) -> str:
    """Digest of every recognised dependency manifest/lockfile directly in *project*."""

    digest = hashlib.sha256()
    found = False
    extra = tuple(sorted(item.name for item in project.glob("requirements*.txt") if item.name != "requirements.txt"))
    for name in (*_LOCKFILES, *extra):
        path = project / name
        if path.is_file() and not path.is_symlink():
            found = True
            digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest() if found else ""


def npm_dependency_digest(project: Path) -> str:
    """Digest of the declared dependency graph and lockfile bytes (fingerprint input)."""

    digest = hashlib.sha256()
    for name in ("package.json", _LOCKFILE):
        path = project / name
        if path.is_file():
            digest.update(name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def _expected_tarball_suffix(name: str, version: str) -> str:
    unscoped = name.split("/", 1)[1] if name.startswith("@") else name
    return f"/{name}/-/{unscoped}-{version}.tgz"


def _platform_matches(entry: Mapping[str, Any]) -> bool:
    system = {"Linux": "linux", "Darwin": "darwin", "Windows": "win32"}.get(
        platform.system(), platform.system().lower()
    )
    machine = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    for key, actual in (("os", system), ("cpu", machine)):
        allowed = entry.get(key)
        if not isinstance(allowed, list) or not allowed:
            continue
        values = [str(item) for item in allowed]
        if any(item == f"!{actual}" for item in values):
            return False
        positives = [item for item in values if not item.startswith("!")]
        if positives and actual not in positives:
            return False
    return True


def plan_npm_install(project: Path, policy: HermeticPolicy) -> InstallPlan:
    """Validate the reviewed lockfile against trusted policy; refuse anything unsafe."""

    lock_path = project / _LOCKFILE
    if not (project / "package.json").is_file():
        raise HermeticRefused("the reviewed project has no package.json")
    policy.require_fetch("npm")
    if not lock_path.is_file():
        # lc-debt: only npm package-lock v2/v3 is supported hermetically; pnpm/yarn/bun lockfiles are refused.
        raise HermeticRefused("hermetic dependency provisioning needs a package-lock.json (lockfileVersion 2 or 3)")
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        manifest = json.loads((project / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HermeticRefused(f"the reviewed package metadata is unreadable: {exc}") from exc
    if not isinstance(lock, Mapping) or int(lock.get("lockfileVersion") or 0) < 2:
        raise HermeticRefused("package-lock.json must be lockfileVersion 2 or 3")
    entries = lock.get("packages")
    if not isinstance(entries, Mapping):
        raise HermeticRefused("package-lock.json has no packages map")
    if len(entries) > MAX_PACKAGES:
        raise HermeticRefused(f"package-lock.json lists more than {MAX_PACKAGES} packages")
    blocked: list[str] = []
    root_scripts = manifest.get("scripts") if isinstance(manifest, Mapping) else None
    if isinstance(root_scripts, Mapping):
        blocked.extend(f"(project): {name}" for name in _LIFECYCLE if name in root_scripts)
    packages: list[PlannedPackage] = []
    skipped: list[str] = []
    for key, raw in sorted(entries.items()):
        if key == "" or not isinstance(raw, Mapping):
            continue
        path = str(key)
        pure = PurePosixPath(path)
        if pure.is_absolute() or ".." in pure.parts or "node_modules" not in pure.parts:
            raise HermeticRefused(f"lockfile entry {path!r} is not a node_modules path")
        if raw.get("link"):
            # lc-debt: workspace links (npm workspaces) are refused; supporting them needs a workspace-aware mount.
            raise HermeticRefused(f"lockfile entry {path} is a workspace link; hermetic provisioning refuses links")
        if raw.get("inBundle"):
            continue
        name = str(raw.get("name") or path.rsplit("node_modules/", 1)[-1])
        version = str(raw.get("version") or "")
        if not _PACKAGE_NAME_RE.match(name) or not version or any(ch in version for ch in "/\\ "):
            raise HermeticRefused(f"lockfile entry {path} has an invalid name or version")
        if (raw.get("optional") or raw.get("devOptional")) and not _platform_matches(raw):
            skipped.append(f"{name}@{version} (platform)")
            continue
        url = str(raw.get("resolved") or "")
        integrity = str(raw.get("integrity") or "")
        if not url or not integrity:
            raise HermeticRefused(f"lockfile entry {name}@{version} has no resolved URL and integrity")
        rule = policy.registry_for(url, "npm")
        if rule is None:
            raise HermeticRefused(f"{name}@{version} resolves outside the allowlisted registries: {url}")
        pinned = policy.pinned_registry(name, "npm")
        if pinned is not None and pinned is not rule:
            raise HermeticRefused(
                f"{name} is pinned to registry {pinned.host} by trusted policy but resolves from {rule.host} "
                "(dependency confusion)"
            )
        if pinned is None and (rule.scopes or rule.packages) and not rule.pins(name):
            # A private registry serves only the names it is pinned for.
            raise HermeticRefused(f"{name} is not a package this registry ({rule.host}) is allowed to serve")
        if not unquote(urlsplit(url).path).endswith(_expected_tarball_suffix(name, version)):
            raise HermeticRefused(f"{name}@{version} resolves to a tarball for a different package: {url}")
        _strongest_integrity(integrity)
        if raw.get("hasInstallScript"):
            blocked.append(f"{name}@{version}: install scripts")
        packages.append(PlannedPackage(path, name, version, url, integrity, bool(raw.get("hasInstallScript"))))
    return InstallPlan(
        packages=tuple(packages),
        lockfile_digest=npm_dependency_digest(project),
        skipped=tuple(skipped),
        blocked_scripts=tuple(blocked),
    )


# --------------------------------------------------------------------------- #
# Registry-only fetcher
# --------------------------------------------------------------------------- #


def _connect_pinned(addresses: Sequence[str], port: int, timeout: float | None) -> socket.socket:
    last: OSError | None = None
    for address in addresses:
        try:
            return socket.create_connection((address, port), timeout)
        except OSError as exc:
            last = exc
    raise last or OSError("no registry address")


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, port: int, addresses: Sequence[str], timeout: float) -> None:
        super().__init__(host, port, timeout=timeout)
        self._addresses = tuple(addresses)

    def connect(self) -> None:
        self.sock = _connect_pinned(self._addresses, self.port, self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, port: int, addresses: Sequence[str], timeout: float) -> None:
        self._tls = ssl.create_default_context()
        super().__init__(host, port, timeout=timeout, context=self._tls)
        self._addresses = tuple(addresses)

    def connect(self) -> None:
        raw = _connect_pinned(self._addresses, self.port, self.timeout)
        self.sock = self._tls.wrap_socket(raw, server_hostname=self.host)


def _registry_diagnostic_target(url: str) -> str:
    """Show only a bounded registry origin, never signed paths or query tokens."""
    try:
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            return "<unidentifiable registry>"
        hostname = parts.hostname[:180]
        if not re.fullmatch(r"[a-zA-Z0-9.:-]+", hostname):
            return "<unidentifiable registry>"
        port = parts.port
        return f"{parts.scheme}://{hostname}{':' + str(port) if port is not None else ''}"
    except ValueError:
        return "<unidentifiable registry>"


class RegistryFetcher:
    """The fetch stage's only network client: allowlisted registries, nothing else.

    Ambient proxy variables are ignored (``http.client`` never reads them), DNS
    answers are resolved once, checked, and pinned for the connection, and
    every redirect hop is re-checked against the allowlist before it is
    followed.
    """

    def __init__(
        self,
        policy: HermeticPolicy,
        *,
        timeout: float = FETCH_TIMEOUT_SECONDS,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.policy = policy
        self.timeout = timeout
        self.should_cancel = should_cancel

    def _addresses(self, rule: RegistryRule, host: str, port: int) -> tuple[str, ...]:
        try:
            answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except OSError as exc:
            raise HermeticUnavailable(f"registry {host} does not resolve: {exc}") from exc
        addresses = tuple(dict.fromkeys(str(answer[4][0]) for answer in answers))
        if not addresses:
            raise HermeticUnavailable(f"registry {host} has no addresses")
        for address in addresses:
            ip = ipaddress.ip_address(address.split("%", 1)[0])
            if not rule.private_network and not ip.is_global:
                raise HermeticRefused(
                    f"registry {host} resolves to non-public address {address}; refusing (DNS rebinding guard)"
                )
            if ip.is_multicast or ip.is_unspecified:
                raise HermeticRefused(f"registry {host} resolves to unusable address {address}")
        return addresses

    def fetch(self, url: str, *, max_bytes: int = MAX_TARBALL_BYTES) -> bytes:
        return self.fetch_page(url, max_bytes=max_bytes)[0]

    def fetch_page(
        self, url: str, *, max_bytes: int = MAX_TARBALL_BYTES, accept: str = "application/octet-stream"
    ) -> tuple[bytes, str, str]:
        """``(body, content type, final URL)``; every hop must be an allowlisted registry."""

        current = url
        for _hop in range(MAX_REDIRECTS + 1):
            rule = self.policy.registry_for(current)
            if rule is None:
                raise HermeticRefused(
                    f"fetch target is not an allowlisted registry: {_registry_diagnostic_target(current)}"
                )
            parts = urlsplit(current)
            host = parts.hostname or ""
            addresses = self._addresses(rule, host, rule.effective_port)
            connection: http.client.HTTPConnection = (
                _PinnedHTTPSConnection(host, rule.effective_port, addresses, self.timeout)
                if rule.scheme == "https"
                else _PinnedHTTPConnection(host, rule.effective_port, addresses, self.timeout)
            )
            target = parts.path + (f"?{parts.query}" if parts.query else "")
            # Only the leased-worker path watches revocation. A blocked
            # registry socket must not keep a cancelled assignment running
            # until its full request timeout. Shut down the pinned socket from
            # the trusted host; never pass the callback to the remote server.
            revoked = threading.Event()
            # HTTP/1.0 can detach the socket from HTTPConnection when the
            # response is created. Keep its reference so shutdown interrupts
            # a stalled HTTPResponse.read even after connection.sock is None.
            active_socket: list[socket.socket | None] = [None]
            finished = threading.Event()
            watcher: threading.Thread | None = None
            if self.should_cancel is not None:

                def watch_registry(
                    *,
                    done: threading.Event = finished,
                    cancelled: threading.Event = revoked,
                    conn: http.client.HTTPConnection = connection,
                    sock_ref: list[socket.socket | None] = active_socket,
                ) -> None:
                    while not done.wait(0.1):
                        if self.should_cancel is not None and self.should_cancel():
                            cancelled.set()
                            sock = sock_ref[0] or conn.sock
                            if sock is not None:
                                try:
                                    sock.shutdown(socket.SHUT_RDWR)
                                except OSError:
                                    pass
                            conn.close()
                            return

                watcher = threading.Thread(target=watch_registry, daemon=True)
                watcher.start()
            try:
                if self.should_cancel is not None and self.should_cancel():
                    raise HermeticUnavailable("registry fetch cancelled by revoked authority")
                connection.request(
                    "GET",
                    target or "/",
                    headers={"User-Agent": "lemoncrow-hermetic-fetch/1", "Accept": accept},
                )
                active_socket[0] = connection.sock
                if revoked.is_set():
                    raise HermeticUnavailable("registry fetch cancelled by revoked authority")
                response = connection.getresponse()
                if response.status in {301, 302, 303, 307, 308}:
                    location = response.getheader("Location") or ""
                    response.read(64 * 1024)
                    if not location:
                        raise HermeticRefused(
                            f"registry redirect without Location from {_registry_diagnostic_target(current)}"
                        )
                    current = urljoin(current, location)
                    continue
                if response.status != 200:
                    raise HermeticUnavailable(
                        f"registry answered HTTP {response.status} for {_registry_diagnostic_target(current)}"
                    )
                declared = response.getheader("Content-Length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    raise HermeticRefused(f"tarball exceeds {max_bytes} bytes: {_registry_diagnostic_target(current)}")
                payload = response.read(max_bytes + 1)
                if revoked.is_set() or (self.should_cancel is not None and self.should_cancel()):
                    raise HermeticUnavailable("registry fetch cancelled by revoked authority")
                if len(payload) > max_bytes:
                    raise HermeticRefused(f"tarball exceeds {max_bytes} bytes: {_registry_diagnostic_target(current)}")
                return payload, str(response.getheader("Content-Type") or ""), current
            except (OSError, http.client.HTTPException) as exc:
                if revoked.is_set() or (self.should_cancel is not None and self.should_cancel()):
                    raise HermeticUnavailable("registry fetch cancelled by revoked authority") from exc
                raise HermeticUnavailable(
                    f"registry fetch failed for {_registry_diagnostic_target(current)} ({type(exc).__name__})"
                ) from exc
            finally:
                finished.set()
                connection.close()
                if watcher is not None:
                    watcher.join(timeout=1.0)
        raise HermeticRefused(
            f"registry redirected more than {MAX_REDIRECTS} times: {_registry_diagnostic_target(url)}"
        )


def _verify_integrity(payload: bytes, integrity: str) -> None:
    algorithm, expected = _strongest_integrity(integrity)
    if hashlib.new(algorithm, payload).digest() != expected:
        raise HermeticRefused("downloaded tarball does not match its lockfile integrity")


# --------------------------------------------------------------------------- #
# Offline extractor (runs inside bubblewrap with every namespace unshared)
# --------------------------------------------------------------------------- #

_INSTALL_SCRIPT: Final[str] = r"""
import hashlib, io, json, os, posixpath, sys, tarfile
plan = json.load(open(sys.argv[1], encoding="utf-8"))
tar_root, out = sys.argv[2], sys.argv[3]
limit_total, limit_file, limit_entries = plan["max_total_bytes"], plan["max_file_bytes"], plan["max_entries"]
LIFECYCLE = ("preinstall", "install", "postinstall", "prepare")
total = entries = 0
blocked = []
def fail(message):
    print(json.dumps({"ok": False, "error": message}))
    sys.exit(3)
def locate(digest):
    for name in sorted(os.listdir(tar_root)):
        candidate = os.path.join(tar_root, name, digest + ".tgz")
        if os.path.isfile(candidate):
            return candidate
    return None
def inside(base, target):
    base = os.path.realpath(base)
    target = os.path.realpath(target)
    return target == base or target.startswith(base + os.sep)
out_modules = os.path.join(out, "node_modules")
for pkg in plan["packages"]:
    rel = posixpath.normpath(pkg["path"])
    parts = rel.split("/")
    if rel.startswith("/") or ".." in parts or parts[0] != "node_modules":
        fail("unsafe install path " + rel)
    source = locate(pkg["sha512"])
    if source is None:
        fail("missing fetched tarball for " + pkg["name"])
    data = open(source, "rb").read()
    if hashlib.sha512(data).hexdigest() != pkg["sha512"]:
        fail("fetched tarball changed after verification: " + pkg["name"])
    dest = os.path.join(out, *parts)
    os.makedirs(dest, exist_ok=True)
    if not inside(out_modules, dest):
        fail("install path escapes node_modules: " + rel)
    manifest = None
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive:
            entries += 1
            if entries > limit_entries:
                fail("dependency tree has too many entries")
            name = member.name.replace("\\", "/")
            pieces = [piece for piece in name.split("/") if piece not in ("", ".")][1:]
            if not pieces:
                continue
            if name.startswith("/") or ".." in pieces:
                fail("tarball member escapes its package: " + pkg["name"] + ": " + name)
            target = os.path.join(dest, *pieces)
            if member.isdir():
                os.makedirs(target, exist_ok=True)
                continue
            if not member.isreg():
                continue
            if member.size > limit_file:
                fail("tarball member is too large: " + pkg["name"])
            total += member.size
            if total > limit_total:
                fail("dependency tree exceeds the size limit")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            if not inside(dest, os.path.dirname(target)):
                fail("tarball member escapes its package: " + pkg["name"])
            handle = archive.extractfile(member)
            body = handle.read() if handle is not None else b""
            with open(target, "wb") as output:
                output.write(body)
            os.chmod(target, 0o755 if member.mode & 0o111 else 0o644)
            if pieces == ["package.json"]:
                try:
                    manifest = json.loads(body.decode("utf-8"))
                except ValueError:
                    fail("package.json is not JSON in " + pkg["name"])
    if not isinstance(manifest, dict):
        fail("tarball has no package.json: " + pkg["name"])
    if manifest.get("name") != pkg["name"] or str(manifest.get("version")) != pkg["version"]:
        fail("tarball for %s@%s contains %s@%s (lookalike package)" % (
            pkg["name"], pkg["version"], manifest.get("name"), manifest.get("version")))
    scripts = manifest.get("scripts") if isinstance(manifest.get("scripts"), dict) else {}
    for script in LIFECYCLE:
        if script in scripts:
            blocked.append("%s@%s: %s" % (pkg["name"], pkg["version"], script))
    bins = manifest.get("bin")
    if isinstance(bins, str):
        bins = {pkg["name"].split("/")[-1]: bins}
    if isinstance(bins, dict):
        last_modules = len(parts) - 1 - parts[::-1].index("node_modules")
        modules_dir = os.path.join(out, *parts[: last_modules + 1])
        bin_dir = os.path.join(modules_dir, ".bin")
        for bin_name, bin_target in sorted(bins.items()):
            bin_name = str(bin_name).split("/")[-1]
            if not bin_name or bin_name in (".", "..") or not isinstance(bin_target, str):
                continue
            resolved = os.path.normpath(os.path.join(dest, bin_target))
            if not inside(dest, resolved) or not os.path.isfile(resolved):
                continue
            os.makedirs(bin_dir, exist_ok=True)
            link = os.path.join(bin_dir, bin_name)
            if os.path.lexists(link):
                os.unlink(link)
            os.symlink(os.path.relpath(resolved, bin_dir), link)
            os.chmod(resolved, 0o755)
print(json.dumps({"ok": True, "packages": len(plan["packages"]), "bytes": total, "blocked": blocked}))
"""


def _bwrap() -> str:
    found = shutil.which("bwrap") or shutil.which("bubblewrap")
    if not found:
        raise HermeticUnavailable("hermetic stages require bubblewrap; refusing to run unsandboxed")
    return found


def offline_bwrap_prefix(*, unshare_all: bool = True) -> list[str]:
    """Base bubblewrap argv for an offline stage: runtime read-only, nothing else.

    No host home, credentials, ``/etc`` secrets, Docker socket or ambient
    environment are mounted or inherited.
    """

    command = [_bwrap(), "--die-with-parent", "--new-session"]
    command.extend(("--unshare-all",) if unshare_all else ("--unshare-net", "--unshare-pid"))
    command.extend(("--proc", "/proc", "--dev", "/dev"))
    for mount in _RUNTIME_MOUNTS:
        if Path(mount).exists():
            command.extend(("--ro-bind", mount, mount))
    command.extend(
        (
            "--tmpfs",
            "/tmp",
            "--dir",
            "/home",
            "--clearenv",
            "--setenv",
            "PATH",
            _SANDBOX_PATH,
            "--setenv",
            "HOME",
            "/tmp",
            "--setenv",
            "LC_ALL",
            "C.UTF-8",
        )
    )
    return command


# --------------------------------------------------------------------------- #
# Dependency provisioning (fetch + offline install) with trust-isolated caches
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ProvisionedTree:
    node_modules: Path
    plan: InstallPlan
    stages: tuple[StageOutcome, ...]


_PRUNE_GUARD = threading.Lock()
_LAST_PRUNE: dict[str, float] = {}
_TREE_LOCKS: dict[str, threading.Lock] = {}


def _tree_lock(key: str) -> threading.Lock:
    # lc-debt: process-local lock map grows with distinct plan digests; bounded by trees provisioned per process.
    with _PRUNE_GUARD:
        return _TREE_LOCKS.setdefault(key, threading.Lock())


class DependencyCache:
    """Fetch/build caches under ``<store>/review/hermetic/<namespace>``.

    A run writes only to its own namespace and reads only the namespaces
    :func:`readable_cache_namespaces` allows. Writers touch entries on use and
    :meth:`prune` (run by the writer, amortized) is the closer.
    """

    def __init__(self, root: Path, namespace: CacheNamespace) -> None:
        self.root = root
        self.namespace = namespace

    @staticmethod
    def for_store(store_root: Path, namespace: CacheNamespace) -> DependencyCache:
        return DependencyCache(store_root / "review" / "hermetic", namespace)

    def _dir(self, namespace: CacheNamespace, kind: str) -> Path:
        return self.root / namespace / kind

    @property
    def writable_tarballs(self) -> Path:
        return self._dir(self.namespace, "tarballs")

    def readable_tarball_dirs(self) -> tuple[Path, ...]:
        return tuple(self._dir(ns, "tarballs") for ns in readable_cache_namespaces(self.namespace))

    def tarball(self, sha512_hex: str) -> Path | None:
        for directory in self.readable_tarball_dirs():
            candidate = directory / f"{sha512_hex}.tgz"
            try:
                if candidate.is_file() and hashlib.sha512(candidate.read_bytes()).hexdigest() == sha512_hex:
                    os.utime(candidate)
                    return candidate
            except OSError:
                continue
        return None

    def store_tarball(self, sha512_hex: str, payload: bytes) -> Path:
        return self._store_blob(self.writable_tarballs, f"{sha512_hex}.tgz", payload)

    @property
    def writable_wheels(self) -> Path:
        return self._dir(self.namespace, "wheels")

    def readable_wheel_dirs(self) -> tuple[Path, ...]:
        return tuple(self._dir(ns, "wheels") for ns in readable_cache_namespaces(self.namespace))

    def wheel(self, sha256_hex: str) -> Path | None:
        """A cached wheel whose bytes still hash to *sha256_hex*."""

        for directory in self.readable_wheel_dirs():
            candidate = directory / f"{sha256_hex}.whl"
            try:
                if candidate.is_file() and hashlib.sha256(candidate.read_bytes()).hexdigest() == sha256_hex:
                    os.utime(candidate)
                    return candidate
            except OSError:
                continue
        return None

    def store_wheel(self, sha256_hex: str, payload: bytes) -> Path:
        return self._store_blob(self.writable_wheels, f"{sha256_hex}.whl", payload)

    @staticmethod
    def _store_blob(directory: Path, name: str, payload: bytes) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / name
        tmp = directory / f".{name}.{uuid.uuid4().hex}.tmp"
        tmp.write_bytes(payload)
        tmp.replace(target)
        return target

    def tree(self, plan_digest: str, content: str = "node_modules") -> Path | None:
        for namespace in readable_cache_namespaces(self.namespace):
            entry = self._dir(namespace, "trees") / plan_digest
            try:
                marker = json.loads((entry / "tree.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if marker.get("plan_digest") == plan_digest and (entry / content).is_dir():
                try:
                    os.utime(entry)
                except OSError:
                    pass
                return entry / content
        return None

    def new_tree_dir(self, plan_digest: str) -> tuple[Path, Path]:
        trees = self._dir(self.namespace, "trees")
        trees.mkdir(parents=True, exist_ok=True)
        staging = trees / f".{plan_digest}.{uuid.uuid4().hex}.tmp"
        staging.mkdir()
        return staging, trees / plan_digest

    def prune(self, *, force: bool = False) -> None:
        """Closer: drop idle trees beyond the per-namespace budget and idle tarballs."""

        key = str(self.root.resolve()) if self.root.exists() else str(self.root)
        now = time.time()
        with _PRUNE_GUARD:
            if not force and now - _LAST_PRUNE.get(key, 0.0) < _PRUNE_EVERY_SECONDS:
                return
            _LAST_PRUNE[key] = now
        for namespace in readable_cache_namespaces(self.namespace)[:1]:
            trees = self._dir(namespace, "trees")
            try:
                rows = sorted(
                    (item for item in trees.iterdir() if item.is_dir()),
                    key=lambda item: item.stat().st_mtime,
                    reverse=True,
                )
            except OSError:
                rows = []
            for index, entry in enumerate(rows):
                try:
                    idle = now - entry.stat().st_mtime
                except OSError:
                    continue
                stale_tmp = entry.name.startswith(".") and idle > 3600
                if (
                    stale_tmp
                    or (index >= KEEP_TREES_PER_NAMESPACE and idle >= TREE_IDLE_SECONDS)
                    or idle >= 4 * TREE_IDLE_SECONDS
                ):
                    shutil.rmtree(entry, ignore_errors=True)
            for kind in ("tarballs", "wheels"):
                try:
                    for item in self._dir(namespace, kind).iterdir():
                        if now - item.stat().st_mtime >= TARBALL_IDLE_SECONDS:
                            item.unlink(missing_ok=True)
                except OSError:
                    continue


def _stage_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def provision_npm_dependencies(
    project: Path,
    *,
    policy: HermeticPolicy,
    cache: DependencyCache,
    fetcher: RegistryFetcher | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ProvisionedTree:
    """Fetch (registry-only) then build (offline) the reviewed lockfile's dependency tree.

    Raises :class:`HermeticRefused` for policy violations and
    :class:`HermeticUnavailable` when the host or registry cannot serve.
    """

    started = time.monotonic()
    plan = plan_npm_install(project, policy)
    blocked = (*plan.blocked_scripts, *(f"skipped {item}" for item in plan.skipped))
    existing = cache.tree(plan.digest)
    if existing is not None:
        hit = (
            StageOutcome("fetch", "skipped", network="registry_only", detail="dependency tree cache hit", cache="hit"),
            StageOutcome("build", "passed", detail="dependency tree cache hit", cache="hit", blocked=blocked),
        )
        return ProvisionedTree(existing, plan, hit)

    client = fetcher or RegistryFetcher(policy, should_cancel=should_cancel)
    missing: list[PlannedPackage] = []
    digests: dict[str, str] = {}
    for pkg in plan.packages:
        algorithm, expected = pkg.strongest
        cached = None
        if algorithm == "sha512":
            digests[pkg.path] = expected.hex()
            cached = cache.tarball(expected.hex())
        if cached is None:
            missing.append(pkg)

    fetched_bytes = 0
    byte_guard = threading.Lock()

    def fetch_one(pkg: PlannedPackage) -> tuple[str, str]:
        nonlocal fetched_bytes
        if should_cancel is not None and should_cancel():
            raise HermeticUnavailable("dependency fetch cancelled by revoked authority")
        payload = client.fetch(pkg.url)
        if should_cancel is not None and should_cancel():
            raise HermeticUnavailable("dependency fetch cancelled by revoked authority")
        _verify_integrity(payload, pkg.integrity)
        with byte_guard:
            fetched_bytes += len(payload)
            if fetched_bytes > MAX_TOTAL_FETCH_BYTES:
                raise HermeticRefused(f"dependency fetch exceeds {MAX_TOTAL_FETCH_BYTES} bytes")
        sha512_hex = hashlib.sha512(payload).hexdigest()
        cache.store_tarball(sha512_hex, payload)
        return pkg.path, sha512_hex

    if missing:
        with ThreadPoolExecutor(max_workers=min(FETCH_CONCURRENCY, len(missing))) as pool:
            for path, sha512_hex in pool.map(fetch_one, missing):
                digests[path] = sha512_hex
    fetch_stage = StageOutcome(
        "fetch",
        "passed",
        network="registry_only",
        detail=(
            f"{len(missing)} of {len(plan.packages)} package(s) fetched from "
            f"{', '.join(sorted({urlsplit(pkg.url).hostname or '' for pkg in missing})) or 'cache'}; "
            "no repository code ran"
        ),
        duration_ms=_stage_ms(started),
        blocked=blocked,
        cache="miss" if missing else "hit",
    )

    if should_cancel is not None and should_cancel():
        raise HermeticUnavailable("dependency build cancelled by revoked authority")
    build_started = time.monotonic()
    with _tree_lock(f"{cache.root}:{cache.namespace}:{plan.digest}"):
        existing = cache.tree(plan.digest)
        if existing is not None:
            return ProvisionedTree(existing, plan, (fetch_stage, StageOutcome("build", "passed", cache="hit")))
        staging, final = cache.new_tree_dir(plan.digest)
        try:
            report = _run_offline_install(plan, digests, cache, staging, should_cancel=should_cancel)
            (staging / "node_modules").mkdir(exist_ok=True)
            (staging / "tree.json").write_text(
                json.dumps({"plan_digest": plan.digest, "namespace": cache.namespace, "created_at": time.time()}),
                encoding="utf-8",
            )
            if final.exists():
                shutil.rmtree(final, ignore_errors=True)
            staging.replace(final)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    cache.prune()
    build_blocked = tuple(dict.fromkeys((*blocked, *(str(item) for item in report.get("blocked", ())))))
    build_stage = StageOutcome(
        "build",
        "passed",
        detail=f"{report.get('packages', 0)} package(s) unpacked offline; lifecycle scripts never executed",
        duration_ms=_stage_ms(build_started),
        blocked=build_blocked,
        cache="miss",
    )
    return ProvisionedTree(final / "node_modules", plan, (fetch_stage, build_stage))


def _run_offline_install(
    plan: InstallPlan,
    digests: Mapping[str, str],
    cache: DependencyCache,
    staging: Path,
    *,
    should_cancel: Callable[[], bool] | None = None,
) -> Mapping[str, Any]:
    from lemoncrow.pro.capabilities.review.sandbox_limits import (
        SandboxCapacityBusy,
        SandboxExecutionCancelled,
        run_limited,
        run_limited_bounded,
    )

    plan_file = staging.parent / f"{staging.name}.plan.json"
    plan_file.write_text(
        json.dumps(
            {
                "max_total_bytes": MAX_TOTAL_FETCH_BYTES * 4,
                "max_file_bytes": MAX_TARBALL_BYTES,
                "max_entries": 500_000,
                "packages": [
                    {"path": pkg.path, "name": pkg.name, "version": pkg.version, "sha512": digests[pkg.path]}
                    for pkg in plan.packages
                ],
            }
        ),
        encoding="utf-8",
    )
    command = offline_bwrap_prefix()
    command.extend(("--ro-bind", str(plan_file), "/lemoncrow-plan.json", "--dir", "/lemoncrow-tarballs"))
    for index, directory in enumerate(cache.readable_tarball_dirs()):
        if directory.is_dir():
            command.extend(("--ro-bind", str(directory), f"/lemoncrow-tarballs/{index}"))
    command.extend(
        (
            "--bind",
            str(staging),
            "/out",
            "--chdir",
            "/out",
            "--",
            "python3",
            "-I",
            "-S",
            "-c",
            _INSTALL_SCRIPT,
            "/lemoncrow-plan.json",
            "/lemoncrow-tarballs",
            "/out",
        )
    )
    try:
        if should_cancel is None:
            # Keep the original author-machine capture and installer diagnostics.
            proc = run_limited(
                command,
                timeout=INSTALL_TIMEOUT_SECONDS,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
                env={"PATH": _SANDBOX_PATH},
            )
        else:
            # Signed leased workers need cooperative process-group cancellation
            # and bounded output even while the offline installer is running.
            proc = run_limited_bounded(
                command,
                timeout=INSTALL_TIMEOUT_SECONDS,
                max_output_bytes=65536,
                should_cancel=should_cancel,
                env={"PATH": _SANDBOX_PATH},
            )
    except SandboxExecutionCancelled as exc:
        raise HermeticUnavailable("offline dependency build cancelled by revoked authority") from exc
    except SandboxCapacityBusy as exc:
        raise HermeticUnavailable(str(exc)) from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HermeticUnavailable(f"offline dependency build could not run: {exc}") from exc
    finally:
        plan_file.unlink(missing_ok=True)
    lines = [line for line in (proc.stdout or "").splitlines() if line.startswith("{")]
    try:
        report = json.loads(lines[-1]) if lines else {}
    except ValueError:
        report = {}
    if proc.returncode != 0 or not report.get("ok"):
        error = str(
            report.get("error")
            or ((proc.stdout or "") + (proc.stderr or "")).strip()[-2000:]
            or f"exit {proc.returncode}"
        )
        if "lookalike" in error or "escapes" in error or "unsafe" in error:
            raise HermeticRefused(f"offline dependency build refused: {error}")
        raise HermeticUnavailable(f"offline dependency build failed: {error}")
    return cast(Mapping[str, Any], report)


# --------------------------------------------------------------------------- #
# Ecosystem dispatch (local command runner and worker plane share this)
# --------------------------------------------------------------------------- #

_UNSUPPORTED_LOCKFILES: Final[Mapping[str, str]] = {
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "Yarn",
    "bun.lockb": "Bun",
    "bun.lock": "Bun",
    "poetry.lock": "Poetry",
    "Pipfile.lock": "Pipenv",
    "pdm.lock": "PDM",
    "Cargo.lock": "Cargo (Rust)",
    "go.sum": "Go modules",
    "Gemfile.lock": "Bundler (Ruby)",
    "composer.lock": "Composer (PHP)",
}


def unsupported_lockfiles(project: Path) -> tuple[str, ...]:
    """Lockfiles in *project* whose ecosystem has no hermetic provisioner.

    Their presence refuses the run (NOT_RUN with this reason): the sandbox
    could only run them against whatever happens to be installed, which is a
    silent fallback.
    """

    # lc-debt: only npm package-lock and Python uv.lock/hashed requirements are provisioned; pnpm, Yarn, Bun, Poetry, Pipenv, PDM, Cargo, Go, Bundler and Composer are refused.
    return tuple(f"{name} ({tool})" for name, tool in _UNSUPPORTED_LOCKFILES.items() if (project / name).is_file())


def npm_project_state(project: Path) -> str:
    """``locked``, ``unlocked`` (dependencies declared, no package-lock) or ``""``."""

    if (project / _LOCKFILE).is_file():
        return "locked"
    manifest = project / "package.json"
    if not manifest.is_file():
        return ""
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "unlocked"
    declared = (
        (data.get(key) for key in ("dependencies", "devDependencies", "optionalDependencies"))
        if isinstance(data, Mapping)
        else ()
    )
    return "unlocked" if any(declared) else ""


@dataclass(frozen=True, slots=True)
class ProvisionedDependencies:
    """Everything the run stage needs from fetch/build: read-only mounts and environment."""

    stages: tuple[StageOutcome, ...] = ()
    mounts: tuple[tuple[Path, str], ...] = ()
    path_prefix: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    lockfile_digest: str = ""
    ecosystems: tuple[str, ...] = ()

    def bwrap_args(self, base_path: str = _SANDBOX_PATH) -> list[str]:
        """Read-only mounts plus ``--setenv`` (append after ``--clearenv``)."""

        args: list[str] = []
        for host, mount in self.mounts:
            args.extend(("--ro-bind", str(host), mount))
        if self.path_prefix:
            args.extend(("--setenv", "PATH", ":".join((*self.path_prefix, base_path))))
        for key, value in self.env:
            args.extend(("--setenv", key, value))
        return args


def provision_dependencies(
    project: Path,
    *,
    policy: HermeticPolicy,
    cache: DependencyCache,
    sandbox_project: str,
    fetcher: RegistryFetcher | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> ProvisionedDependencies:
    """Provision every lockfile-declared dependency set of *project* hermetically.

    The same entry point serves the author machine and the worker plane.
    Raises :class:`HermeticRefused` for an unsupported or unlocked ecosystem
    and for any policy violation; nothing falls back to the live checkout.
    """

    from lemoncrow.pro.capabilities.review.hermetic_python import (
        PYTHON_ENV_MOUNT,
        PYTHON_VENV,
        provision_python_dependencies,
        python_project_state,
    )

    def require_active() -> None:
        # The worker's authoritative assignment lease is checked in the
        # trusted host only. Never pass a PostgreSQL credential to reviewed
        # source code or to package build subprocesses. These checkpoints do
        # not interrupt an individual blocking registry request/build stage.
        if should_cancel is not None and should_cancel():
            raise HermeticUnavailable("dependency provisioning cancelled by revoked execution authority")

    require_active()
    unsupported = unsupported_lockfiles(project)
    if unsupported:
        raise HermeticRefused(
            f"dependencies locked by {', '.join(unsupported)} cannot be provisioned hermetically; "
            "refusing to run without them"
        )
    npm_state = npm_project_state(project)
    python_state = python_project_state(project)
    if npm_state == "unlocked":
        raise HermeticRefused("package.json declares dependencies but the project has no package-lock.json")
    stages: list[StageOutcome] = []
    mounts: list[tuple[Path, str]] = []
    prefix: list[str] = []
    env: list[tuple[str, str]] = []
    ecosystems: list[str] = []
    base = sandbox_project.rstrip("/") or "/"
    if npm_state == "locked":
        require_active()
        tree = provision_npm_dependencies(
            project, policy=policy, cache=cache, fetcher=fetcher, should_cancel=should_cancel
        )
        stages.extend(tree.stages)
        mounts.append((tree.node_modules, f"{base}/node_modules"))
        prefix.append(f"{base}/node_modules/.bin")
        ecosystems.append("npm")
    if python_state:
        require_active()
        python = provision_python_dependencies(
            project, policy=policy, cache=cache, sandbox_project=base, fetcher=fetcher, should_cancel=should_cancel
        )
        stages.extend(python.stages)
        mounts.append((python.root, PYTHON_ENV_MOUNT))
        prefix.append(f"{PYTHON_VENV}/bin")
        env.extend((("VIRTUAL_ENV", PYTHON_VENV), ("PYTHONDONTWRITEBYTECODE", "1")))
        ecosystems.append("pypi")
    require_active()
    return ProvisionedDependencies(
        stages=tuple(stages),
        mounts=tuple(mounts),
        path_prefix=tuple(prefix),
        env=tuple(env),
        lockfile_digest=lockfile_digest(project),
        ecosystems=tuple(ecosystems),
    )


# --------------------------------------------------------------------------- #
# Run stage: recording sinks
# --------------------------------------------------------------------------- #

SINK_PORTS: Final[Mapping[SinkKind, int]] = {"smtp": 1025, "http": 18025, "object_store": 19000, "queue": 19324}

_SINK_SUPERVISOR: Final[str] = r"""
import base64, http.server, json, os, socketserver, subprocess, sys, threading, urllib.parse
# Keep the supervisor's address-space footprint small: the run's RLIMIT_AS
# budget belongs to the reviewed command (thread stacks and per-thread malloc
# arenas would otherwise reserve tens of MB each).
threading.stack_size(256 * 1024)
try:
    import ctypes
    ctypes.CDLL(None).mallopt(-8, 1)  # M_ARENA_MAX = 1
except Exception:
    pass
OUT = os.path.join(os.environ.get("LEMONCROW_SINK_DIR", "/lemoncrow-sinks"), "captures.jsonl")
LIMIT, PAYLOAD = int(os.environ.get("LEMONCROW_SINK_MAX", "256")), int(os.environ.get("LEMONCROW_SINK_PAYLOAD", "262144"))
lock = threading.Lock()
count = [0]
def record(kind, destination, method, payload, blocked=False, summary=""):
    with lock:
        if count[0] >= LIMIT:
            return
        count[0] += 1
        row = {"kind": kind, "destination": destination[:2048], "method": method[:32], "bytes": len(payload),
               "payload_b64": base64.b64encode(payload[:PAYLOAD]).decode(), "truncated": len(payload) > PAYLOAD,
               "blocked": blocked, "summary": summary[:500]}
        with open(OUT, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(row) + "\n")
class Quiet(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    def log_message(self, *args):
        pass
    def body(self):
        try:
            size = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            size = 0
        return self.rfile.read(max(0, min(size, PAYLOAD * 4)))
    def answer(self, status, payload=b"{}", content_type="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
class Webhook(Quiet):
    def do_CONNECT(self):
        record("http", "https://" + self.path, "CONNECT", b"", True, "TLS egress blocked by the sandbox; payload not observable")
        self.answer(403, b"egress denied by LemonCrow sandbox", "text/plain")
    def any(self):
        if self.path.startswith(("http://", "https://")):
            url = self.path
        else:
            url = "http://" + (self.headers.get("Host") or "webhook-sink") + self.path
        record("http", url, self.command, self.body(), False, "recorded; not delivered")
        self.answer(202)
    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = any
class ObjectStore(Quiet):
    def do_PUT(self):
        record("object_store", "s3://" + urllib.parse.unquote(self.path.split("?", 1)[0]).lstrip("/"), "PUT", self.body(), False, "upload recorded; not stored")
        self.send_response(200)
        self.send_header("ETag", '"lemoncrow-sink"')
        self.send_header("Content-Length", "0")
        self.end_headers()
    def do_POST(self):
        record("object_store", "s3://" + urllib.parse.unquote(self.path.split("?", 1)[0]).lstrip("/"), "POST", self.body(), False, "upload recorded; not stored")
        self.answer(200, b"<ok/>", "application/xml")
    def do_GET(self):
        self.answer(404, b"<Error><Code>NoSuchKey</Code></Error>", "application/xml")
    do_HEAD = do_GET
class Queue(Quiet):
    def do_POST(self):
        raw = self.body()
        target = self.headers.get("X-Amz-Target") or ""
        destination, payload = "queue:" + self.path, raw
        try:
            if target.endswith("SendMessage"):
                doc = json.loads(raw.decode("utf-8"))
                destination, payload = str(doc.get("QueueUrl") or destination), str(doc.get("MessageBody") or "").encode()
            else:
                form = urllib.parse.parse_qs(raw.decode("utf-8"))
                if form.get("Action", [""])[0] == "SendMessage":
                    destination = form.get("QueueUrl", [destination])[0]
                    payload = form.get("MessageBody", [""])[0].encode()
        except (UnicodeDecodeError, ValueError):
            pass
        record("queue", destination, "SendMessage", payload, False, "message recorded; not enqueued")
        self.answer(200, json.dumps({"MessageId": "lemoncrow-sink-%d" % count[0], "MD5OfMessageBody": ""}).encode())
class Smtp(socketserver.StreamRequestHandler):
    def handle(self):
        rcpt, sender = [], ""
        self.wfile.write(b"220 lemoncrow-sink ESMTP\r\n")
        while True:
            line = self.rfile.readline(65536)
            if not line:
                return
            verb = line.decode("utf-8", "replace").strip()
            upper = verb.upper()
            if upper.startswith(("EHLO", "HELO")):
                self.wfile.write(b"250-lemoncrow-sink\r\n250 8BITMIME\r\n")
            elif upper.startswith("MAIL FROM"):
                sender = verb[10:].strip()
                self.wfile.write(b"250 OK\r\n")
            elif upper.startswith("RCPT TO"):
                rcpt.append(verb[8:].strip().strip("<>"))
                self.wfile.write(b"250 OK\r\n")
            elif upper == "DATA":
                self.wfile.write(b"354 End data with <CR><LF>.<CR><LF>\r\n")
                chunks, size = [], 0
                while True:
                    data = self.rfile.readline(65536)
                    if not data or data in (b".\r\n", b".\n"):
                        break
                    size += len(data)
                    if size <= PAYLOAD * 4:
                        chunks.append(data[1:] if data.startswith(b"..") else data)
                record("smtp", "smtp:" + ",".join(rcpt), "DATA", b"".join(chunks), False, "email recorded; not delivered (from " + sender[:200] + ")")
                rcpt = []
                self.wfile.write(b"250 OK queued by lemoncrow-sink\r\n")
            elif upper.startswith("AUTH"):
                self.wfile.write(b"235 Authentication accepted by sink\r\n")
            elif upper.startswith("STARTTLS"):
                self.wfile.write(b"454 TLS not available in the sandbox\r\n")
            elif upper.startswith("QUIT"):
                self.wfile.write(b"221 Bye\r\n")
                return
            else:
                self.wfile.write(b"250 OK\r\n")
class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True
ports = json.loads(os.environ["LEMONCROW_SINK_PORTS"])
for kind, handler in (("http", Webhook), ("object_store", ObjectStore), ("queue", Queue), ("smtp", Smtp)):
    server = Server(("127.0.0.1", int(ports[kind])), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
for sidecar in json.loads(os.environ.get("LEMONCROW_SIDECARS", "[]")):
    import socket, time
    subprocess.Popen(sidecar["command"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + float(sidecar.get("ready_timeout", 10))
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", int(sidecar["port"])), 0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        sys.stderr.write("sidecar %s did not become ready\n" % sidecar["id"])
        sys.exit(125)
argv = sys.argv[sys.argv.index("--") + 1:]
for name in ("LEMONCROW_SINK_PORTS", "LEMONCROW_SIDECARS", "LEMONCROW_SINK_DIR", "LEMONCROW_SINK_MAX", "LEMONCROW_SINK_PAYLOAD"):
    os.environ.pop(name, None)
try:
    code = subprocess.call(argv)
except OSError as exc:
    sys.stderr.write("command could not start: %s\n" % exc)
    code = 127
sys.exit(code)
"""


def sink_environment() -> dict[str, str]:
    """Variables that point common clients at the recording sinks."""

    smtp, web, objects, queue = (
        SINK_PORTS["smtp"],
        SINK_PORTS["http"],
        SINK_PORTS["object_store"],
        SINK_PORTS["queue"],
    )
    proxy = f"http://127.0.0.1:{web}"
    return {
        "SMTP_HOST": "127.0.0.1",
        "SMTP_PORT": str(smtp),
        "MAIL_HOST": "127.0.0.1",
        "MAIL_PORT": str(smtp),
        "EMAIL_HOST": "127.0.0.1",
        "EMAIL_PORT": str(smtp),
        "HTTP_PROXY": proxy,
        "http_proxy": proxy,
        "HTTPS_PROXY": proxy,
        "https_proxy": proxy,
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
        "LEMONCROW_WEBHOOK_SINK_URL": f"{proxy}/",
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{objects}",
        "AWS_ENDPOINT_URL_S3": f"http://127.0.0.1:{objects}",
        "AWS_ENDPOINT_URL_SQS": f"http://127.0.0.1:{queue}",
        "AWS_ACCESS_KEY_ID": "lemoncrow-sink",
        "AWS_SECRET_ACCESS_KEY": "lemoncrow-sink",
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "LEMONCROW_OBJECT_STORE_SINK_URL": f"http://127.0.0.1:{objects}",
        "LEMONCROW_QUEUE_SINK_URL": f"http://127.0.0.1:{queue}",
        "LEMONCROW_SMTP_SINK": f"127.0.0.1:{smtp}",
    }


def sinks_available() -> bool:
    """The supervisor needs a Python inside the sandbox's read-only runtime mounts."""

    return any(Path(root, "python3").is_file() for root in ("/usr/bin", "/bin", "/usr/local/bin"))


@dataclass
class RecordingSinks:
    """Per-run recording sinks: mount, environment and capture collection.

    The sinks listen on loopback inside the run's private network namespace,
    so nothing they accept can leave the sandbox and real egress stays denied.
    """

    directory: Path
    sidecars: tuple[SidecarSpec, ...] = ()

    @classmethod
    def create(cls, parent: Path, *, sidecars: Sequence[SidecarSpec] = ()) -> RecordingSinks:
        parent.mkdir(parents=True, exist_ok=True)
        directory = Path(tempfile.mkdtemp(prefix="sinks-", dir=parent))
        directory.chmod(0o700)
        return cls(directory, tuple(sidecars))

    def bwrap_args(self) -> list[str]:
        args = ["--bind", str(self.directory), SINK_MOUNT]
        environment = {
            **sink_environment(),
            "LEMONCROW_SINK_DIR": SINK_MOUNT,
            "LEMONCROW_SINK_PORTS": json.dumps(dict(SINK_PORTS), sort_keys=True),
            "LEMONCROW_SINK_MAX": str(SINK_MAX_CAPTURES),
            "LEMONCROW_SINK_PAYLOAD": str(SINK_MAX_PAYLOAD_BYTES),
            "LEMONCROW_SIDECARS": json.dumps([item.to_wire() for item in self.sidecars]),
        }
        for key, value in sorted(environment.items()):
            args.extend(("--setenv", key, value))
        return args

    @staticmethod
    def wrap(command: Sequence[str]) -> list[str]:
        return ["python3", "-I", "-S", "-c", _SINK_SUPERVISOR, "--", *command]

    def collect(
        self,
        *,
        review_id: str,
        policy: RedactionPolicy | None,
        content_access: ContentAccess,
        artifacts: RunArtifactStore | None,
    ) -> tuple[tuple[SinkCapture, ...], tuple[ArtifactRef, ...]]:
        """Read captures, redact payloads before storage, and store them with retention."""

        return captures_from_rows(
            read_sink_rows(self.directory),
            review_id=review_id,
            policy=policy,
            content_access=content_access,
            artifacts=artifacts,
        )

    def cleanup(self) -> None:
        shutil.rmtree(self.directory, ignore_errors=True)


def read_sink_rows(
    directory: Path,
    *,
    max_rows: int = SINK_MAX_CAPTURES,
    max_payload_bytes: int = SINK_MAX_PAYLOAD_BYTES,
) -> list[dict[str, Any]]:
    """Bounded, schema-checked raw capture rows (still unredacted; never store these)."""

    path = directory / "captures.jsonl"
    try:
        with path.open("rb") as handle:
            raw_lines = handle.read(SINK_MAX_CAPTURES * SINK_MAX_PAYLOAD_BYTES * 2).splitlines()
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for raw_line in raw_lines:
        if len(rows) >= max_rows:
            break
        try:
            row = json.loads(raw_line)
            kind = str(row.get("kind") or "")
            payload = base64.b64decode(str(row.get("payload_b64") or ""), validate=True)
        except (ValueError, binascii.Error, AttributeError):
            continue
        if kind not in SINK_PORTS:
            continue
        rows.append(
            {
                "kind": kind,
                "destination": str(row.get("destination") or "")[:2048],
                "method": str(row.get("method") or "")[:32],
                "summary": str(row.get("summary") or "")[:500],
                "bytes": int(row.get("bytes") or len(payload)),
                "payload_b64": base64.b64encode(payload[:max_payload_bytes]).decode(),
                "blocked": bool(row.get("blocked")),
            }
        )
    return rows


def captures_from_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    review_id: str,
    policy: RedactionPolicy | None,
    content_access: ContentAccess,
    artifacts: RunArtifactStore | None,
    max_rows: int = SINK_MAX_CAPTURES,
) -> tuple[tuple[SinkCapture, ...], tuple[ArtifactRef, ...]]:
    """Redact raw capture rows *before* anything is stored; payload bytes go to retention-bound storage."""

    captures: list[SinkCapture] = []
    refs: list[ArtifactRef] = []
    for row in rows:
        if len(captures) >= max_rows:
            break
        kind = str(row.get("kind") or "")
        if kind not in SINK_PORTS:
            continue
        try:
            payload = base64.b64decode(str(row.get("payload_b64") or ""), validate=True)[:SINK_MAX_PAYLOAD_BYTES]
        except (ValueError, binascii.Error):
            continue
        protected, redactions = protect_bytes(payload, policy)
        destination, dest_redactions = protect_text(str(row.get("destination") or "")[:2048], policy)
        payload_ref = ""
        if artifacts is not None and protected:
            ref = artifacts.put(
                review_id,
                protected,
                role=f"sink:{kind}",
                media_type="text/plain" if not protected.startswith(b"<binary") else "application/octet-stream",
                retention="raw",
            )
            refs.append(ref)
            payload_ref = ref.ref
        preview = protected[:_PREVIEW_CHARS].decode("utf-8", errors="replace")
        try:
            declared_bytes = max(0, int(row.get("bytes") or len(payload)))
        except (TypeError, ValueError):
            declared_bytes = len(payload)
        captures.append(
            SinkCapture(
                kind=cast(SinkKind, kind),
                destination=destination,
                method=str(row.get("method") or "")[:32],
                summary=protect_text(str(row.get("summary") or "")[:500], policy)[0],
                payload_preview=preview,
                payload_digest=hashlib.sha256(protected).hexdigest(),
                payload_bytes=declared_bytes,
                payload_ref=payload_ref,
                content_access=content_access,
                blocked=bool(row.get("blocked")),
                redactions=redactions + dest_redactions,
            )
        )
    return tuple(captures), tuple(refs)


# --------------------------------------------------------------------------- #
# Environment fingerprint
# --------------------------------------------------------------------------- #
#
# Every probe result is cached by the probed file's identity on disk (resolved
# path, device, inode, size, mtime_ns, ctime_ns), never per process: replacing
# or upgrading a binary under a running server changes its key, so the next
# probe runs again and the record states the environment the run actually used.

_PROBE_CACHE_LIMIT: Final[int] = 512
_PROBE_CACHE: OrderedDict[tuple[object, ...], str] = OrderedDict()
_PROBE_LOCK = threading.Lock()
#: An executable larger than this is not digested; its identity is then unknown.
_DIGEST_LIMIT: Final[int] = 512 * 1024 * 1024
_VERSION_OUTPUT_LIMIT: Final[int] = 8192
_SHEBANG_LIMIT: Final[int] = 256
_CHAIN_LIMIT: Final[int] = 6
_LINK_LIMIT: Final[int] = 40


def _file_key(path: str) -> tuple[object, ...] | None:
    """The on-disk identity of a regular file, or ``None`` when it cannot be read."""

    try:
        real = os.path.realpath(path)
        info = os.stat(real)
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return (real, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _probe_cached(kind: str, path: str, extra: tuple[str, ...], compute: Callable[[], str]) -> str:
    """``compute()``, cached under the file's on-disk identity; unknown results are not cached."""

    before = _file_key(path)
    if before is None:
        return compute()
    key = (kind, *before, *extra)
    with _PROBE_LOCK:
        hit = _PROBE_CACHE.get(key)
        if hit is not None:
            _PROBE_CACHE.move_to_end(key)
            return hit
    value = compute()
    # A file replaced while it was probed keeps no entry: the next probe reads it again.
    if value not in UNKNOWN_ENVIRONMENT_VALUES and _file_key(path) == before:
        with _PROBE_LOCK:
            _PROBE_CACHE[key] = value
            _PROBE_CACHE.move_to_end(key)
            while len(_PROBE_CACHE) > _PROBE_CACHE_LIMIT:
                _PROBE_CACHE.popitem(last=False)
    return value


def _runtime_image() -> tuple[str, bool]:
    """The host runtime image digest and whether every input to it was read."""

    declared = os.environ.get("LEMONCROW_SANDBOX_IMAGE_DIGEST", "").strip()
    if declared:
        return declared[:200], True
    complete = True
    parts: list[str] = [platform.machine(), platform.system()]
    try:
        parts.append(Path("/etc/os-release").read_text(encoding="utf-8"))
    except OSError:
        parts.append(platform.platform())
        complete = sys.platform == "darwin" and bool(platform.mac_ver()[0])
    bwrap = shutil.which("bwrap") or shutil.which("bubblewrap")
    if bwrap:
        version = _tool_version((bwrap, "--version"))
        parts.append(version)
        complete = complete and version not in UNKNOWN_ENVIRONMENT_VALUES
    elif sys.platform != "darwin":
        complete = False
    return "host:sha256:" + hashlib.sha256("\n".join(parts).encode()).hexdigest(), complete


def runtime_image_digest() -> str:
    """Image digest of this sandbox runtime, read again on every call.

    Worker images export ``LEMONCROW_SANDBOX_IMAGE_DIGEST`` (the container
    image digest). A host bubblewrap sandbox mounts the host runtime
    read-only, so its "image" is the host OS release plus bubblewrap version.
    """

    return _runtime_image()[0]


def executor_image_digest() -> str:
    """The image digest an executor fingerprint records: partial inputs are marked as such.

    A digest whose inputs were not all read (no os-release, no bubblewrap
    version) carries :data:`PARTIAL_IMAGE_PREFIX`, which makes the executor
    environment unknown, so it never carries.
    """

    digest, complete = _runtime_image()
    return digest if complete else PARTIAL_IMAGE_PREFIX + digest.removeprefix("host:")


def kernel_release() -> str:
    """The running kernel's release; ``""`` (unknown) when it cannot be read."""

    try:
        return os.uname().release[:256]
    except (AttributeError, OSError):
        return ""


# Bound at import: fingerprint probes are host facts, independent of whatever
# executes reviewed code (and of test doubles that replace it).
_PROBE_RUN = subprocess.run
_PROBE_POPEN = subprocess.Popen


def _bounded_output(argv: Sequence[str], *, timeout: float = 10.0) -> tuple[int, str] | None:
    """Run a probe with no stdin; ``None`` on failure, timeout or runaway output."""

    try:
        proc = _PROBE_POPEN(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env={"PATH": _SANDBOX_PATH, "HOME": "/nonexistent", "LANG": "C", "LC_ALL": "C"},
            close_fds=True,
        )
    except (OSError, ValueError):
        return None
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    output = bytearray()
    finished = False
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while len(output) <= _VERSION_OUTPUT_LIMIT:
                left = deadline - time.monotonic()
                if left <= 0 or not selector.select(left):
                    break
                chunk = os.read(proc.stdout.fileno(), _VERSION_OUTPUT_LIMIT + 1 - len(output))
                if not chunk:
                    finished = True
                    break
                output += chunk
        code = proc.wait(timeout=max(0.0, deadline - time.monotonic())) if finished else None
    except (OSError, subprocess.TimeoutExpired):
        code = None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        proc.stdout.close()
    if code is None:
        return None
    return code, output.decode("utf-8", errors="replace")


def _tool_version(argv: tuple[str, ...]) -> str:
    """A tool's version output, cached by the binary's on-disk identity.

    ``"absent"`` when the binary is missing, ``"unavailable"`` when it could not
    run, ``"unknown"`` when it printed nothing. A tool that rejects the flag still
    has a deterministic answer (its exit status and message), recorded as such.
    """

    if not argv or not os.path.exists(argv[0]):
        return "absent"

    def probe() -> str:
        result = _bounded_output(argv)
        if result is None:
            return "unavailable"
        code, text = result
        lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
        first = lines[0][:128] if lines else ""
        if code == 0:
            return first or "unknown"
        return f"exit {code}" + (f": {first}" if first else "")

    return _probe_cached("version", argv[0], argv, probe)


def _content_digest(path: str) -> str:
    """``sha256:`` of a file's bytes, cached by its on-disk identity; ``""`` when unreadable."""

    def probe() -> str:
        digest = hashlib.sha256()
        try:
            with open(path, "rb") as handle:
                if os.fstat(handle.fileno()).st_size > _DIGEST_LIMIT:
                    return ""
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
        except OSError:
            return ""
        return "sha256:" + digest.hexdigest()

    return _probe_cached("content", path, (), probe)


_TOOL_PROBES: Final[Mapping[str, tuple[str, ...]]] = {
    "python3": ("python3", "--version"),
    "node": ("node", "--version"),
    "npm": ("npm", "--version"),
    "strace": ("strace", "-V"),
}


def toolchain_versions(tools: Iterable[str]) -> tuple[tuple[str, str], ...]:
    """Versions of the sandbox-visible toolchain (the same read-only binaries)."""

    out: dict[str, str] = {}
    for tool in sorted(set(tools)):
        probe = _TOOL_PROBES.get(tool)
        if probe is None:
            continue
        executable = None
        for root in ("/usr/local/bin", "/usr/bin", "/bin"):
            if Path(root, probe[0]).is_file():
                executable = str(Path(root, probe[0]))
                break
        out[tool] = _tool_version((executable, *probe[1:])) if executable else "absent"
    return tuple(sorted(out.items()))


@dataclass(frozen=True, slots=True)
class SandboxView:
    """How a sandboxed command sees the filesystem: its PATH, working directory and mounts.

    ``mounts`` maps sandbox paths to host paths (bind order; the longest prefix
    wins). ``probed`` names the mount points that hold the host runtime: only an
    executable there is run for its version and digested. Anything else (the
    reviewed workspace, provisioned dependencies) is never run or read by the
    probe, so its identity is its resolved path alone, which is unknown.
    """

    path: str
    cwd: str
    mounts: tuple[tuple[str, str], ...]
    probed: frozenset[str] = frozenset()

    def mapped(self, sandbox_path: str) -> tuple[str, str] | None:
        """``(host path, mount point)`` for a sandbox path, or ``None`` when nothing mounts it."""

        best: tuple[str, str] | None = None
        for mount, host in self.mounts:
            if sandbox_path == mount or sandbox_path.startswith(mount.rstrip("/") + "/"):
                if best is None or len(mount) >= len(best[1]):
                    rest = sandbox_path[len(mount) :].lstrip("/")
                    best = (os.path.join(host, rest) if rest else host, mount)
        return best

    def resolve(self, sandbox_path: str) -> tuple[str, str, str] | None:
        """Follow symlinks inside the sandbox view: ``(sandbox path, host path, mount)``."""

        resolved = PurePosixPath("/")
        pending = list(PurePosixPath(sandbox_path).parts[1:])
        hops = 0
        while pending:
            part = pending.pop(0)
            if part in {"", "."}:
                continue
            if part == "..":
                resolved = resolved.parent
                continue
            candidate = resolved / part
            mapped = self.mapped(str(candidate))
            if mapped is None:
                # The sandbox creates the parents of its mount points as plain directories.
                prefix = str(candidate).rstrip("/") + "/"
                if any(mount.startswith(prefix) for mount, _host in self.mounts):
                    resolved = candidate
                    continue
                return None
            host, mount = mapped
            if str(candidate) == mount:
                # A bind mount point is a directory inside the sandbox, whatever the host link.
                resolved = candidate
                continue
            try:
                target = os.readlink(host)
            except OSError:
                resolved = candidate
                continue
            hops += 1
            if hops > _LINK_LIMIT:
                return None
            if target.startswith("/"):
                resolved = PurePosixPath("/")
            pending = [*PurePosixPath(target).parts[1 if target.startswith("/") else 0 :], *pending]
        mapped = self.mapped(str(resolved))
        if mapped is None:
            return None
        return str(resolved), mapped[0], mapped[1]

    def lookup(self, name: str) -> tuple[str, str, str] | None:
        """Resolve a command name the way ``execvp`` inside the sandbox would."""

        if not name:
            return None
        if "/" in name:
            candidates = [name if name.startswith("/") else posixpath.join(self.cwd, name)]
        else:
            candidates = [posixpath.join(entry or self.cwd, name) for entry in self.path.split(":")]
        for candidate in candidates:
            found = self.resolve(posixpath.normpath(candidate))
            if found is None:
                continue
            try:
                info = os.stat(found[1])
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode) and os.access(found[1], os.X_OK):
                return found
        return None


def _shebang(host_path: str) -> tuple[str, ...]:
    try:
        with open(host_path, "rb") as handle:
            head = handle.read(_SHEBANG_LIMIT)
    except OSError:
        return ()
    if not head.startswith(b"#!"):
        return ()
    line = head[2:].split(b"\n", 1)[0].decode("utf-8", errors="replace")
    return tuple(line.split())


def probe_executables(names: Sequence[str], view: SandboxView) -> tuple[ExecutableIdentity, ...]:
    """The identity of each executable that runs for ``names``, in order, with interpreters.

    Each name is resolved through the sandbox view (its PATH and mounts), then
    followed through ``#!`` interpreters (``env`` and ``env -S`` included). A
    name that does not resolve yields an identity with no path: unknown.
    """

    out: list[ExecutableIdentity] = []
    for first in names:
        name, steps = first, 0
        while name and steps < _CHAIN_LIMIT:
            steps += 1
            found = view.lookup(name)
            if found is None:
                out.append(ExecutableIdentity.from_wire({"name": name}))
                break
            sandbox_path, host_path, mount = found
            if mount not in view.probed:
                # Reviewed content (workspace, provisioned dependencies): never run, read or
                # followed by the probe, so base and head record the same unknown identity.
                out.append(ExecutableIdentity.from_wire({"name": name, "path": sandbox_path}))
                break
            out.append(
                ExecutableIdentity.from_wire(
                    {
                        "name": name,
                        "path": sandbox_path,
                        "version": _tool_version((host_path, "--version")),
                        "sha256": _content_digest(host_path),
                    }
                )
            )
            interpreter = _shebang(host_path)
            if not interpreter:
                break
            name = interpreter[0]
            if posixpath.basename(name) == "env":
                # ``env`` ran too; the program it looks up on PATH runs next.
                found_env = view.lookup(name)
                if found_env is None:
                    out.append(ExecutableIdentity.from_wire({"name": name}))
                    break
                env_version = _tool_version((found_env[1], "--version")) if found_env[2] in view.probed else ""
                out.append(
                    ExecutableIdentity.from_wire(
                        {
                            "name": name,
                            "path": found_env[0],
                            "version": env_version,
                            "sha256": _content_digest(found_env[1]),
                        }
                    )
                )
                rest = [item for item in interpreter[1:] if not item.startswith("-")]
                name = rest[0] if rest else ""
    return tuple(out[:16])


def workspace_digest(root: Path, *, max_files: int = 50_000, max_bytes: int = 1024 * 1024 * 1024) -> str:
    """Digest of a materialized workspace (paths, contents, exec bit).

    Returns ``""`` past the bound: reproduction then reports source equality as
    unverifiable rather than silently comparing a partial tree.
    """

    # lc-debt: full-tree hash per run; reuse the Snapshot manifest digest when runners expose it.
    entries: list[tuple[str, str, bool]] = []
    total = 0
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(item for item in dirs if item not in {".git", "node_modules"})
        for name in sorted(files):
            path = Path(current, name)
            if path.is_symlink() or not path.is_file():
                continue
            try:
                data = path.read_bytes()
            except OSError:
                return ""
            total += len(data)
            if len(entries) >= max_files or total > max_bytes:
                return ""
            entries.append(
                (path.relative_to(root).as_posix(), hashlib.sha256(data).hexdigest(), os.access(path, os.X_OK))
            )
    return canonical_digest(sorted(entries))


@dataclass(frozen=True, slots=True)
class HermeticContext:
    """What a trusted dispatcher hands an executor for one run.

    Assembled from trusted-base/organization policy and the dispatcher's own
    knowledge of the side and source trust; nothing in it comes from the head.
    ``policy_error`` carries an invalid trusted policy so executors refuse with
    the real reason instead of silently falling back to defaults.
    """

    policy: HermeticPolicy
    store_root: Path
    review_id: str
    namespace: CacheNamespace = "head_untrusted"
    executor_class: ExecutorClass = ExecutorClass.AUTHOR_MACHINE
    content_access: ContentAccess = "ai_denied"
    policy_error: str = ""
    source_trust: str = "unknown"

    def for_side(self, side: str) -> HermeticContext:
        """The same context for *side*, with that side's cache namespace."""

        from dataclasses import replace

        from lemoncrow.pro.capabilities.review.execution import cache_namespace

        return replace(self, namespace=cache_namespace(side, self.source_trust))

    @property
    def cache(self) -> DependencyCache:
        return DependencyCache.for_store(self.store_root, self.namespace)

    @property
    def artifacts(self) -> RunArtifactStore:
        return RunArtifactStore.for_store(self.store_root, retention_seconds=self.policy.retention_seconds)


def hermetic_context(
    *,
    trusted_review_config: str | None,
    store_root: Path,
    review_id: str,
    side: str,
    source_trust: str = "unknown",
    organization: Mapping[str, Any] | None = None,
    executor_class: ExecutorClass = ExecutorClass.AUTHOR_MACHINE,
    content_access: ContentAccess = "ai_denied",
) -> HermeticContext:
    """Build a :class:`HermeticContext`; an invalid trusted policy yields a refusing context."""

    from lemoncrow.pro.capabilities.review.execution import cache_namespace

    namespace = cache_namespace(side, source_trust)
    try:
        policy = load_hermetic_policy(trusted_review_config, organization=organization)
        error = ""
    except ValueError as exc:
        policy, error = HermeticPolicy(), str(exc)[:500]
    return HermeticContext(
        policy=policy,
        store_root=store_root,
        review_id=review_id,
        namespace=namespace,
        executor_class=executor_class,
        content_access=content_access,
        policy_error=error,
        source_trust=source_trust,
    )


def build_execution_result(
    *,
    subject: ExecutionSubject,
    executor_class: ExecutorClass,
    policy: HermeticPolicy,
    recipe: ExecutionRecipe,
    stages: Sequence[StageOutcome],
    namespace: CacheNamespace,
    content_access: ContentAccess,
    lockfile_digest: str = "",
    tools: Iterable[str] = ("python3",),
    captures: Sequence[SinkCapture] = (),
    artifacts: Sequence[ArtifactRef] = (),
    image_digest: str = "",
    sinks_active: bool = False,
    reproduce: str = "",
    sandbox: str = "bubblewrap",
    toolchain: Sequence[tuple[str, str]] | None = None,
    environment: EnvironmentFingerprint | None = None,
) -> ExecutionResult:
    if environment is not None:
        # A probed executor environment (what the recipe actually ran); the recipe
        # and lockfile digests come from this run.
        image = environment.image_digest
        fingerprint = replace(environment, lockfile_digest=lockfile_digest, recipe_digest=recipe.digest)
    else:
        image = image_digest or runtime_image_digest()
        fingerprint = EnvironmentFingerprint(
            image_digest=image,
            # A remote executor reports its own toolchain; local runs probe it here.
            toolchain=tuple(sorted(toolchain)) if toolchain is not None else toolchain_versions(tools),
            lockfile_digest=lockfile_digest,
            recipe_digest=recipe.digest,
            sandbox=sandbox,
        )
    verification = VerificationEnvironment(
        executor_class=executor_class,
        image_digest=image,
        network="denied",
        sinks=tuple(SINK_PORTS) if sinks_active else (),
        not_verified=policy.unverified,
    )
    return ExecutionResult(
        subject=subject,
        executor_class=executor_class,
        fingerprint=fingerprint,
        verification=verification,
        recipe=recipe,
        stages=tuple(stages),
        sink_captures=tuple(captures),
        artifacts=tuple(artifacts),
        cache_namespace=namespace,
        content_access=content_access,
        redaction_policy_digest=policy.redaction.digest,
        reproduce=reproduce,
    )


__all__ = [
    "DEFAULT_NPM_REGISTRY",
    "DEFAULT_PYPI_FILES",
    "DEFAULT_PYPI_INDEX",
    "DEFAULT_REGISTRIES",
    "ECOSYSTEMS",
    "SINK_MOUNT",
    "SINK_PORTS",
    "DependencyCache",
    "FixtureSpec",
    "HermeticContext",
    "HermeticPolicy",
    "HermeticRefused",
    "HermeticUnavailable",
    "InstallPlan",
    "PlannedPackage",
    "ProvisionedDependencies",
    "ProvisionedTree",
    "RecordingSinks",
    "RegistryFetcher",
    "RegistryRule",
    "SandboxView",
    "SidecarSpec",
    "build_execution_result",
    "executor_image_digest",
    "hermetic_context",
    "hermetic_policy_from_wire",
    "kernel_release",
    "load_hermetic_policy",
    "npm_dependency_digest",
    "npm_project_state",
    "offline_bwrap_prefix",
    "plan_npm_install",
    "probe_executables",
    "provision_dependencies",
    "provision_npm_dependencies",
    "pypi_normalize",
    "runtime_image_digest",
    "sink_environment",
    "sinks_available",
    "toolchain_versions",
    "unsupported_lockfiles",
    "workspace_digest",
]
