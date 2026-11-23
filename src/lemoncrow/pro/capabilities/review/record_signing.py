"""Signing review records: DSSE envelopes, signers, persistence and the landing hook.

A record is a DSSE envelope (``payloadType`` in-toto, base64 canonical payload,
signatures over the DSSE pre-authentication encoding) around the Statement
:mod:`.review_record` builds. Who may sign is a composition seam,
:class:`RecordSigningAuthority`:

* local Reviews sign with the reviewer's own Ed25519 key only after an explicit
  opt-in (``review.record_signing_key`` in the user-level settings file, written
  by ``lc review record-key init``); the key file must be owner-only and may
  never live inside a Git work tree, so it can never be committed with the
  repository. Without it a local record is exported explicitly UNSIGNED;
* hosted Reviews sign in the control plane with organization-scoped keys held
  behind a KMS/HSM seam (enterprise ``review_signing_keys``). No process that
  executes reviewed code constructs an authority.

An export that asks for a signature never silently degrades to unsigned:
:class:`RecordSigningUnavailable` refuses it with the reason, and the caller
may then ask for an explicitly unsigned record.

:func:`sign_review_record` is the hook P10-LAND calls after its landing
decision (and the explicit export / provider-merge paths call today).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from lemoncrow.pro.capabilities.review.review_record import (
    PAYLOAD_TYPE,
    RECORD_RELATIONSHIP_KIND,
    UNSIGNED,
    RecordContext,
    ReviewDecision,
    SigningIdentity,
    build_statement,
    canonical_json,
    sha256_digest,
    summarize,
)

if TYPE_CHECKING:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

    from lemoncrow.pro.capabilities.review.session_models import ReviewSession

#: User-level setting naming the reviewer's own Ed25519 private key (PEM, PKCS#8).
USER_KEY_SETTING: Final[str] = "review.record_signing_key"
USER_KEY_INIT_COMMAND: Final[str] = "lc review record-key init"
SIGNATURE_ALGORITHMS: Final[tuple[str, ...]] = ("Ed25519", "ES256")
_RECORD_ID_PREFIX: Final[str] = "rec_"
#: The statement facts a record list row shows (display only; verification is a separate verdict).
_LIST_SUMMARY_KEYS: Final[frozenset[str]] = frozenset(
    {"decision", "decided_by", "decided_at", "snapshot_number", "head_sha", "parts_reviewed_one_by_one"}
)


class RecordSigningUnavailable(Exception):
    """Signing was asked for and cannot happen; never a silent unsigned fallback.

    ``code``: ``not_configured`` (no key / not opted in), ``not_available`` (the
    configured mechanism cannot sign here, e.g. Sigstore keyless without a
    Fulcio/Rekor client), ``forbidden`` (this principal may not sign) or
    ``provider_error`` (the key provider failed).
    """

    def __init__(self, reason: str, *, code: str = "not_configured") -> None:
        super().__init__(reason)
        self.reason = reason
        self.code = code


@dataclass(frozen=True, slots=True)
class SigningStatus:
    """What an export would sign with right now (display and API)."""

    mode: str
    available: bool
    reason: str = ""
    key_id: str = ""
    algorithm: str = ""
    issuer_kind: str = ""
    issuer_id: str = ""
    code: str = ""

    def to_wire(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "available": self.available,
            "reason": self.reason,
            "code": self.code,
            "key_id": self.key_id,
            "algorithm": self.algorithm,
            "issuer_kind": self.issuer_kind,
            "issuer_id": self.issuer_id,
        }


class RecordSigner(Protocol):
    """One signing key. ``sign`` returns the raw signature over *message*."""

    @property
    def identity(self) -> SigningIdentity: ...

    def sign(self, message: bytes) -> bytes: ...


class RecordSigningAuthority(Protocol):
    """Composition seam deciding who may sign a Review's record, and with what."""

    def status(self, session: ReviewSession) -> SigningStatus: ...

    def signer(self, session: ReviewSession) -> RecordSigner:
        """The signer for an export now; raises :class:`RecordSigningUnavailable`."""
        ...

    def public_keys(self, session: ReviewSession) -> Mapping[str, Any]:
        """The published key set (JWKS-like, with status and revocation) records verify against."""
        ...

    def exporter(self, session: ReviewSession) -> tuple[str, str]:
        """``(actor id, actor type)`` of whoever is exporting now."""
        ...

    def issuer(self, session: ReviewSession) -> tuple[str, str]:
        """``(issuer kind, issuer id)``: ``organization``/org id hosted, ``local``/key id locally."""
        ...

    def audit(self, session: ReviewSession, event: str, *, allowed: bool, detail: Mapping[str, str]) -> None:
        """Security-audit one signing event (allowed or refused)."""
        ...


