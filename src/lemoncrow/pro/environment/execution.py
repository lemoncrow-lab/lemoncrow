"""Execution-result identity contract for Review sandbox runs.

Every execution LemonCrow performs or accepts for a Review (a preview build, a
recipe/check run, a runtime surface run) is described by one versioned
:class:`ExecutionResult`. Downstream packages consume only this contract:

* ``executor_class`` -- *who* executed: ``author_machine`` (self-attested),
  ``customer_worker`` or ``managed_sandbox``. The class is stamped by the
  authority that dispatched the work (local host, control plane), never taken
  from the executor's own claim (:meth:`ExecutionResult.with_executor`).
* ``fingerprint`` -- *in which environment*: image digest, toolchain versions,
  lockfile digest and recipe digest. ``fingerprint.digest`` is the opaque
  ``environment_fingerprint`` string shared with the behavior-target contract.
* ``verification`` -- an honest statement of where the result was verified and
  which integrations the sandbox cannot exercise (always ``not_verified``).
* ``sink_captures`` -- outbound email/webhook/queue/object-store attempts that
  the run stage recorded instead of delivering. ``delivered`` is always false.
* ``artifacts`` -- references to raw heavy bytes with a retention class; raw
  bytes expire, summaries (this record) persist.

``identity`` binds the subject (exact Snapshot side), the executor class and
the environment fingerprint, so two results are the same execution only when
all three agree. Outcome fields (stages, captures, durations) are deliberately
outside the identity and inside ``result_digest``.

Schema changes bump :data:`EXECUTION_RESULT_SCHEMA_VERSION`; readers refuse
versions they do not know instead of guessing.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Final, Literal, cast

EXECUTION_RESULT_SCHEMA_VERSION = 1
SUPPORTED_EXECUTION_RESULT_SCHEMA_VERSIONS = frozenset({EXECUTION_RESULT_SCHEMA_VERSION})
EXECUTION_RESULT_MEDIA_TYPE = "application/vnd.lemoncrow.execution-result+json;version=1"
ENVIRONMENT_FINGERPRINT_VERSION = 1
#: Version 2 adds the kernel release and the identity of each executable a
#: recipe actually ran (resolved path, version output, content digest). Only a
#: version-2 fingerprint has an executor digest, so a version-1 record never
#: matches a version-2 environment and never carries.
EXECUTOR_FINGERPRINT_VERSION = 2
SUPPORTED_ENVIRONMENT_FINGERPRINT_VERSIONS = frozenset({ENVIRONMENT_FINGERPRINT_VERSION, EXECUTOR_FINGERPRINT_VERSION})
#: Values a probe reports when it could not establish a component. Any of them
#: makes the whole executor environment unknown, so two unknowns never compare equal.
UNKNOWN_ENVIRONMENT_VALUES: Final[frozenset[str]] = frozenset({"", "unknown", "unavailable", "absent"})
#: Prefix of a host image digest built from incomplete inputs (unreadable
#: os-release, unknown bubblewrap version): usable as a label, never for carry.
PARTIAL_IMAGE_PREFIX: Final[str] = "host-partial:"


class ExecutorClass(StrEnum):
    """Who executed reviewed code. Values are shared with the behavior-target contract."""

    AUTHOR_MACHINE = "author_machine"
    CUSTOMER_WORKER = "customer_worker"
    MANAGED_SANDBOX = "managed_sandbox"
    #: A Workspaces task sandbox ran it for the task's own agent (``lc verify`` on the server). Self-reported: the
    #: author's own sandbox never counts as Review evidence, and Review never plans a run as one (it has no
    #: ``run_on``).
    WORKSPACE_TASK = "workspace_task"

    @property
    def self_attested(self) -> bool:
        """An author-machine or task-sandbox result is attested only by the author's own environment."""

        return self in (ExecutorClass.AUTHOR_MACHINE, ExecutorClass.WORKSPACE_TASK)


EXECUTOR_CLASSES: tuple[str, ...] = tuple(item.value for item in ExecutorClass)
TRUSTED_EXECUTOR_CLASSES: frozenset[str] = frozenset(
    {ExecutorClass.CUSTOMER_WORKER.value, ExecutorClass.MANAGED_SANDBOX.value}
)


def parse_executor_class(value: object) -> ExecutorClass:
    """Parse a stored/wire executor class; anything unknown is the least trusted class.

    Fail closed: a missing or malformed class can never be read as a trusted
    executor, so it degrades to ``author_machine`` (self-attested).
    """

    try:
        return ExecutorClass(str(value or "").strip())
    except ValueError:
        return ExecutorClass.AUTHOR_MACHINE


