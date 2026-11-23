"""Protect and expire Review run evidence (Phase 9R item 39).

Three guarantees for everything an execution produces:

1. **Protected before storage.** Logs, traces, sink payloads and record text
   pass :func:`protect_text` (built-in secret patterns plus trusted-base
   configured patterns and literal values) and screenshots pass
   :func:`mask_png` *before* any byte is written. :class:`RunArtifactStore`
   only accepts :class:`ProtectedBytes`, which only those functions mint.
2. **Content-access gated before any model.** :func:`execution_evidence_for_model`
   is the single projection of an execution result a model route may receive;
   payloads whose AI content-access mode denies the route are withheld.
3. **Tiered retention.** Raw heavy bytes (video/trace/log/payload) expire after
   a configurable window unless pinned by a judgment, finding or required
   check, or reported referenced by the caller's resolver. Summaries (the
   :class:`~lemoncrow.pro.environment.execution.ExecutionResult`
   record) live in the ordinary Review evidence store and persist.

The store's writer (:meth:`RunArtifactStore.put`) runs its closer
(:meth:`RunArtifactStore.expire`) opportunistically when it was built with a
reference resolver (:meth:`ReviewStore.run_artifacts` always is), so retention
holds even without a separate maintenance job; a resolver-less pass never
deletes, because it cannot tell which bytes a judgment cites.
:meth:`RunArtifactStore.expire` is also the maintenance entry point (local
service worker; hosted Review retention pass).

Local stores keep bytes beside the Review store on disk. Hosted stores put
bytes and metadata through their org-scoped Review artifact backend (sealed
with source content on encrypted deployments) under the same
``review/run-artifacts/<review_id>/`` keys.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, NewType

from lemoncrow_client.kit.redaction import redact as _builtin_redact

from lemoncrow.pro.environment.execution import ArtifactRef, ExecutionResult, RetentionClass

DEFAULT_RAW_RETENTION_SECONDS: Final[int] = 14 * 24 * 3600
MIN_RAW_RETENTION_SECONDS: Final[int] = 3600
MAX_RAW_RETENTION_SECONDS: Final[int] = 365 * 24 * 3600
_MAX_PATTERNS: Final[int] = 64
_MAX_PATTERN_CHARS: Final[int] = 512
_MAX_LITERAL_CHARS: Final[int] = 4096
_MIN_LITERAL_CHARS: Final[int] = 6
_MAX_MASK_REGIONS: Final[int] = 64
_EXPIRE_EVERY_SECONDS: Final[float] = 300.0
_REVIEW_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")

ProtectedBytes = NewType("ProtectedBytes", bytes)
"""Bytes that already passed redaction/masking; the only input the store accepts."""


@dataclass(frozen=True, slots=True)
class MaskRegion:
    """One screenshot rectangle (CSS pixels of the captured viewport) to black out."""

    x: int
    y: int
    width: int
    height: int

    def to_wire(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True, slots=True)
class RedactionPolicy:
    """Sensitive-content policy from trusted-base/organization configuration only.

    ``patterns`` are regular expressions; ``literals`` are exact sensitive
    values (for example a fixture password) masked wherever they appear.
    """

    patterns: tuple[str, ...] = ()
    literals: tuple[str, ...] = ()
    mask_regions: tuple[MaskRegion, ...] = ()

    def __post_init__(self) -> None:
        if len(self.patterns) > _MAX_PATTERNS or len(self.literals) > _MAX_PATTERNS:
            raise ValueError(f"redaction policy exceeds {_MAX_PATTERNS} entries")
        if len(self.mask_regions) > _MAX_MASK_REGIONS:
            raise ValueError(f"screenshot mask exceeds {_MAX_MASK_REGIONS} regions")
        for pattern in self.patterns:
            if not pattern or len(pattern) > _MAX_PATTERN_CHARS:
                raise ValueError("redaction pattern is empty or too long")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValueError(f"redaction pattern is invalid: {exc}") from exc
        for literal in self.literals:
            if len(literal) < _MIN_LITERAL_CHARS or len(literal) > _MAX_LITERAL_CHARS:
                raise ValueError(f"redaction literal must be {_MIN_LITERAL_CHARS}-{_MAX_LITERAL_CHARS} characters")

    @classmethod
    def from_config(cls, raw: object) -> RedactionPolicy:
        """Parse ``execution.redact`` / ``execution.mask`` trusted configuration."""

        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            raise ValueError("execution redaction policy must be a mapping")
        patterns = _string_list(raw.get("redact"), field="execution.redact")
        literals = _string_list(raw.get("redact_literals"), field="execution.redact_literals")
        regions: list[MaskRegion] = []
        masks = raw.get("mask")
        if masks is not None:
            if not isinstance(masks, Sequence) or isinstance(masks, (str, bytes)):
                raise ValueError("execution.mask must be a list")
            for index, item in enumerate(masks):
                if not isinstance(item, Mapping):
                    raise ValueError(f"execution.mask[{index}] must be a mapping")
                try:
                    region = MaskRegion(*(int(item[key]) for key in ("x", "y", "width", "height")))
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError(f"execution.mask[{index}] needs integer x, y, width and height") from exc
                if region.x < 0 or region.y < 0 or region.width <= 0 or region.height <= 0:
                    raise ValueError(f"execution.mask[{index}] is not a positive rectangle")
                regions.append(region)
        return cls(patterns=patterns, literals=literals, mask_regions=tuple(regions))

    def to_wire(self) -> dict[str, Any]:
        # Literal values are secrets themselves: only their digest is exported.
        return {
            "patterns": list(self.patterns),
            "literals": [hashlib.sha256(item.encode()).hexdigest() for item in self.literals],
            "mask": [region.to_wire() for region in self.mask_regions],
        }

    @property
    def digest(self) -> str:
        payload = json.dumps(self.to_wire(), sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(payload).hexdigest()


def _string_list(raw: object, *, field: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,) if raw else ()
    if not isinstance(raw, Sequence) or isinstance(raw, (bytes, bytearray)):
        raise ValueError(f"{field} must be a string or list")
    return tuple(str(item) for item in raw if str(item))


def protect_text(text: str, policy: RedactionPolicy | None = None) -> tuple[str, int]:
    """Redact secrets and configured sensitive content; return (text, replacements)."""

    if not text:
        return text, 0
    out = str(_builtin_redact(text))
    count = 0 if out == text else max(1, out.count("<redacted-"))
    if policy is not None:
        for literal in policy.literals:
            if literal in out:
                count += out.count(literal)
                out = out.replace(literal, "<redacted-configured>")
        for pattern in policy.patterns:
            out, replaced = re.subn(pattern, "<redacted-configured>", out)
            count += replaced
    return out, count


def protect_bytes(payload: bytes, policy: RedactionPolicy | None = None) -> tuple[ProtectedBytes, int]:
    """Redact a textual payload for storage; undecodable bytes are dropped, never stored raw."""

    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        # Binary payloads cannot be scanned for secrets, so only their size and
        # digest survive. Storing unscanned bytes would defeat redaction.
        marker = f"<binary payload withheld: {len(payload)} bytes sha256:{hashlib.sha256(payload).hexdigest()}>"
        return ProtectedBytes(marker.encode()), 1
    redacted, count = protect_text(text, policy)
    return ProtectedBytes(redacted.encode("utf-8")), count


def protect_mapping(value: Any, policy: RedactionPolicy | None = None) -> Any:
    """Recursively redact every string inside a JSON-like record (keys preserved)."""

    if isinstance(value, str):
        return protect_text(value, policy)[0]
    if isinstance(value, Mapping):
        return {key: protect_mapping(item, policy) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [protect_mapping(item, policy) for item in value]
    return value


def protect_execution_result(result: ExecutionResult, policy: RedactionPolicy | None = None) -> ExecutionResult:
    """Redact every free-text field of an execution record before it is stored or shown.

    Stage output/detail, capture destinations/summaries/previews and the
    recipe's command line and environment values are redacted. Identity
    fields (ids, digests, executor class) are structural and unchanged; a
    redacted recipe then reproduces with a stated ``recipe_digest`` difference.
    """

    from dataclasses import replace

    def clean(text: str) -> str:
        return protect_text(text, policy)[0]

    stages = tuple(
        replace(
            stage,
            detail=clean(stage.detail),
            output=clean(stage.output),
            blocked=tuple(clean(item) for item in stage.blocked),
        )
        for stage in result.stages
    )
    captures = tuple(
        replace(
            capture,
            destination=clean(capture.destination),
            summary=clean(capture.summary),
            payload_preview=clean(capture.payload_preview),
        )
        for capture in result.sink_captures
    )
    recipe = replace(
        result.recipe,
        command=tuple(clean(item) for item in result.recipe.command),
        env=tuple((key, clean(value)) for key, value in result.recipe.env),
    )
    return replace(result, stages=stages, sink_captures=captures, recipe=recipe)


class MaskingUnavailable(RuntimeError):
    """A screenshot could not be masked; callers must withhold it (fail closed)."""


def mask_png(payload: bytes, regions: Sequence[MaskRegion]) -> ProtectedBytes:
    """Black out *regions* in a PNG before it is stored.

    Raises :class:`MaskingUnavailable` when the image cannot be decoded or the
    imaging library is missing; the caller then withholds the screenshot
    instead of storing it unmasked.
    """

    if not regions:
        return ProtectedBytes(payload)
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:  # pragma: no cover - depends on the host install
        # lc-debt: masking needs Pillow (optional); without it masked screenshots are withheld. Vendor a PNG codec if this matters.
        raise MaskingUnavailable("screenshot masking needs Pillow; the screenshot was withheld") from exc
    try:
        with Image.open(io.BytesIO(payload)) as image:
            image.load()
            canvas = image.convert("RGBA")
    except Exception as exc:
        raise MaskingUnavailable(f"screenshot could not be decoded for masking: {type(exc).__name__}") from exc
    draw = ImageDraw.Draw(canvas)
    for region in regions:
        draw.rectangle(
            (region.x, region.y, region.x + region.width - 1, region.y + region.height - 1),
            fill=(0, 0, 0, 255),
        )
    out = io.BytesIO()
    canvas.save(out, format="PNG")
    return ProtectedBytes(out.getvalue())


# --------------------------------------------------------------------------- #
# Model gate
# --------------------------------------------------------------------------- #

_ACCESS_RANK: Mapping[str, int] = {"external_allowed": 0, "customer_local_only": 1, "ai_denied": 2}


def most_restrictive_access(modes: Iterable[str]) -> str:
    """Most restrictive AI content-access mode; unknown modes count as ``ai_denied``."""

    best = "external_allowed"
    for mode in modes:
        if _ACCESS_RANK.get(mode, 2) > _ACCESS_RANK[best]:
            best = mode if mode in _ACCESS_RANK else "ai_denied"
    return best


def access_allows(mode: str, route_trust: str) -> bool:
    """Phase 6 semantics: denied never; local-only only on a customer-local route."""

    if mode == "external_allowed":
        return True
    if mode == "customer_local_only":
        return route_trust == "customer_local"
    return False


def execution_evidence_for_model(result: ExecutionResult | Mapping[str, Any], *, route_trust: str) -> dict[str, Any]:
    """The only projection of execution-derived evidence a model route may receive.

    Outcome facts (stage statuses, verification statement, capture kinds and
    destinations) are structural. Execution-derived *content* (stage output,
    sink payload previews) is included only when the result's and the
    capture's content-access modes allow *route_trust*; otherwise it is
    replaced by an explicit ``withheld`` marker so a model cannot mistake its
    absence for a clean run.
    """

    parsed = result if isinstance(result, ExecutionResult) else ExecutionResult.from_wire(result)
    content_allowed = access_allows(parsed.content_access, route_trust)
    withheld = f"withheld: AI content access {parsed.content_access} for {route_trust} route"
    stages = [
        {
            "stage": stage.stage,
            "status": stage.status,
            "network": stage.network,
            "output": stage.output if content_allowed else withheld,
        }
        for stage in parsed.stages
    ]
    captures = []
    for capture in parsed.sink_captures:
        allowed = content_allowed and access_allows(capture.content_access, route_trust)
        captures.append(
            {
                "kind": capture.kind,
                "destination": capture.destination,
                "method": capture.method,
                "delivered": False,
                "payload": (
                    capture.payload_preview
                    if allowed
                    else f"withheld: AI content access {capture.content_access} for {route_trust} route"
                ),
            }
        )
    return {
        "identity": parsed.identity,
        "status": parsed.status,
        "executor_class": parsed.executor_class.value,
        "self_attested": parsed.self_attested,
        "verification": parsed.verification.statement,
        "not_verified": [item.name for item in parsed.verification.not_verified],
        "stages": stages,
        "sink_captures": captures,
    }


def execution_evidence_model_label(title: str, status: str, executor_class: str) -> str:
    """Label for execution-derived evidence in a model's evidence registry.

    Only trusted-configuration text (the scenario title), the typed status and
    the executor class; never execution output or captured payloads.
    """

    parts = [title or "execution"]
    if status:
        parts.append(f"status={status}")
    parts.append(f"executor={executor_class or 'author_machine'}")
    return " · ".join(parts)[:320]


# --------------------------------------------------------------------------- #
# Raw run-artifact store with tiered retention
# --------------------------------------------------------------------------- #

ReferenceResolver = Callable[[str, Mapping[str, Any]], bool]
"""``(review_id, meta) -> referenced`` used by the closer for live references."""


def review_reference_resolver(store: Any) -> ReferenceResolver:
    """Closer resolver backed by the Review store.

    A raw run artifact stays referenced while a judgment or finding cites it
    (directly, or through the evidence record that carries it) or while a
    check attestation that can satisfy a required check carries it. Human
    behavior judgments (``beh:`` marks) and human-observation checklist
    results also keep the execution evidence of the Snapshot they were made
    on, plus any run artifact their note names. A deleted Review references
    nothing. A Review whose state cannot be read keeps its bytes until the
    next pass rather than losing cited evidence.
    """

    cache: dict[str, frozenset[str] | None] = {}

    def referenced(review_id: str) -> frozenset[str] | None:
        if review_id in cache:
            return cache[review_id]
        # lc-debt: reads every cited/attesting evidence record of a Review once per pass; index refs at write if Reviews grow large.
        pattern = re.compile(rb"run-artifact:" + re.escape(review_id.encode()) + rb":[0-9a-f]{64}")
        refs: set[str] = set()
        try:
            cited = {ref for annotation in store.list_annotations(review_id) for ref in annotation.evidence}
            refs.update(ref for ref in cited if ref.startswith("run-artifact:"))
            evidence_rows = store.list_evidence(review_id)
            # lc-debt: keeps all execution evidence of a judged/observed Snapshot, not only the
            # judged target's own refs; resolve BehaviorDelta.evidence per target if storage matters.
            human_snapshots: set[str] = set()
            for revision_id, note in store.human_behavior_judgments(review_id):
                human_snapshots.add(revision_id)
                refs.update(match.decode() for match in pattern.findall(note.encode()))
            for evidence in evidence_rows:
                if evidence.attestation_class == "human_observation":
                    human_snapshots.add(evidence.revision_id)
                    refs.update(match.decode() for match in pattern.findall(evidence.detail.encode()))
            for evidence in evidence_rows:
                if not evidence.executor_class or not (
                    evidence.stable_key or evidence.id in cited or evidence.revision_id in human_snapshots
                ):
                    continue
                payload = store.read_evidence_artifact(evidence) or b""
                refs.update(match.decode() for match in pattern.findall(payload))
        except Exception:
            cache[review_id] = None
            return None
        cache[review_id] = frozenset(refs)
        return cache[review_id]

    def resolve(review_id: str, meta: Mapping[str, Any]) -> bool:
        refs = referenced(review_id)
        return refs is None or f"run-artifact:{review_id}:{meta.get('digest')}" in refs

    return resolve


RUN_ARTIFACT_PREFIX: Final[str] = "review/run-artifacts"


class _FileArtifactBytes:
    """Local layout: ``<root>/<review_id>/<name>`` with atomic writes."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def reviews(self) -> list[str]:
        try:
            return [item.name for item in self.root.iterdir() if item.is_dir()]
        except OSError:
            return []

    def names(self, review_id: str, suffix: str) -> list[str]:
        try:
            return [item.name for item in (self.root / review_id).iterdir() if item.name.endswith(suffix)]
        except OSError:
            return []

    def exists(self, review_id: str, name: str) -> bool:
        return (self.root / review_id / name).is_file()

    def read(self, review_id: str, name: str) -> bytes | None:
        try:
            return (self.root / review_id / name).read_bytes()
        except OSError:
            return None

    def write(self, review_id: str, name: str, payload: bytes) -> None:
        directory = self.root / review_id
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".{name}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_bytes(payload)
        tmp.replace(directory / name)

    def delete(self, review_id: str, name: str) -> None:
        (self.root / review_id / name).unlink(missing_ok=True)

    def delete_review(self, review_id: str) -> None:
        import shutil

        shutil.rmtree(self.root / review_id, ignore_errors=True)

    def tidy(self, review_id: str) -> None:
        try:
            (self.root / review_id).rmdir()
        except OSError:
            pass