class Ed25519RecordSigner:
    """A signer holding one Ed25519 private key in this (control-plane or local) process."""

    def __init__(
        self,
        private_key: Ed25519PrivateKey,
        *,
        key_id: str,
        mode: str,
        issuer_kind: str = "",
        issuer_id: str = "",
    ) -> None:
        self._private_key = private_key
        self._identity = SigningIdentity(
            mode=mode, key_id=key_id, algorithm="Ed25519", issuer_kind=issuer_kind, issuer_id=issuer_id
        )

    @property
    def identity(self) -> SigningIdentity:
        return self._identity

    def sign(self, message: bytes) -> bytes:
        return self._private_key.sign(message)


# --------------------------------------------------------------------------- #
# DSSE
# --------------------------------------------------------------------------- #


def pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE v1 pre-authentication encoding: what a signature actually covers."""

    kind = payload_type.encode("utf-8")
    return b"DSSEv1 %d %s %d %s" % (len(kind), kind, len(payload), payload)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def dsse_envelope(payload: bytes, signer: RecordSigner | None) -> dict[str, Any]:
    """A DSSE envelope; ``signatures`` is empty for an unsigned record."""

    signatures: list[dict[str, str]] = []
    if signer is not None:
        signature = signer.sign(pae(PAYLOAD_TYPE, payload))
        if not signature:
            raise RecordSigningUnavailable("the key provider returned an empty signature", code="provider_error")
        signatures.append({"keyid": signer.identity.key_id, "sig": _b64(signature)})
    return {"payloadType": PAYLOAD_TYPE, "payload": _b64(payload), "signatures": signatures}


@dataclass(frozen=True, slots=True)
class SignedReviewRecord:
    """One emitted record: the envelope plus the facts a list view needs."""

    record_id: str
    review_id: str
    revision_id: str
    payload_digest: str
    signing_state: str
    signing_mode: str
    key_id: str
    issued_at: str
    decision_kind: str
    envelope: Mapping[str, Any]
    statement: Mapping[str, Any] = field(default_factory=dict)

    @property
    def signed(self) -> bool:
        return self.signing_state == "signed"

    def summary(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "review_id": self.review_id,
            "revision_id": self.revision_id,
            "payload_digest": self.payload_digest,
            "signing_state": self.signing_state,
            "signing_mode": self.signing_mode,
            "key_id": self.key_id,
            "issued_at": self.issued_at,
            "decision_kind": self.decision_kind,
            # Display label only; verification is always a separate verdict.
            "label": signing_label(self.signing_state, self.signing_mode, self.key_id),
            **{key: value for key, value in summarize(self.statement).items() if key in _LIST_SUMMARY_KEYS},
        }


def signing_label(state: str, mode: str, key_id: str) -> str:
    """How every surface names a record's signing state; unsigned always says UNSIGNED."""

    if state != "signed":
        return "UNSIGNED record (no signature)"
    who = {"organization_key": "organization key", "user_key": "personal key", "sigstore": "Sigstore"}.get(mode, mode)
    return f"Signed with {who} {key_id}"


def record_id_for(payload_digest: str) -> str:
    return _RECORD_ID_PREFIX + payload_digest.removeprefix("sha256:")[:32]