def executor_class_for_locus(locus: str, *, operator: str = "customer") -> ExecutorClass:
    """Map a durable job's execution locus to the executor class its results carry.

    ``operator`` names who runs the hosted worker plane: ``customer`` for a
    self-hosted deployment (its internal worker is customer compute) and
    ``lemoncrow`` for the LemonCrow-managed metered sandbox.
    """

    normalized = str(locus or "").strip().lower()
    if normalized == "customer":
        return ExecutorClass.CUSTOMER_WORKER
    if normalized == "managed":
        return ExecutorClass.MANAGED_SANDBOX if operator == "lemoncrow" else ExecutorClass.CUSTOMER_WORKER
    return ExecutorClass.AUTHOR_MACHINE


# The run planner's executor locus (``run_on``) for each executor class.
RUN_ON_BY_EXECUTOR_CLASS: Mapping[ExecutorClass, str] = {
    ExecutorClass.AUTHOR_MACHINE: "local",
    ExecutorClass.CUSTOMER_WORKER: "worker",
    ExecutorClass.MANAGED_SANDBOX: "managed",
}
_RUN_ON_BY_LOCUS: Mapping[str, str] = {"local": "local", "customer": "worker", "managed": "managed"}


def run_on_for_locus(locus: str) -> str:
    """The run planner's ``run_on`` for a durable job's execution locus.

    A hosted job's locus is the plane the plan names, whoever operates it: a
    customer-operated deployment's ``managed`` plane is still the plan's
    ``managed`` executor (its results carry ``customer_worker``).
    """

    normalized = str(locus or "").strip().lower()
    if normalized not in _RUN_ON_BY_LOCUS:
        raise ValueError(f"unknown execution locus: {locus!r}")
    return _RUN_ON_BY_LOCUS[normalized]


def normalize_executor_requirement(raw: object) -> tuple[str, ...]:
    """Parse a policy executor requirement into an explicit allowlist.

    ``None``/``"any"``/empty means any executor; ``"trusted"`` means every
    trusted (non-self-attested) executor class; a list names classes exactly.
    Unknown class names are rejected, never ignored.
    """

    if raw is None:
        return ()
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in {"", "any"}:
            return ()
        if value == "trusted":
            return tuple(sorted(TRUSTED_EXECUTOR_CLASSES))
        rows: Sequence[object] = (value,)
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray)):
        rows = raw
    else:
        raise ValueError("executor requirement must be 'any', 'trusted' or a list of executor classes")
    out: list[str] = []
    for item in rows:
        name = str(item).strip().lower()
        if name not in EXECUTOR_CLASSES:
            raise ValueError(f"unknown executor class {name!r}")
        if name not in out:
            out.append(name)
    return tuple(sorted(out))


def executor_satisfies(executor_class: object, allowed: Collection[str]) -> bool:
    """Policy hook: may a result from *executor_class* satisfy a requirement?

    An empty *allowed* set accepts every executor. Otherwise the result's class
    must be listed. Unknown classes parse as ``author_machine``, so a forged or
    missing class can never satisfy a trusted-executor reservation.
    """

    if not allowed:
        return True
    return parse_executor_class(executor_class).value in set(allowed)


CacheNamespace = Literal["base", "head_trusted", "head_untrusted"]
CACHE_NAMESPACES: tuple[CacheNamespace, ...] = ("base", "head_trusted", "head_untrusted")
_CACHE_READS: Mapping[CacheNamespace, tuple[CacheNamespace, ...]] = {
    # A base run only ever consumes entries written by base runs.
    "base": ("base",),
    # Trusted heads may reuse base entries read-only and write their own.
    "head_trusted": ("head_trusted", "base"),
    # Untrusted heads share nothing: no read of trusted/private entries, and
    # nothing they write is ever visible to another namespace.
    "head_untrusted": ("head_untrusted",),
}


def cache_namespace(side: str, source_trust: str) -> CacheNamespace:
    """Write namespace for dependency/build caches of one run.

    The base (old) side is the trusted base. A head (new) side is trusted only
    when its source trust is explicitly ``trusted_internal``; unknown and
    external sources are untrusted.
    """

    if side == "old":
        return "base"
    return "head_trusted" if str(source_trust) == "trusted_internal" else "head_untrusted"