class _BackendArtifactBytes:
    """Hosted layout: keys ``review/run-artifacts/<review_id>/<name>`` in the Review artifact backend."""

    def __init__(self, backend: Any) -> None:
        self.backend = backend
        self.prefix = RUN_ARTIFACT_PREFIX + "/"

    def _key(self, review_id: str, name: str) -> str:
        return f"{self.prefix}{review_id}/{name}"

    def reviews(self) -> list[str]:
        found = {str(key)[len(self.prefix) :].split("/", 1)[0] for key in self.backend.list_keys(self.prefix)}
        return sorted(item for item in found if item)

    def names(self, review_id: str, suffix: str) -> list[str]:
        base = f"{self.prefix}{review_id}/"
        tails = (str(key)[len(base) :] for key in self.backend.list_keys(base))
        return [tail for tail in tails if tail.endswith(suffix) and "/" not in tail]

    def exists(self, review_id: str, name: str) -> bool:
        return self.read(review_id, name) is not None

    def read(self, review_id: str, name: str) -> bytes | None:
        payload = self.backend.read(self._key(review_id, name))
        return payload if isinstance(payload, bytes) else None

    def write(self, review_id: str, name: str, payload: bytes) -> None:
        self.backend.write(self._key(review_id, name), payload)

    def delete(self, review_id: str, name: str) -> None:
        self.backend.delete_prefix(self._key(review_id, name))

    def delete_review(self, review_id: str) -> None:
        self.backend.delete_prefix(f"{self.prefix}{review_id}/")

    def tidy(self, review_id: str) -> None:
        return None