def sign_review_record(
    context: RecordContext,
    decision: ReviewDecision,
    *,
    signer: RecordSigner | None,
    issued_at: str,
    persist: bool = True,
) -> SignedReviewRecord:
    """Build, sign (or explicitly leave unsigned) and persist one review record.

    The P10-LAND hook: call it after a landing decision with
    ``ReviewDecision(kind="landing", ...)`` and the composition's signer
    (``None`` produces an UNSIGNED record, labeled so inside the payload).
    ``context.readiness`` must be the readiness evaluated at *issued_at*.
    Raises :class:`RecordSigningUnavailable` when the signer cannot sign;
    nothing is persisted then.
    """

    identity = signer.identity if signer is not None else UNSIGNED
    statement = build_statement(context, decision, signing=identity, issued_at=issued_at)
    payload = canonical_json(statement)
    envelope = dsse_envelope(payload, signer)
    digest = sha256_digest(payload)
    record = SignedReviewRecord(
        record_id=record_id_for(digest),
        review_id=context.session.id,
        revision_id=context.revision.id,
        payload_digest=digest,
        signing_state="signed" if identity.signed else "unsigned",
        signing_mode=identity.mode,
        key_id=identity.key_id,
        issued_at=issued_at,
        decision_kind=decision.kind,
        envelope=envelope,
        statement=statement,
    )
    if persist:
        persist_record(context.store, record)
    return record


# --------------------------------------------------------------------------- #
# persistence (an existing relationship row per record; no new table)
# --------------------------------------------------------------------------- #