def readable_cache_namespaces(namespace: CacheNamespace) -> tuple[CacheNamespace, ...]:
    """Namespaces a run may read; the first entry is the only writable one."""

    return _CACHE_READS[namespace]


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def canonical_digest(value: object) -> str:
    """Lowercase sha256 hex of the canonical JSON encoding."""

    return hashlib.sha256(_canonical(value)).hexdigest()


def _str_pairs(value: object) -> tuple[tuple[str, str], ...]:
    if isinstance(value, Mapping):
        items: Iterable[Any] = value.items()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        items = value
    else:
        return ()
    pairs: dict[str, str] = {}
    for item in items:
        if isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)) and len(item) == 2:
            pairs[str(item[0])[:64]] = str(item[1])[:128]
    return tuple(sorted(pairs.items()))


@dataclass(frozen=True, slots=True)
class ExecutableIdentity:
    """One executable a recipe ran: its name, where it resolved, its version and bytes.

    ``path`` is empty when the name did not resolve; ``version`` is empty when
    the executable is reviewed content (workspace or provisioned dependencies)
    that the probe digests but never runs. Either makes the identity unknown.
    """

    name: str
    path: str = ""
    version: str = ""
    sha256: str = ""

    @property
    def known(self) -> bool:
        return all(value not in UNKNOWN_ENVIRONMENT_VALUES for value in (self.path, self.version, self.sha256))

    def to_wire(self) -> dict[str, str]:
        return {"name": self.name, "path": self.path, "version": self.version, "sha256": self.sha256}

    @classmethod
    def from_wire(cls, raw: object) -> ExecutableIdentity:
        item = raw if isinstance(raw, Mapping) else {}
        return cls(
            name=str(item.get("name") or "")[:256],
            path=str(item.get("path") or "")[:1024],
            version=str(item.get("version") or "")[:256],
            sha256=str(item.get("sha256") or "")[:80],
        )


@dataclass(frozen=True, slots=True)
class EnvironmentFingerprint:
    """The environment one execution ran in; equal fingerprints mean comparable results.

    ``image_digest`` is the container image digest for worker sandboxes, or a
    ``host:sha256:`` digest of the host runtime identity for local bubblewrap.
    Version 2 adds ``kernel`` (the kernel release) and ``executables`` (what the
    recipe actually ran, in resolution order).
    """

    image_digest: str
    toolchain: tuple[tuple[str, str], ...] = ()
    lockfile_digest: str = ""
    recipe_digest: str = ""
    sandbox: str = "bubblewrap"
    version: int = ENVIRONMENT_FINGERPRINT_VERSION
    kernel: str = ""
    executables: tuple[ExecutableIdentity, ...] = ()

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "version": self.version,
            "image_digest": self.image_digest,
            "toolchain": dict(self.toolchain),
            "lockfile_digest": self.lockfile_digest,
            "recipe_digest": self.recipe_digest,
            "sandbox": self.sandbox,
        }
        if self.version >= EXECUTOR_FINGERPRINT_VERSION:
            wire["kernel"] = self.kernel
            wire["executables"] = [item.to_wire() for item in self.executables]
        return wire

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> EnvironmentFingerprint:
        version = int(raw.get("version") or 0)
        if version not in SUPPORTED_ENVIRONMENT_FINGERPRINT_VERSIONS:
            raise ValueError(f"environment fingerprint version {version} is not supported")
        executables = raw.get("executables") if version >= EXECUTOR_FINGERPRINT_VERSION else None
        return cls(
            image_digest=str(raw.get("image_digest") or ""),
            toolchain=_str_pairs(raw.get("toolchain")),
            lockfile_digest=str(raw.get("lockfile_digest") or ""),
            recipe_digest=str(raw.get("recipe_digest") or ""),
            sandbox=str(raw.get("sandbox") or ""),
            version=version,
            kernel=str(raw.get("kernel") or "")[:256] if version >= EXECUTOR_FINGERPRINT_VERSION else "",
            executables=(
                tuple(ExecutableIdentity.from_wire(item) for item in executables[:16])
                if isinstance(executables, list)
                else ()
            ),
        )

    @property
    def digest(self) -> str:
        """Opaque ``environment_fingerprint`` string used by downstream contracts."""

        return "envfp:sha256:" + canonical_digest(self.to_wire())

    @property
    def executor_known(self) -> bool:
        """Every executor component was established; one unknown makes the whole unknown."""

        return (
            self.version == EXECUTOR_FINGERPRINT_VERSION
            and self.image_digest not in UNKNOWN_ENVIRONMENT_VALUES
            and not self.image_digest.startswith(PARTIAL_IMAGE_PREFIX)
            and self.kernel not in UNKNOWN_ENVIRONMENT_VALUES
            and self.sandbox not in UNKNOWN_ENVIRONMENT_VALUES
            and bool(self.executables)
            and all(item.known for item in self.executables)
            and all(value not in UNKNOWN_ENVIRONMENT_VALUES for _name, value in self.toolchain)
        )

    @property
    def executor_digest(self) -> str:
        """The executor environment alone: image, kernel, what ran, toolchain and sandbox.

        ``recipe_digest`` and ``lockfile_digest`` follow from the recipe and the
        Snapshot, which a carry key already covers. Equal executor digests mean
        the same image, kernel, executables (path, version and bytes), toolchain
        versions and sandbox. An environment with any unknown component has no
        executor digest (``""``), so it never compares equal to anything.
        """

        if not self.executor_known:
            return ""
        wire = self.to_wire()
        return "envx:sha256:" + canonical_digest(
            {name: wire[name] for name in ("version", "image_digest", "kernel", "toolchain", "executables", "sandbox")}
        )

    def differences(self, other: EnvironmentFingerprint) -> tuple[str, ...]:
        """Fields that differ, so a reproduction can say *why* it is not comparable."""

        mine, theirs = self.to_wire(), other.to_wire()
        out = [
            name
            for name in ("image_digest", "lockfile_digest", "recipe_digest", "sandbox", "kernel", "executables")
            if mine.get(name) != theirs.get(name)
        ]
        tools = sorted(set(mine["toolchain"]) | set(theirs["toolchain"]))
        out.extend(
            f"toolchain.{tool}" for tool in tools if mine["toolchain"].get(tool) != theirs["toolchain"].get(tool)
        )
        return tuple(out)