class RunArtifactStore:
    """Content-addressed raw run artifacts with an expiry window (writer + closer).

    Layout: ``<review_id>/<sha256>.bin`` plus ``<sha256>.json`` metadata
    (role, media type, retention class, created/expiry time, pins, owner),
    under ``root`` locally or under ``review/run-artifacts/`` keys of a hosted
    Review artifact backend. ``summary`` entries never expire; ``raw`` entries
    expire after the window unless pinned or referenced. A hosted backend must
    offer ``list_keys(prefix)`` so its closer can enumerate what it wrote.
    """

    def __init__(
        self,
        root: Path,
        *,
        retention_seconds: int = DEFAULT_RAW_RETENTION_SECONDS,
        clock: Callable[[], float] = time.time,
        backend: Any | None = None,
        resolver: ReferenceResolver | None = None,
    ) -> None:
        self.root = root
        self.retention_seconds = max(MIN_RAW_RETENTION_SECONDS, min(int(retention_seconds), MAX_RAW_RETENTION_SECONDS))
        self.resolver = resolver
        self._clock = clock
        # lc-debt: process-local lock; concurrent hosted replicas can race a pin/expiry
        # metadata update (last writer wins). The resolver still keeps referenced bytes.
        self._lock = threading.Lock()
        self._last_expire = 0.0
        self._bytes: _FileArtifactBytes | _BackendArtifactBytes
        if backend is None:
            self._bytes = _FileArtifactBytes(root)
        elif callable(getattr(backend, "list_keys", None)):
            self._bytes = _BackendArtifactBytes(backend)
        else:
            raise ValueError("run artifact backend cannot list its keys, so retention could not be enforced")

    @property
    def hosted(self) -> bool:
        return isinstance(self._bytes, _BackendArtifactBytes)

    @staticmethod
    def for_store(store_root: Path, **kwargs: Any) -> RunArtifactStore:
        return RunArtifactStore(store_root / "review" / "run-artifacts", **kwargs)

    @staticmethod
    def _review(review_id: str) -> str:
        if review_id in {".", ".."} or not _REVIEW_ID_RE.match(review_id):
            raise ValueError("run artifact review id is invalid")
        return review_id

    @staticmethod
    def _check_digest(digest: str) -> str:
        if not _DIGEST_RE.match(digest):
            raise ValueError("run artifact digest is invalid")
        return digest

    def put(
        self,
        review_id: str,
        payload: ProtectedBytes,
        *,
        role: str,
        media_type: str,
        retention: RetentionClass = "raw",
        owner: str = "",
    ) -> ArtifactRef:
        """Store protected bytes; returns an :class:`ArtifactRef`. Runs the closer first."""
        self.maybe_expire()
        review = self._review(review_id)
        digest = hashlib.sha256(payload).hexdigest()
        now = self._clock()
        expires_at = None if retention == "summary" else now + self.retention_seconds
        with self._lock:
            meta = self._load_meta(review, digest)
            if meta is None or not self._bytes.exists(review, f"{digest}.bin"):
                self._bytes.write(review, f"{digest}.bin", bytes(payload))
                meta = {
                    "digest": digest,
                    "role": role[:64],
                    "media_type": media_type[:128],
                    "bytes": len(payload),
                    "retention": retention,
                    "created_at": now,
                    "expires_at": expires_at,
                    "pins": [],
                    "owner": owner[:256],
                }
            else:
                # Re-putting identical content refreshes its window (never shortens it).
                if retention == "summary" or meta.get("retention") == "summary":
                    meta["retention"], meta["expires_at"] = "summary", None
                elif meta.get("expires_at") is not None and expires_at is not None:
                    meta["expires_at"] = max(float(meta["expires_at"]), expires_at)
                if owner and not meta.get("owner"):
                    meta["owner"] = owner[:256]
            self._store_meta(review, digest, meta)
        return ArtifactRef(
            role=role,
            ref=f"run-artifact:{review_id}:{digest}",
            digest=digest,
            bytes=len(payload),
            media_type=media_type,
            retention=retention,
            expires_at=meta.get("expires_at"),
        )

    def read(self, review_id: str, digest: str) -> bytes | None:
        return self._bytes.read(self._review(review_id), f"{self._check_digest(digest)}.bin")

    def meta(self, review_id: str, digest: str) -> dict[str, Any] | None:
        return self._load_meta(self._review(review_id), self._check_digest(digest))

    def pin(self, review_id: str, digest: str, reason: str) -> None:
        """Retain raw bytes because a judgment, finding or required check references them.

        ``reason`` is a typed reference such as ``judgment:<id>``,
        ``finding:<id>`` or ``required_check:<key>``.
        """

        if not reason or reason.split(":", 1)[0] not in {"judgment", "finding", "required_check"}:
            raise ValueError("pin reason must be judgment:<id>, finding:<id> or required_check:<key>")
        review, digest = self._review(review_id), self._check_digest(digest)
        with self._lock:
            meta = self._load_meta(review, digest)
            if meta is None:
                raise KeyError(f"run artifact {digest} is not stored (it may have expired)")
            pins = [str(item) for item in meta.get("pins", [])]
            if reason not in pins:
                pins.append(reason[:256])
            meta["pins"] = pins
            self._store_meta(review, digest, meta)

    def unpin(self, review_id: str, digest: str, reason: str) -> None:
        review, digest = self._review(review_id), self._check_digest(digest)
        with self._lock:
            meta = self._load_meta(review, digest)
            if meta is None:
                return
            meta["pins"] = [item for item in meta.get("pins", []) if item != reason]
            self._store_meta(review, digest, meta)

    def maybe_expire(self, resolver: ReferenceResolver | None = None) -> None:
        """Rate-limited closer pass; skipped without a resolver (it could drop cited bytes)."""

        resolver = resolver or self.resolver
        now = self._clock()
        if resolver is None or now - self._last_expire < _EXPIRE_EVERY_SECONDS:
            return
        self._last_expire = now
        self.expire(resolver=resolver)

    def expire(self, *, resolver: ReferenceResolver | None = None) -> tuple[str, ...]:
        """Closer: delete expired raw bytes that nothing references; return removed refs.

        Pinned entries and entries the resolver reports as referenced are kept
        (their window restarts so a later unpin still gets a grace period).
        Metadata of removed entries is removed with the bytes.
        """

        now = self._clock()
        removed: list[str] = []
        for review in self._bytes.reviews():
            if review in {".", ".."} or not _REVIEW_ID_RE.match(review):
                continue
            for name in self._bytes.names(review, ".json"):
                digest = name[: -len(".json")]
                if not _DIGEST_RE.match(digest):
                    continue
                with self._lock:
                    meta = self._load_meta(review, digest)
                    if meta is None or meta.get("retention") == "summary":
                        continue
                    expires = meta.get("expires_at")
                    if not isinstance(expires, (int, float)) or now < float(expires):
                        continue
                    referenced = bool(meta.get("pins")) or (resolver is not None and resolver(review, meta))
                    if referenced:
                        meta["expires_at"] = now + self.retention_seconds
                        self._store_meta(review, digest, meta)
                        continue
                    self._bytes.delete(review, f"{digest}.bin")
                    self._bytes.delete(review, f"{digest}.json")
                    removed.append(f"run-artifact:{review}:{digest}")
            self._bytes.tidy(review)
        return tuple(removed)

    def delete_review(self, review_id: str) -> None:
        """Closer for a deleted Review: drop every artifact it owns, pinned or not."""

        self._bytes.delete_review(self._review(review_id))

    def _load_meta(self, review_id: str, digest: str) -> dict[str, Any] | None:
        raw = self._bytes.read(review_id, f"{digest}.json")
        try:
            value = json.loads(raw.decode("utf-8")) if raw is not None else None
        except (UnicodeDecodeError, ValueError):
            return None
        return value if isinstance(value, dict) else None

    def _store_meta(self, review_id: str, digest: str, meta: Mapping[str, Any]) -> None:
        self._bytes.write(review_id, f"{digest}.json", json.dumps(dict(meta), sort_keys=True).encode("utf-8"))


__all__ = [
    "DEFAULT_RAW_RETENTION_SECONDS",
    "RUN_ARTIFACT_PREFIX",
    "MaskRegion",
    "MaskingUnavailable",
    "ProtectedBytes",
    "RedactionPolicy",
    "ReferenceResolver",
    "RunArtifactStore",
    "access_allows",
    "execution_evidence_for_model",
    "execution_evidence_model_label",
    "mask_png",
    "most_restrictive_access",
    "protect_bytes",
    "protect_execution_result",
    "protect_mapping",
    "protect_text",
    "review_reference_resolver",
]
