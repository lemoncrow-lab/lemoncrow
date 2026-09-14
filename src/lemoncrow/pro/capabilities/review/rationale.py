"""Author rationale captured while the authoring agent still knows *why*.

Rationale is authoring provenance first and review state second.  The MCP tool
writes a small, exact-session sidecar bound to the file content hash; capturing
it never creates or advances a ReviewRevision.  When a human later captures a
revision, :func:`import_author_rationales` projects only records whose repository,
path and blob hash match that revision into ordinary ``source='author'`` review
annotations.

That separation preserves the review workspace's load-bearing rule: an agent may
finish and explain work while a reviewer is reading, but it cannot move the
review frontier underneath them.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lemoncrow.core.foundation.paths import confine_to_root, session_dir
from lemoncrow.pro.capabilities.review.anchors import blob_sha
from lemoncrow.pro.capabilities.review.session_models import ReviewRevision, ReviewSession, ReviewUnit
from lemoncrow.pro.capabilities.review.store import ReviewStore

_FILENAME = "review_rationale.jsonl"
_MAX_ENTRIES_PER_CALL = 8
_MAX_TITLE = 160
_MAX_BODY = 1600
_MAX_EVIDENCE = 8
_MAX_SIDECARS = 256


@dataclass(frozen=True)
class AuthorRationale:
    id: str
    host: str
    session_id: str
    model: str
    repo_root: str
    path: str
    blob_sha: str
    title: str
    body: str
    line: int = 0
    end_line: int = 0
    symbol: str = ""
    evidence: tuple[str, ...] = ()
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "host": self.host,
            "session_id": self.session_id,
            "model": self.model,
            "repo_root": self.repo_root,
            "path": self.path,
            "blob_sha": self.blob_sha,
            "title": self.title,
            "body": self.body,
            "line": self.line,
            "end_line": self.end_line,
            "symbol": self.symbol,
            "evidence": list(self.evidence),
            "created_at": self.created_at,
        }


def _record_id(
    *, host: str, session_id: str, path: str, blob: str, title: str, body: str, line: int, symbol: str
) -> str:
    raw = "\0".join((host, session_id, path, blob, title, body, str(line), symbol))
    return "rat-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _coerce_record(raw: Mapping[str, Any]) -> AuthorRationale | None:
    try:
        record = AuthorRationale(
            id=str(raw.get("id") or ""),
            host=str(raw.get("host") or ""),
            session_id=str(raw.get("session_id") or ""),
            model=str(raw.get("model") or ""),
            repo_root=str(raw.get("repo_root") or ""),
            path=str(raw.get("path") or ""),
            blob_sha=str(raw.get("blob_sha") or ""),
            title=str(raw.get("title") or ""),
            body=str(raw.get("body") or ""),
            line=max(0, int(raw.get("line") or 0)),
            end_line=max(0, int(raw.get("end_line") or 0)),
            symbol=str(raw.get("symbol") or ""),
            evidence=tuple(str(item) for item in (raw.get("evidence") or ()) if str(item)),
            created_at=str(raw.get("created_at") or ""),
        )
    except (TypeError, ValueError):
        return None
    if not all(
        (record.id, record.host, record.session_id, record.repo_root, record.path, record.blob_sha, record.body)
    ):
        return None
    return record


def record_author_rationales(
    store_root: Path,
    repo_root: Path,
    *,
    host: str,
    session_id: str,
    model: str = "",
    entries: Sequence[Mapping[str, Any]],
) -> tuple[AuthorRationale, ...]:
    """Append a bounded batch of exact-session rationale records.

    Each record is bound to the file bytes present *now*.  A later review imports
    it only if those bytes are still the reviewed new side, so an explanation for
    revision N cannot silently describe revision N+1.
    """

    if not host or not session_id:
        raise ValueError("author rationale requires an exact host session")
    if not entries:
        raise ValueError("at least one rationale entry is required")
    if len(entries) > _MAX_ENTRIES_PER_CALL:
        raise ValueError(f"at most {_MAX_ENTRIES_PER_CALL} rationale entries may be recorded at once")

    root = repo_root.resolve()
    out: list[AuthorRationale] = []
    now = datetime.now(UTC).isoformat()
    for raw in entries:
        path_raw = str(raw.get("path") or "").strip()
        body = str(raw.get("body") or "").strip()[:_MAX_BODY]
        if not path_raw or not body:
            raise ValueError("each rationale needs path and body")
        target = confine_to_root(root / path_raw, root)
        if not target.is_file():
            raise ValueError(f"rationale path is not a readable file: {path_raw!r}")
        rel = target.relative_to(root).as_posix()
        text = target.read_text(encoding="utf-8", errors="replace")
        title = str(raw.get("title") or "").strip()[:_MAX_TITLE]
        symbol = str(raw.get("symbol") or "").strip()
        line = max(0, int(raw.get("line") or 0))
        total_lines = len(text.splitlines())
        if line > total_lines:
            raise ValueError(f"{rel} has {total_lines} line(s); rationale line {line} is past the end")
        end_line = max(line, int(raw.get("end_line") or line)) if line else 0
        if end_line > total_lines:
            raise ValueError(f"{rel} has {total_lines} line(s); rationale end_line {end_line} is past the end")
        raw_evidence = raw.get("evidence") or ()
        if not isinstance(raw_evidence, (list, tuple)):
            raise ValueError("rationale evidence must be a list of strings")
        evidence = tuple(str(item)[:240] for item in raw_evidence[:_MAX_EVIDENCE] if str(item))
        digest = blob_sha(text)
        out.append(
            AuthorRationale(
                id=_record_id(
                    host=host,
                    session_id=session_id,
                    path=rel,
                    blob=digest,
                    title=title,
                    body=body,
                    line=line,
                    symbol=symbol,
                ),
                host=host,
                session_id=session_id,
                model=model,
                repo_root=str(root),
                path=rel,
                blob_sha=digest,
                title=title,
                body=body,
                line=line,
                end_line=end_line,
                symbol=symbol,
                evidence=evidence,
                created_at=now,
            )
        )

    path = session_dir(store_root, host, session_id) / _FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                payload = json.loads(raw_line)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("id"):
                existing.add(str(payload["id"]))
    new = [record for record in out if record.id not in existing]
    if new:
        with path.open("a", encoding="utf-8") as fh:
            for record in new:
                fh.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
    return tuple(out)


def _read_sidecar(path: Path) -> tuple[AuthorRationale, ...]:
    if not path.is_file():
        return ()
    out: list[AuthorRationale] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ()
    for line in lines:
        try:
            raw = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(raw, dict) and (record := _coerce_record(raw)) is not None:
            out.append(record)
    return tuple(out)


def _candidate_records(store_root: Path, revision: ReviewRevision) -> tuple[AuthorRationale, ...]:
    """Exact provenance first; otherwise bounded content-bound candidates."""

    if revision.provenance_certainty == "exact" and revision.provenance_host and revision.provenance_session_id:
        exact = session_dir(store_root, revision.provenance_host, revision.provenance_session_id) / _FILENAME
        return _read_sidecar(exact)

    root = Path(store_root) / "sessions"
    try:
        paths = list(root.glob("*/*/*/*/*/" + _FILENAME))
        paths.sort(key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        return ()
    out: list[AuthorRationale] = []
    for path in paths[:_MAX_SIDECARS]:
        out.extend(_read_sidecar(path))
    return tuple(out)


def _target_lines(record: AuthorRationale, units: Sequence[ReviewUnit]) -> tuple[int, int]:
    if record.line > 0:
        return record.line, max(record.line, record.end_line or record.line)
    if not record.symbol:
        return 0, 0
    candidates = [
        unit
        for unit in units
        if unit.kind == "symbol"
        and unit.path == record.path
        and (unit.symbol == record.symbol or unit.symbol.endswith("." + record.symbol))
    ]
    if len(candidates) != 1:
        return 0, 0
    return candidates[0].start_line, max(candidates[0].start_line, candidates[0].end_line)


def import_author_rationales(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    *,
    store_root: Path,
    repo_root: Path,
    new_blobs: Mapping[str, str],
) -> int:
    """Project safe author-sidecar records into this captured review revision.

    With exact revision provenance all matching records from that session are
    eligible. Without exact provenance, a ``(path, blob_sha)`` match is accepted
    only when it identifies one author session; two sessions that produced the
    same file bytes are ambiguous and neither gets attributed.
    """

    records = [
        record
        for record in _candidate_records(store_root, revision)
        if record.repo_root == str(repo_root.resolve())
        and record.path in new_blobs
        and blob_sha(new_blobs[record.path]) == record.blob_sha
    ]
    if not records:
        return 0

    exact = revision.provenance_certainty == "exact" and bool(revision.provenance_session_id)
    if not exact:
        sessions_by_content: dict[tuple[str, str], set[tuple[str, str]]] = defaultdict(set)
        for record in records:
            sessions_by_content[(record.path, record.blob_sha)].add((record.host, record.session_id))
        records = [record for record in records if len(sessions_by_content[(record.path, record.blob_sha)]) == 1]

    existing_ids = {
        evidence.removeprefix("rationale:")
        for annotation in store.list_annotations(session.id)
        if annotation.source == "author"
        for evidence in annotation.evidence
        if evidence.startswith("rationale:")
    }
    units = store.list_units(revision.id)
    imported = 0

    # Imported lazily to avoid a module cycle: sources.local calls us after it
    # has persisted the revision; annotate itself is the single anchoring door.
    from lemoncrow.pro.capabilities.review.sources.local import annotate

    for record in records:
        if record.id in existing_ids:
            continue
        start, end = _target_lines(record, units)
        evidence = [f"rationale:{record.id}", f"session:{record.session_id}", f"host:{record.host}"]
        if record.model:
            evidence.append(f"model:{record.model}")
        evidence.extend(record.evidence)
        annotate(
            store,
            session,
            revision,
            path=record.path,
            start_line=start,
            end_line=end,
            body=record.body,
            title=record.title,
            created_by=record.host,
            created_by_actor="agent",
            source="author",
            source_id=record.session_id,
            evidence=evidence,
            new_text=new_blobs.get(record.path),
        )
        imported += 1
    return imported