@dataclass(frozen=True, slots=True)
class UnverifiedIntegration:
    """An integration the sandbox cannot exercise; it is never implied verified."""

    name: str
    reason: str

    def to_wire(self) -> dict[str, str]:
        return {"name": self.name, "reason": self.reason, "status": "not_verified"}


DEFAULT_UNVERIFIED_INTEGRATIONS: tuple[UnverifiedIntegration, ...] = (
    UnverifiedIntegration("payment providers", "real payment networks are unreachable from the sandbox"),
    UnverifiedIntegration("third-party APIs", "external egress is denied; outbound calls are recorded, not answered"),
    UnverifiedIntegration("production data", "the sandbox only sees the revision and trusted-base fixtures"),
)


@dataclass(frozen=True, slots=True)
class VerificationEnvironment:
    """Where a result was verified, and what that environment cannot verify."""

    executor_class: ExecutorClass
    image_digest: str
    network: str = "denied"
    sinks: tuple[str, ...] = ()
    not_verified: tuple[UnverifiedIntegration, ...] = DEFAULT_UNVERIFIED_INTEGRATIONS

    @property
    def location(self) -> str:
        if self.executor_class is ExecutorClass.MANAGED_SANDBOX:
            return "managed sandbox"
        if self.executor_class is ExecutorClass.CUSTOMER_WORKER:
            return "customer worker sandbox"
        if self.executor_class is ExecutorClass.WORKSPACE_TASK:
            return "Workspaces task sandbox (self-reported)"
        return "author machine sandbox (self-attested)"

    @property
    def statement(self) -> str:
        image = self.image_digest or "unknown"
        return f"{self.location} · image {image} · network {self.network}"

    def to_wire(self) -> dict[str, Any]:
        return {
            "executor_class": self.executor_class.value,
            "location": self.location,
            "image_digest": self.image_digest,
            "network": self.network,
            "sinks": list(self.sinks),
            "statement": self.statement,
            "not_verified": [item.to_wire() for item in self.not_verified],
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> VerificationEnvironment:
        rows = raw.get("not_verified")
        unverified = tuple(
            UnverifiedIntegration(str(item.get("name") or "")[:200], str(item.get("reason") or "")[:400])
            for item in (rows if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)) else ())
            if isinstance(item, Mapping) and str(item.get("name") or "")
        )
        sinks = raw.get("sinks")
        return cls(
            executor_class=parse_executor_class(raw.get("executor_class")),
            image_digest=str(raw.get("image_digest") or ""),
            network=str(raw.get("network") or "denied"),
            sinks=(
                tuple(str(item) for item in sinks) if isinstance(sinks, Sequence) and not isinstance(sinks, str) else ()
            ),
            # An empty list on the wire never erases the built-in honesty floor.
            not_verified=unverified or DEFAULT_UNVERIFIED_INTEGRATIONS,
        )