def persist_record(store: Any, record: SignedReviewRecord) -> None:
    """Store the record on its Change, keyed by payload digest (idempotent)."""

    from lemoncrow.pro.capabilities.review.session_models import ReviewRelationship

    store.upsert_relationship(
        ReviewRelationship(
            id="",
            review_id=record.review_id,
            kind=RECORD_RELATIONSHIP_KIND,
            relation="dsse_envelope",
            provider=record.signing_mode,
            external_id=record.payload_digest,
            title=signing_label(record.signing_state, record.signing_mode, record.key_id),
            status=record.signing_state,
            source="lemoncrow",
            metadata_json=json.dumps(
                {
                    "record_id": record.record_id,
                    "revision_id": record.revision_id,
                    "key_id": record.key_id,
                    "issued_at": record.issued_at,
                    "decision_kind": record.decision_kind,
                    "payload_digest": record.payload_digest,
                    "envelope": dict(record.envelope),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
    )


def decode_payload(envelope: Mapping[str, Any]) -> tuple[bytes, dict[str, Any]] | None:
    """The envelope's payload bytes and parsed Statement, or ``None`` when unreadable."""

    try:
        payload = base64.b64decode(str(envelope.get("payload") or ""), validate=True)
        statement = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return (payload, statement) if isinstance(statement, dict) else None


def record_from_relationship(row: Any) -> SignedReviewRecord | None:
    try:
        metadata = json.loads(str(getattr(row, "metadata_json", "") or "{}"))
    except ValueError:
        return None
    envelope = metadata.get("envelope") if isinstance(metadata, Mapping) else None
    if not isinstance(envelope, Mapping):
        return None
    decoded = decode_payload(envelope)
    statement = decoded[1] if decoded is not None else {}
    # The stored digest is recomputed from the stored bytes, never trusted.
    digest = sha256_digest(decoded[0]) if decoded is not None else str(metadata.get("payload_digest") or "")
    signed = bool(envelope.get("signatures"))
    return SignedReviewRecord(
        record_id=str(metadata.get("record_id") or record_id_for(digest)),
        review_id=str(getattr(row, "review_id", "") or ""),
        revision_id=str(metadata.get("revision_id") or ""),
        payload_digest=digest,
        signing_state="signed" if signed else "unsigned",
        signing_mode=str(getattr(row, "provider", "") or ("unsigned" if not signed else "")),
        key_id=str(metadata.get("key_id") or ""),
        issued_at=str(metadata.get("issued_at") or ""),
        decision_kind=str(metadata.get("decision_kind") or ""),
        envelope=dict(envelope),
        statement=statement,
    )


def list_records(store: Any, review_id: str) -> tuple[SignedReviewRecord, ...]:
    """Records of one Change, newest first."""

    rows = store.list_relationships(review_id, kind=RECORD_RELATIONSHIP_KIND)
    records = [record for row in rows if (record := record_from_relationship(row)) is not None]
    return tuple(sorted(records, key=lambda item: (item.issued_at, item.record_id), reverse=True))


def get_record(store: Any, review_id: str, record_id: str) -> SignedReviewRecord | None:
    return next((item for item in list_records(store, review_id) if item.record_id == record_id), None)


# --------------------------------------------------------------------------- #
# local: the reviewer's own key (explicit opt-in)
# --------------------------------------------------------------------------- #


def user_key_id(public_key: Ed25519PublicKey) -> str:
    from cryptography.hazmat.primitives import serialization

    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return "user-" + hashlib.sha256(raw).hexdigest()[:24]


def _inside_git_work_tree(path: Path) -> Path | None:
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            return parent
    return None


def refuse_key_location(path: Path, *, repo_root: Path | None = None) -> str:
    """Why a personal signing key may not live at *path* (``""`` when it may).

    A key inside any Git work tree (the Review's repository or another) could be
    committed with it, so it is refused outright.
    """

    resolved = path.expanduser().resolve()
    if repo_root is not None:
        root = repo_root.expanduser().resolve()
        if resolved == root or root in resolved.parents:
            return f"the signing key {resolved} is inside the repository under review; keep it outside any repository"
    work_tree = _inside_git_work_tree(resolved.parent)
    if work_tree is not None:
        return f"the signing key {resolved} is inside the Git work tree {work_tree}; keep it outside any repository"
    return ""


def generate_user_key(path: Path) -> str:
    """Create an owner-only Ed25519 key at *path*; returns its key id. Never overwrites."""

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    target = path.expanduser().resolve()
    refusal = refuse_key_location(target)
    if refusal:
        raise RecordSigningUnavailable(refusal, code="forbidden")
    if target.exists():
        raise RecordSigningUnavailable(
            f"{target} already exists; refusing to overwrite a signing key", code="forbidden"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    key = Ed25519PrivateKey.generate()
    encoded = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(encoded)
    os.chmod(target, 0o600)
    return user_key_id(key.public_key())


def load_user_key(path: Path, *, repo_root: Path | None = None) -> Ed25519PrivateKey:
    """Load the opted-in personal key; every unsafe condition refuses (never unsigned-as-signed)."""

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    target = path.expanduser()
    refusal = refuse_key_location(target, repo_root=repo_root)
    if refusal:
        raise RecordSigningUnavailable(refusal, code="forbidden")
    try:
        info = target.stat()
    except OSError as exc:
        raise RecordSigningUnavailable(f"the configured signing key {target} is not readable: {exc}") from exc
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RecordSigningUnavailable(
            f"the signing key {target} is readable by other users (mode {stat.S_IMODE(info.st_mode):04o}); "
            "chmod 600 it",
            code="forbidden",
        )
    try:
        key = serialization.load_pem_private_key(target.read_bytes(), password=None)
    except (ValueError, TypeError) as exc:
        raise RecordSigningUnavailable(f"the signing key {target} is not an unencrypted PEM private key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise RecordSigningUnavailable(f"the signing key {target} is not an Ed25519 key")
    return key


def configured_user_key_path(settings_root: Path) -> Path | None:
    from lemoncrow.core.settings import load_raw

    try:
        raw = load_raw(settings_root).get(USER_KEY_SETTING)
    except Exception:  # unreadable settings: not opted in, never a guessed key
        return None
    value = str(raw or "").strip()
    return Path(value).expanduser() if value else None


def public_key_entry(
    public_key: Any,
    *,
    key_id: str,
    status: str = "active",
    not_before: str = "",
    not_after: str = "",
    revoked_at: str = "",
    revocation_reason: str = "",
    issuer: str = "",
) -> dict[str, Any]:
    """One JWKS-style public key with its lifecycle (status, validity, revocation)."""

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    def b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")

    entry: dict[str, Any]
    if isinstance(public_key, Ed25519PublicKey):
        raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        entry = {"kty": "OKP", "crv": "Ed25519", "x": b64url(raw), "alg": "EdDSA"}
    elif isinstance(public_key, ec.EllipticCurvePublicKey) and isinstance(public_key.curve, ec.SECP256R1):
        numbers = public_key.public_numbers()
        entry = {
            "kty": "EC",
            "crv": "P-256",
            "x": b64url(numbers.x.to_bytes(32, "big")),
            "y": b64url(numbers.y.to_bytes(32, "big")),
            "alg": "ES256",
        }
    else:
        raise ValueError("review records are signed with Ed25519 or ECDSA P-256 keys only")
    entry.update(
        {
            "kid": key_id,
            "use": "sig",
            "status": status,
            "not_before": not_before,
            "not_after": not_after,
            "revoked_at": revoked_at,
            "revocation_reason": revocation_reason,
            "issuer": issuer,
        }
    )
    return entry


def key_set_document(entries: list[dict[str, Any]], *, issuer: str, generated_at: str = "") -> dict[str, Any]:
    return {
        "type": "lemoncrow.review-record-keys",
        "version": 1,
        "issuer": issuer,
        "generated_at": generated_at,
        "keys": sorted(entries, key=lambda row: str(row.get("kid") or "")),
    }


class LocalRecordSigning:
    """Local authority: the reviewer's opted-in personal key, else explicitly unsigned."""

    def __init__(
        self,
        *,
        settings_root: Callable[[], Path],
        repo_root: Path | None = None,
    ) -> None:
        self._settings_root = settings_root
        self._repo_root = repo_root

    def _key(self) -> tuple[Ed25519PrivateKey, str] | None:
        path = configured_user_key_path(self._settings_root())
        if path is None:
            return None
        key = load_user_key(path, repo_root=self._repo_root)
        return key, user_key_id(key.public_key())

    def status(self, session: ReviewSession) -> SigningStatus:
        try:
            loaded = self._key()
        except RecordSigningUnavailable as exc:
            return SigningStatus(mode="user_key", available=False, reason=exc.reason, code=exc.code)
        if loaded is None:
            return SigningStatus(
                mode="unsigned",
                available=False,
                code="not_configured",
                reason=(
                    f"No personal signing key is configured. Run `{USER_KEY_INIT_COMMAND}` to opt in; until then "
                    "records export explicitly labeled UNSIGNED."
                ),
            )
        _key, key_id = loaded
        return SigningStatus(
            mode="user_key", available=True, key_id=key_id, algorithm="Ed25519", issuer_kind="local", issuer_id=key_id
        )

    def signer(self, session: ReviewSession) -> RecordSigner:
        loaded = self._key()
        if loaded is None:
            raise RecordSigningUnavailable(
                f"no personal signing key is configured; run `{USER_KEY_INIT_COMMAND}` or export it unsigned"
            )
        key, key_id = loaded
        return Ed25519RecordSigner(key, key_id=key_id, mode="user_key", issuer_kind="local", issuer_id=key_id)

    def public_keys(self, session: ReviewSession) -> Mapping[str, Any]:
        try:
            loaded = self._key()
        except RecordSigningUnavailable:
            loaded = None
        if loaded is None:
            return key_set_document([], issuer="local")
        key, key_id = loaded
        return key_set_document([public_key_entry(key.public_key(), key_id=key_id, issuer=key_id)], issuer=key_id)

    def exporter(self, session: ReviewSession) -> tuple[str, str]:
        return session.reviewer_id or "local", "human"

    def issuer(self, session: ReviewSession) -> tuple[str, str]:
        loaded = None
        try:
            loaded = self._key()
        except RecordSigningUnavailable:
            loaded = None
        return "local", loaded[1] if loaded is not None else "local"

    def audit(self, session: ReviewSession, event: str, *, allowed: bool, detail: Mapping[str, str]) -> None:
        # Local history is the Review activity log the route writes; there is no
        # separate security audit sink on a reviewer's own machine.
        del session, event, allowed, detail


__all__ = [
    "SIGNATURE_ALGORITHMS",
    "USER_KEY_INIT_COMMAND",
    "USER_KEY_SETTING",
    "Ed25519RecordSigner",
    "LocalRecordSigning",
    "RecordSigner",
    "RecordSigningAuthority",
    "RecordSigningUnavailable",
    "SignedReviewRecord",
    "SigningStatus",
    "configured_user_key_path",
    "decode_payload",
    "dsse_envelope",
    "generate_user_key",
    "get_record",
    "key_set_document",
    "list_records",
    "load_user_key",
    "pae",
    "persist_record",
    "public_key_entry",
    "record_from_relationship",
    "record_id_for",
    "refuse_key_location",
    "sign_review_record",
    "signing_label",
    "user_key_id",
]
