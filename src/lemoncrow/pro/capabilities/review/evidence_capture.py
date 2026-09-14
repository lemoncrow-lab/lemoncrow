"""Author-side evidence capture that cannot advance a human review.

The coding agent may finish with screenshots, videos, Playwright traces or a
preview URL before a reviewer opens LemonCrow.  Like author rationale, those
artifacts are first written to an exact-session sidecar.  A later matching
ReviewRevision imports them into durable review evidence.

Matching is fail-closed: uncommitted work must have the same source fingerprint;
a committed review may match the exact HEAD sha.  A screenshot from revision N
therefore cannot silently become proof for revision N+1.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from lemoncrow.core.foundation.paths import confine_to_root, session_dir
from lemoncrow.infra.storage.ids import make_uuid7
from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore, new_evidence_id

_FILENAME = "review_evidence.jsonl"
_DIRNAME = "review_evidence"
_MAX_ENTRIES = 12
_MAX_BYTES = 100 * 1024 * 1024
_MAX_TITLE = 180
_KINDS = frozenset({"screenshot", "image", "video", "playwright_trace", "document", "live_preview"})


@dataclass(frozen=True)
class CapturedEvidence:
    id: str
    host: str
    session_id: str
    model: str
    repo_root: str
    kind: str
    title: str
    path: str
    sidecar_path: str
    url: str
    content_hash: str
    mime_type: str
    bytes: int
    head_sha: str
    source_fingerprint: str
    verification_status: str
    detail: str
    created_at: str
    staged_fingerprint: str = ""
    """The same code identity read through the index rather than the worktree.

    Defaulted because records written before this field existed simply do not
    match a staged revision -- which is the fail-closed answer, not a silent
    one-scheme comparison."""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _head_sha(repo_root: Path) -> str:
    try:
        import pygit2

        repo = pygit2.Repository(str(repo_root / ".git" if (repo_root / ".git").is_dir() else repo_root))
        return "" if repo.head_is_unborn else str(repo.head.target)
    except Exception:
        return ""


def _source_fingerprint(repo_root: Path, *, staged: bool = False) -> str:
    """Identity of the uncommitted code this evidence was captured against.

    Both schemes are stamped, because ``source_state`` builds per-path identity
    from the index for a staged range (``index:<oid>:<mode>``) and from the
    worktree otherwise (``file:<sha256>``), so the two hashes are structurally
    distinct even over identical bytes. Recording only the worktree one meant an
    agent that staged its work before capturing proof could never have that
    proof imported into a ``lc review --staged`` review.
    """

    try:
        from lemoncrow.pro.capabilities.review.gitdiff import resolve_rev_range, source_state

        rng = resolve_rev_range(repo_root, staged=staged, working_tree=not staged)
        return source_state(repo_root, rng).fingerprint
    except (OSError, RuntimeError, ValueError):
        return ""


def _kind_for(name: str, mime: str, raw: str) -> str:
    wanted = raw.strip().lower()
    if wanted in _KINDS:
        return wanted
    low = name.lower()
    if low.endswith("trace.zip"):
        return "playwright_trace"
    if mime.startswith("image/"):
        return "screenshot"
    if mime.startswith("video/"):
        return "video"
    return "document"


def _coerce(raw: Mapping[str, Any]) -> CapturedEvidence | None:
    try:
        record = CapturedEvidence(
            id=str(raw.get("id") or ""),
            host=str(raw.get("host") or ""),
            session_id=str(raw.get("session_id") or ""),
            model=str(raw.get("model") or ""),
            repo_root=str(raw.get("repo_root") or ""),
            kind=str(raw.get("kind") or ""),
            title=str(raw.get("title") or ""),
            path=str(raw.get("path") or ""),
            sidecar_path=str(raw.get("sidecar_path") or ""),
            url=str(raw.get("url") or ""),
            content_hash=str(raw.get("content_hash") or ""),
            mime_type=str(raw.get("mime_type") or ""),
            bytes=max(0, int(raw.get("bytes") or 0)),
            head_sha=str(raw.get("head_sha") or ""),
            source_fingerprint=str(raw.get("source_fingerprint") or ""),
            staged_fingerprint=str(raw.get("staged_fingerprint") or ""),
            verification_status=str(raw.get("verification_status") or "").upper(),
            detail=str(raw.get("detail") or ""),
            created_at=str(raw.get("created_at") or ""),
        )
    except (TypeError, ValueError):
        return None
    if not record.id or not record.host or not record.session_id or not record.repo_root or record.kind not in _KINDS:
        return None
    if not record.url and not record.sidecar_path:
        return None
    return record


def record_agent_evidence(
    store_root: Path,
    repo_root: Path,
    *,
    host: str,
    session_id: str,
    model: str = "",
    entries: Sequence[Mapping[str, Any]],
) -> tuple[CapturedEvidence, ...]:
    """Copy a bounded batch of author-produced proof into an exact-session sidecar."""

    if not host or not session_id:
        raise ValueError("review evidence requires an exact host session")
    if not entries:
        raise ValueError("at least one evidence entry is required")
    if len(entries) > _MAX_ENTRIES:
        raise ValueError(f"at most {_MAX_ENTRIES} evidence entries may be recorded at once")

    root = repo_root.resolve()
    now = datetime.now(UTC).isoformat()
    head_sha = _head_sha(root)
    source_fingerprint = _source_fingerprint(root)
    staged_fingerprint = _source_fingerprint(root, staged=True)
    session_root = session_dir(store_root, host, session_id)
    artifact_root = session_root / _DIRNAME
    artifact_root.mkdir(parents=True, exist_ok=True)
    out: list[CapturedEvidence] = []

    for raw in entries:
        file_raw = str(raw.get("file") or "").strip()
        url = str(raw.get("url") or "").strip()
        capture_url = bool(raw.get("capture"))
        if bool(file_raw) == bool(url):
            raise ValueError("each evidence entry needs exactly one of file or url")
        if url:
            parsed_url = urlparse(url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                raise ValueError("evidence URL must be an absolute http(s) URL")
        logical_path = str(raw.get("path") or "").strip()
        if logical_path:
            logical_target = confine_to_root(root / logical_path, root)
            logical_path = logical_target.relative_to(root).as_posix()

        evidence_id = "cap-" + str(make_uuid7())
        sidecar_path = ""
        digest = ""
        mime = ""
        size = 0
        display_name = url
        if file_raw:
            source = Path(file_raw)
            if not source.is_absolute():
                source = root / source
            source = confine_to_root(source, root)
            if not source.is_file():
                raise ValueError(f"evidence file is not readable: {file_raw!r}")
            size = source.stat().st_size
            if size > _MAX_BYTES:
                raise ValueError(f"evidence file exceeds {_MAX_BYTES // (1024 * 1024)} MiB: {file_raw!r}")
            payload = source.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            mime = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
            suffix = source.suffix[:16]
            target = artifact_root / f"{evidence_id}{suffix}"
            shutil.copyfile(source, target)
            sidecar_path = str(target.relative_to(session_root))
            display_name = source.name
        elif capture_url:
            parsed = urlparse(url)
            if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("automatic screenshot capture is limited to loopback preview URLs")
            chrome = shutil.which("google-chrome") or shutil.which("chromium") or shutil.which("chromium-browser")
            if not chrome:
                raise ValueError("automatic screenshot capture needs a local Chrome/Chromium executable")
            target = artifact_root / f"{evidence_id}.png"
            try:
                subprocess.run(
                    [
                        chrome,
                        "--headless=new",
                        "--disable-gpu",
                        "--no-first-run",
                        "--no-default-browser-check",
                        "--window-size=1440,1000",
                        f"--screenshot={target}",
                        url,
                    ],
                    cwd=root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=30,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise ValueError(f"could not capture preview screenshot: {exc}") from exc
            payload = target.read_bytes()
            size = len(payload)
            digest = hashlib.sha256(payload).hexdigest()
            mime = "image/png"
            sidecar_path = str(target.relative_to(session_root))
            display_name = "preview.png"
            # The captured pixels are the evidence. Keep the live URL separate
            # so opening the artifact later cannot silently show newer content.
            url = ""
        kind = _kind_for(display_name, mime, str(raw.get("kind") or ("live_preview" if url else "")))
        if capture_url:
            kind = "screenshot"
        title = str(raw.get("title") or display_name or kind.replace("_", " ").title()).strip()[:_MAX_TITLE]
        verification_status = str(raw.get("status") or raw.get("verification_status") or "").upper().strip()
        if verification_status not in {"", "PASS", "FAIL", "NOT_RUN", "UNKNOWN"}:
            raise ValueError("evidence status must be PASS, FAIL, NOT_RUN, UNKNOWN, or empty")
        detail = str(raw.get("detail") or "").strip()[:500]
        out.append(
            CapturedEvidence(
                id=evidence_id,
                host=host,
                session_id=session_id,
                model=model,
                repo_root=str(root),
                kind=kind,
                title=title,
                path=logical_path,
                sidecar_path=sidecar_path,
                url=url,
                content_hash=digest,
                mime_type=mime,
                bytes=size,
                head_sha=head_sha,
                source_fingerprint=source_fingerprint,
                staged_fingerprint=staged_fingerprint,
                verification_status=verification_status,
                detail=detail,
                created_at=now,
            )
        )

    sidecar = session_root / _FILENAME
    existing: set[str] = set()
    if sidecar.is_file():
        for line in sidecar.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                payload = json.loads(line)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("id"):
                existing.add(str(payload["id"]))
    with sidecar.open("a", encoding="utf-8") as fh:
        for record in out:
            if record.id not in existing:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    return tuple(out)


def _read_records(store_root: Path, revision: ReviewRevision) -> tuple[CapturedEvidence, ...]:
    if revision.provenance_certainty != "exact" or not revision.provenance_host or not revision.provenance_session_id:
        return ()
    root = session_dir(store_root, revision.provenance_host, revision.provenance_session_id)
    sidecar = root / _FILENAME
    if not sidecar.is_file():
        return ()
    out: list[CapturedEvidence] = []
    for line in sidecar.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            raw = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(raw, dict) and (record := _coerce(raw)) is not None:
            out.append(record)
    return tuple(out)


def _matches_revision(record: CapturedEvidence, revision: ReviewRevision) -> bool:
    if revision.range_mode == "commit_range":
        return bool(revision.head_sha and record.head_sha == revision.head_sha)
    # A staged revision fingerprints the index, so it is only ever comparable to
    # the index-side hash taken at capture time -- comparing it to the worktree
    # hash never matched, which silently dropped every agent artifact from a
    # ``lc review --staged`` review.
    captured = record.staged_fingerprint if revision.range_mode == "staged" else record.source_fingerprint
    return bool(revision.source_fingerprint and captured and captured == revision.source_fingerprint)


def import_agent_evidence(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    *,
    store_root: Path,
    repo_root: Path,
) -> int:
    """Import exact-session evidence whose captured code identity matches this revision."""

    records = [
        record
        for record in _read_records(store_root, revision)
        if record.repo_root == str(repo_root.resolve()) and _matches_revision(record, revision)
    ]
    if not records:
        return 0
    existing_refs = {item.source_ref for item in store.list_evidence(session.id) if item.source_ref}
    session_root = session_dir(store_root, revision.provenance_host, revision.provenance_session_id)
    imported = 0
    for record in records:
        source_ref = f"capture:{record.id}"
        if source_ref in existing_refs:
            continue
        evidence_id = new_evidence_id()
        artifact_path = ""
        digest = record.content_hash
        size = record.bytes
        if record.sidecar_path:
            try:
                source = confine_to_root(session_root / record.sidecar_path, session_root)
                payload = source.read_bytes()
            except (OSError, ValueError):
                continue
            if hashlib.sha256(payload).hexdigest() != record.content_hash:
                continue
            artifact_path, digest, size = store.write_evidence_artifact(session.id, revision.id, evidence_id, payload)
        store.add_evidence(
            ReviewEvidence(
                id=evidence_id,
                review_id=session.id,
                revision_id=revision.id,
                kind=record.kind,  # type: ignore[arg-type]
                title=record.title,
                path=record.path,
                artifact_path=artifact_path,
                url=record.url,
                content_hash=digest,
                mime_type=record.mime_type,
                bytes=size,
                source="agent",
                source_ref=source_ref,
                verification_status=record.verification_status,
                detail=record.detail,
                created_at=record.created_at,
            )
        )
        existing_refs.add(source_ref)
        imported += 1
    return imported


__all__ = ["CapturedEvidence", "import_agent_evidence", "record_agent_evidence"]