def verification_row(stored: str) -> dict[str, Any] | None:
    """The Reader row for a stored ``execution_verification`` column, ``None`` when absent or unreadable."""

    if not stored:
        return None
    try:
        raw = json.loads(stored)
    except (TypeError, ValueError):
        return None
    if not isinstance(raw, Mapping):
        return None
    wire = VerificationEnvironment.from_wire(raw).to_wire()
    return {
        key: wire[key] for key in ("executor_class", "location", "image_digest", "network", "statement", "not_verified")
    }


SinkKind = Literal["smtp", "http", "queue", "object_store"]
SINK_KINDS: tuple[SinkKind, ...] = ("smtp", "http", "queue", "object_store")
ContentAccess = Literal["external_allowed", "customer_local_only", "ai_denied"]
_CONTENT_ACCESS: frozenset[str] = frozenset({"external_allowed", "customer_local_only", "ai_denied"})


def _content_access(value: object) -> ContentAccess:
    text = str(value or "")
    # Unknown access state is treated as the most restrictive mode.
    return cast(ContentAccess, text) if text in _CONTENT_ACCESS else "ai_denied"


@dataclass(frozen=True, slots=True)
class SinkCapture:
    """One outbound side effect the run stage recorded instead of delivering.

    ``payload_preview`` is already redacted. ``payload_ref`` names the redacted
    raw payload in the run-artifact store (it may expire). ``content_access`` is
    the AI content-access mode governing whether a model may see the payload.
    """

    kind: SinkKind
    destination: str
    method: str = ""
    summary: str = ""
    payload_preview: str = ""
    payload_digest: str = ""
    payload_bytes: int = 0
    payload_ref: str = ""
    content_access: ContentAccess = "ai_denied"
    blocked: bool = False
    redactions: int = 0
    delivered: bool = False

    def __post_init__(self) -> None:
        if self.kind not in SINK_KINDS:
            raise ValueError(f"unknown sink kind {self.kind!r}")
        if self.delivered:
            raise ValueError("a recording sink never delivers to a real destination")

    def to_wire(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "destination": self.destination,
            "method": self.method,
            "summary": self.summary,
            "payload_preview": self.payload_preview,
            "payload_digest": self.payload_digest,
            "payload_bytes": self.payload_bytes,
            "payload_ref": self.payload_ref,
            "content_access": self.content_access,
            "blocked": self.blocked,
            "redactions": self.redactions,
            "delivered": False,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> SinkCapture:
        kind = str(raw.get("kind") or "")
        if kind not in SINK_KINDS:
            raise ValueError(f"unknown sink kind {kind!r}")
        return cls(
            kind=kind,
            destination=str(raw.get("destination") or "")[:2048],
            method=str(raw.get("method") or "")[:32],
            summary=str(raw.get("summary") or "")[:1000],
            payload_preview=str(raw.get("payload_preview") or "")[:4096],
            payload_digest=str(raw.get("payload_digest") or ""),
            payload_bytes=max(0, int(raw.get("payload_bytes") or 0)),
            payload_ref=str(raw.get("payload_ref") or ""),
            content_access=_content_access(raw.get("content_access")),
            blocked=bool(raw.get("blocked")),
            redactions=max(0, int(raw.get("redactions") or 0)),
        )


RetentionClass = Literal["summary", "raw"]


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """Reference to protected (redacted/masked) bytes in the run-artifact store."""

    role: str
    ref: str
    digest: str
    bytes: int
    media_type: str
    retention: RetentionClass = "raw"
    expires_at: float | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "ref": self.ref,
            "digest": self.digest,
            "bytes": self.bytes,
            "media_type": self.media_type,
            "retention": self.retention,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> ArtifactRef:
        retention: RetentionClass = "summary" if str(raw.get("retention") or "") == "summary" else "raw"
        expires = raw.get("expires_at")
        return cls(
            role=str(raw.get("role") or "")[:64],
            ref=str(raw.get("ref") or "")[:512],
            digest=str(raw.get("digest") or ""),
            bytes=max(0, int(raw.get("bytes") or 0)),
            media_type=str(raw.get("media_type") or "")[:128],
            retention=retention,
            expires_at=float(expires) if isinstance(expires, (int, float)) and not isinstance(expires, bool) else None,
        )


StageName = Literal["fetch", "build", "run"]
STAGE_NAMES: tuple[StageName, ...] = ("fetch", "build", "run")
StageStatus = Literal["passed", "failed", "refused", "skipped", "unavailable"]
_STAGE_STATUSES: frozenset[str] = frozenset({"passed", "failed", "refused", "skipped", "unavailable"})


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """One hermetic stage. ``network`` is the egress the stage had, not what it tried."""

    stage: StageName
    status: StageStatus
    network: str = "denied"
    detail: str = ""
    exit_code: int | None = None
    duration_ms: int = 0
    output: str = ""
    blocked: tuple[str, ...] = ()
    cache: str = ""

    def __post_init__(self) -> None:
        if self.stage not in STAGE_NAMES:
            raise ValueError(f"unknown execution stage {self.stage!r}")
        if self.status not in _STAGE_STATUSES:
            raise ValueError(f"unknown stage status {self.status!r}")
        if self.stage != "fetch" and self.network != "denied":
            raise ValueError("build and run stages are always offline")

    def to_wire(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "status": self.status,
            "network": self.network,
            "detail": self.detail,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "output": self.output,
            "blocked": list(self.blocked),
            "cache": self.cache,
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> StageOutcome:
        stage = str(raw.get("stage") or "")
        status = str(raw.get("status") or "")
        if stage not in STAGE_NAMES or status not in _STAGE_STATUSES:
            raise ValueError("execution stage is malformed")
        exit_code = raw.get("exit_code")
        blocked = raw.get("blocked")
        return cls(
            stage=stage,
            status=cast(StageStatus, status),
            network=str(raw.get("network") or "denied"),
            detail=str(raw.get("detail") or "")[:2000],
            exit_code=int(exit_code) if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
            duration_ms=max(0, int(raw.get("duration_ms") or 0)),
            output=str(raw.get("output") or "")[-8000:],
            blocked=(
                tuple(str(item)[:512] for item in blocked)[:256]
                if isinstance(blocked, Sequence) and not isinstance(blocked, str)
                else ()
            ),
            cache=str(raw.get("cache") or "")[:32],
        )


#: Where a runner exposes a fresh writable report directory inside the sandbox
#: (check recipes read per-test reports from it). It lives under the sandbox's
#: private ``/tmp`` so a one-command reproduction can replay the same argv.
REPORT_DIR: Final[str] = "/tmp/lemoncrow-reports"
REPORT_ENV: Final[str] = "LEMONCROW_REPORT_DIR"
#: Where an analysis driver works when the sandbox has no private ``/tmp`` (macOS Seatbelt).
ANALYSIS_WORK_ENV: Final[str] = "LEMONCROW_ANALYSIS_WORK"


@dataclass(frozen=True, slots=True)
class ExecutionSubject:
    """The exact Snapshot side (and scope inside it) an execution ran against.

    ``overlay`` names files of another Snapshot side copied over this one
    before the run (``(path, sha256)``), taken from ``overlay_source`` (that
    side's snapshot identity): a test-adequacy probe runs the head's tests on
    base source. Both are omitted from the wire when empty, so every
    execution without an overlay keeps its identity.
    """

    review_id: str = ""
    revision_id: str = ""
    side: str = "new"
    snapshot_identity: str = ""
    scope: str = "."
    workspace_digest: str = ""
    overlay_source: str = ""
    overlay: tuple[tuple[str, str], ...] = ()

    def to_wire(self) -> dict[str, Any]:
        wire: dict[str, Any] = {
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "side": self.side,
            "snapshot_identity": self.snapshot_identity,
            "scope": self.scope,
            "workspace_digest": self.workspace_digest,
        }
        if self.overlay:
            wire["overlay_source"] = self.overlay_source
            wire["overlay"] = [list(pair) for pair in self.overlay]
        return wire

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> ExecutionSubject:
        values = {name: str(raw.get(name) or default) for name, default in _SUBJECT_DEFAULTS.items()}
        base = cls(
            review_id=values["review_id"],
            revision_id=values["revision_id"],
            side=values["side"],
            snapshot_identity=values["snapshot_identity"],
            scope=values["scope"],
            workspace_digest=values["workspace_digest"],
        )
        overlay_raw = raw.get("overlay")
        if not isinstance(overlay_raw, Sequence) or isinstance(overlay_raw, (str, bytes)) or not overlay_raw:
            return base
        overlay = tuple(
            (str(pair[0])[:1000], str(pair[1])[:100])
            for pair in overlay_raw[:256]
            if isinstance(pair, Sequence) and not isinstance(pair, (str, bytes)) and len(pair) == 2
        )
        if len(overlay) != len(overlay_raw[:256]):
            raise ValueError("execution subject overlay is malformed")
        return replace(base, overlay_source=str(raw.get("overlay_source") or "")[:200], overlay=overlay)


_SUBJECT_DEFAULTS: Mapping[str, str] = {
    "review_id": "",
    "revision_id": "",
    "side": "new",
    "snapshot_identity": "",
    "scope": ".",
    "workspace_digest": "",
}


@dataclass(frozen=True, slots=True)
class ExecutionRecipe:
    """What ran, in enough detail to reproduce it with one command.

    ``policy`` is the trusted-base/organization hermetic policy wire that
    governed the run (never head configuration).
    """

    command: tuple[str, ...] = ()
    cwd: str = "."
    env: tuple[tuple[str, str], ...] = ()
    stages: tuple[StageName, ...] = ("run",)
    sinks: bool = False
    policy: Mapping[str, Any] = field(default_factory=dict)

    def to_wire(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "cwd": self.cwd,
            "env": dict(self.env),
            "stages": list(self.stages),
            "sinks": self.sinks,
            "policy": dict(self.policy),
        }

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> ExecutionRecipe:
        command = raw.get("command")
        stages = raw.get("stages")
        policy = raw.get("policy")
        return cls(
            command=(
                tuple(str(item) for item in command)
                if isinstance(command, Sequence) and not isinstance(command, str)
                else ()
            ),
            cwd=str(raw.get("cwd") or "."),
            env=_str_pairs(raw.get("env")),
            stages=(
                tuple(cast(StageName, item) for item in stages if item in STAGE_NAMES)
                if isinstance(stages, Sequence) and not isinstance(stages, str)
                else ("run",)
            ),
            sinks=bool(raw.get("sinks")),
            policy=dict(policy) if isinstance(policy, Mapping) else {},
        )

    @property
    def digest(self) -> str:
        return canonical_digest(self.to_wire())


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Versioned, typed result of one sandboxed execution (see module docstring)."""

    subject: ExecutionSubject
    executor_class: ExecutorClass
    fingerprint: EnvironmentFingerprint
    verification: VerificationEnvironment
    recipe: ExecutionRecipe = field(default_factory=ExecutionRecipe)
    stages: tuple[StageOutcome, ...] = ()
    sink_captures: tuple[SinkCapture, ...] = ()
    artifacts: tuple[ArtifactRef, ...] = ()
    cache_namespace: CacheNamespace = "head_untrusted"
    content_access: ContentAccess = "ai_denied"
    redaction_policy_digest: str = ""
    reproduce: str = ""
    schema_version: int = EXECUTION_RESULT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version not in SUPPORTED_EXECUTION_RESULT_SCHEMA_VERSIONS:
            raise ValueError(f"execution result schema version {self.schema_version} is not supported")
        if self.verification.executor_class is not self.executor_class:
            raise ValueError("verification statement and result disagree about the executor class")
        if self.cache_namespace not in CACHE_NAMESPACES:
            raise ValueError(f"unknown cache namespace {self.cache_namespace!r}")

    @property
    def environment_fingerprint(self) -> str:
        return self.fingerprint.digest

    @property
    def self_attested(self) -> bool:
        return self.executor_class.self_attested

    @property
    def identity(self) -> str:
        """Subject + executor class + environment fingerprint; outcomes excluded."""

        return canonical_digest(
            {
                "schema_version": self.schema_version,
                "subject": self.subject.to_wire(),
                "executor_class": self.executor_class.value,
                "environment_fingerprint": self.fingerprint.digest,
            }
        )

    @property
    def status(self) -> StageStatus:
        """Overall outcome: the first non-passing stage decides, skipped stages do not."""

        ran = [stage for stage in self.stages if stage.status != "skipped"]
        if not ran:
            return "skipped"
        for stage in ran:
            if stage.status != "passed":
                return stage.status
        return "passed"

    def stage(self, name: StageName) -> StageOutcome | None:
        return next((item for item in self.stages if item.stage == name), None)

    def with_executor(self, executor_class: ExecutorClass) -> ExecutionResult:
        """Re-stamp the authoritative executor class (the dispatcher, not the executor, decides)."""

        return replace(
            self,
            executor_class=executor_class,
            verification=replace(self.verification, executor_class=executor_class),
        )

    def to_wire(self) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema_version": self.schema_version,
            "media_type": EXECUTION_RESULT_MEDIA_TYPE,
            "subject": self.subject.to_wire(),
            "executor_class": self.executor_class.value,
            "self_attested": self.self_attested,
            "environment_fingerprint": self.fingerprint.digest,
            "fingerprint": self.fingerprint.to_wire(),
            "verification": self.verification.to_wire(),
            "recipe": self.recipe.to_wire(),
            "stages": [stage.to_wire() for stage in self.stages],
            "status": self.status,
            "sink_captures": [capture.to_wire() for capture in self.sink_captures],
            "artifacts": [artifact.to_wire() for artifact in self.artifacts],
            "cache_namespace": self.cache_namespace,
            "content_access": self.content_access,
            "redaction_policy_digest": self.redaction_policy_digest,
            "reproduce": self.reproduce,
            "identity": self.identity,
        }
        document["result_digest"] = canonical_digest({key: value for key, value in document.items()})
        return document

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> ExecutionResult:
        version = int(raw.get("schema_version") or 0)
        if version not in SUPPORTED_EXECUTION_RESULT_SCHEMA_VERSIONS:
            raise ValueError(f"execution result schema version {version} is not supported")

        def mapping(name: str) -> Mapping[str, Any]:
            value = raw.get(name)
            if not isinstance(value, Mapping):
                raise ValueError(f"execution result {name} is missing")
            return value

        def rows(name: str) -> Sequence[Mapping[str, Any]]:
            value = raw.get(name)
            if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
                return ()
            return tuple(item for item in value if isinstance(item, Mapping))

        executor = parse_executor_class(raw.get("executor_class"))
        verification = VerificationEnvironment.from_wire(mapping("verification"))
        namespace = str(raw.get("cache_namespace") or "head_untrusted")
        result = cls(
            subject=ExecutionSubject.from_wire(mapping("subject")),
            executor_class=executor,
            fingerprint=EnvironmentFingerprint.from_wire(mapping("fingerprint")),
            verification=replace(verification, executor_class=executor),
            recipe=ExecutionRecipe.from_wire(mapping("recipe")),
            stages=tuple(StageOutcome.from_wire(item) for item in rows("stages")),
            sink_captures=tuple(SinkCapture.from_wire(item) for item in rows("sink_captures")),
            artifacts=tuple(ArtifactRef.from_wire(item) for item in rows("artifacts")),
            cache_namespace=namespace if namespace in CACHE_NAMESPACES else "head_untrusted",
            content_access=_content_access(raw.get("content_access")),
            redaction_policy_digest=str(raw.get("redaction_policy_digest") or ""),
            reproduce=str(raw.get("reproduce") or "")[:2048],
            schema_version=version,
        )
        claimed = str(raw.get("identity") or "")
        if claimed and claimed != result.identity:
            raise ValueError("execution result identity does not match its subject/executor/fingerprint")
        return result


__all__ = [
    "CACHE_NAMESPACES",
    "DEFAULT_UNVERIFIED_INTEGRATIONS",
    "ENVIRONMENT_FINGERPRINT_VERSION",
    "EXECUTION_RESULT_MEDIA_TYPE",
    "EXECUTION_RESULT_SCHEMA_VERSION",
    "EXECUTOR_CLASSES",
    "EXECUTOR_FINGERPRINT_VERSION",
    "PARTIAL_IMAGE_PREFIX",
    "REPORT_DIR",
    "REPORT_ENV",
    "RUN_ON_BY_EXECUTOR_CLASS",
    "SINK_KINDS",
    "STAGE_NAMES",
    "SUPPORTED_ENVIRONMENT_FINGERPRINT_VERSIONS",
    "TRUSTED_EXECUTOR_CLASSES",
    "UNKNOWN_ENVIRONMENT_VALUES",
    "ArtifactRef",
    "CacheNamespace",
    "ContentAccess",
    "EnvironmentFingerprint",
    "ExecutableIdentity",
    "ExecutionRecipe",
    "ExecutionResult",
    "ExecutionSubject",
    "ExecutorClass",
    "RetentionClass",
    "SinkCapture",
    "SinkKind",
    "StageName",
    "StageOutcome",
    "StageStatus",
    "UnverifiedIntegration",
    "VerificationEnvironment",
    "cache_namespace",
    "canonical_digest",
    "executor_class_for_locus",
    "executor_satisfies",
    "normalize_executor_requirement",
    "parse_executor_class",
    "readable_cache_namespaces",
    "run_on_for_locus",
]
