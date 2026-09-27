"""Snapshot-bound verification projection for Review.

Execution remains in existing local hooks, test runners, browsers and CI. Review
only normalizes immutable observations, keeping PASS/FAIL/NOT_RUN/UNKNOWN and
staleness explicit so a result for an older Snapshot cannot silently bless a
new one.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

from lemoncrow.pro.capabilities.review.session_models import ReviewEvidence, ReviewRevision
from lemoncrow.pro.capabilities.review.supply_chain import parse_evidence_metadata

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.browser_checks import BrowserPolicy

_STATES = frozenset({"PASS", "FAIL", "NOT_RUN", "UNKNOWN"})
_SUPPLY_CHAIN_KINDS = frozenset({"sbom", "provenance", "vulnerability_scan"})


def _status(value: object) -> str:
    text = str(value or "UNKNOWN").upper().strip()
    return text if text in _STATES else "UNKNOWN"


def _lane(source: str) -> str:
    normalized = source.lower().strip()
    if normalized == "ci":
        return "ci"
    if normalized in {"test", "agent", "human"}:
        return "local"
    return normalized or "unknown"


def _row(item: ReviewEvidence, *, number: int | None, state: str) -> dict[str, Any]:
    return {
        "name": item.title.strip() or "Verification",
        "kind": item.kind,
        "status": _status(item.verification_status),
        "state": state,
        "snapshot_id": item.revision_id,
        "snapshot_number": number,
        "source": item.source,
        "source_ref": item.source_ref,
        "lane": _lane(item.source),
        "path": item.path,
        "detail": item.detail,
        "observed_at": item.created_at,
        "artifact": (
            {
                "evidence_id": item.id,
                "kind": item.kind,
                "digest": item.content_hash,
                "mime_type": item.mime_type,
                "url": item.url,
            }
            if item.content_hash or item.url
            else None
        ),
    }


def build_verification_projection(
    *,
    packet: Mapping[str, Any] | None,
    evidence: Sequence[ReviewEvidence],
    revision: ReviewRevision,
    revisions: Sequence[ReviewRevision],
    browser_policy: BrowserPolicy | None = None,
    changed_paths: Sequence[str] = (),
    web_routes: Sequence[str] | None = None,
    read_artifact: Callable[[ReviewEvidence], bytes | None] | None = None,
) -> dict[str, Any]:
    from lemoncrow.pro.capabilities.review.browser_checks import build_browser_projection, is_browser_evidence

    def snapshot_number(item: ReviewRevision) -> int:
        return max(0, item.revision_number - 1)

    numbers = {item.id: snapshot_number(item) for item in revisions}
    browser_evidence = tuple(item for item in evidence if is_browser_evidence(item))
    evidence = tuple(item for item in evidence if not is_browser_evidence(item))
    current: dict[str, dict[str, Any]] = {}
    session: dict[str, dict[str, Any]] = {}

    # Packet evidence is what provenance recorded from the authoring session:
    # commands run at any point in that session (not bound to the captured
    # source) plus placeholders for checks it never ran. It is shown as
    # author-session context and never counts as an observation of this
    # Snapshot, in either direction.
    if packet is not None:
        for raw in packet.get("evidence") or ():
            if not isinstance(raw, Mapping):
                continue
            name = str(raw.get("name") or "").strip()
            if not name:
                continue
            source = str(raw.get("source") or "packet")
            session[name] = {
                "name": name,
                "kind": "verification",
                "status": _status(raw.get("status")),
                "state": "session",
                "snapshot_id": revision.id,
                "snapshot_number": snapshot_number(revision),
                "source": source,
                "source_ref": "",
                "lane": _lane(source),
                "path": str(raw.get("path") or ""),
                "detail": str(raw.get("detail") or ""),
                "observed_at": revision.created_at,
                "artifact": None,
            }

    # Newest exact-Snapshot observation for a name is the current result.
    exact = sorted(
        (item for item in evidence if item.revision_id == revision.id and item.verification_status),
        key=lambda item: (item.created_at, item.id),
        reverse=True,
    )
    for item in exact:
        name = item.title.strip() or "Verification"
        if name in current:
            continue
        current[name] = _row(item, number=numbers.get(item.revision_id), state="current")
    # A real observation of the same check supersedes the session's claim.
    session = {name: row for name, row in session.items() if name not in current}

    # Only the latest prior result per missing check is shown. It is explicitly
    # stale and never contributes to PASS/FAIL for the requested Snapshot.
    stale: dict[str, dict[str, Any]] = {}
    prior = sorted(
        (
            item
            for item in evidence
            if item.revision_id != revision.id
            and item.verification_status
            and (captured := next((row for row in revisions if row.id == item.revision_id), None)) is not None
            and captured.revision_number < revision.revision_number
        ),
        key=lambda item: (numbers.get(item.revision_id, 0), item.created_at, item.id),
        reverse=True,
    )
    for item in prior:
        name = item.title.strip() or "Verification"
        if name in current or name in stale:
            continue
        stale[name] = _row(item, number=numbers.get(item.revision_id), state="stale")

    def read_record(item: ReviewEvidence) -> Mapping[str, Any] | None:
        import json

        payload = read_artifact(item) if read_artifact is not None else None
        if payload is None:
            return None
        try:
            decoded = json.loads(payload)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return decoded if isinstance(decoded, Mapping) else None

    browser_checks, browser_summary = build_browser_projection(
        evidence=browser_evidence,
        revision=revision,
        revisions=revisions,
        policy=browser_policy,
        changed_paths=changed_paths,
        web_routes=web_routes,
        read_record=read_record,
    )

    # A waiver is a human's reasoned override of one check on this exact
    # Snapshot. It never changes the observed status; it only marks a row that
    # blocks without a current failure (a carried non-PASS or a current
    # NOT_RUN/UNKNOWN). A current FAIL outranks any waiver.
    waivers: dict[str, ReviewEvidence] = {}
    for item in sorted(
        (item for item in evidence if item.kind == "waiver" and item.revision_id == revision.id),
        key=lambda item: (item.created_at, item.id),
        reverse=True,
    ):
        waivers.setdefault(item.title.strip(), item)
    latest = all(item.revision_number <= revision.revision_number for item in revisions)
    for row in (*current.values(), *stale.values()):
        blocking = row["kind"] not in _SUPPLY_CHAIN_KINDS and (
            (row["state"] == "stale" and row["status"] != "PASS")
            or (row["state"] == "current" and row["status"] in {"NOT_RUN", "UNKNOWN"})
        )
        waiver = waivers.get(str(row["name"])) if blocking else None
        if waiver is not None:
            row["waiver"] = {
                "evidence_id": waiver.id,
                "reason": waiver.detail,
                "actor": waiver.source_ref,
                "snapshot_id": waiver.revision_id,
                "observed_at": waiver.created_at,
            }
        row["waivable"] = blocking and waiver is None and latest

    checks = [*current.values(), *session.values(), *stale.values(), *browser_checks]
    checks.sort(
        key=lambda row: (
            {"current": 0, "session": 1}.get(str(row["state"]), 2),
            {"FAIL": 0, "UNKNOWN": 1, "NOT_RUN": 2, "PASS": 3}.get(str(row["status"]), 4),
            str(row["name"]),
        )
    )
    statuses = [str(row["status"]) for row in checks if row["state"] == "current"]

    artifacts: list[dict[str, Any]] = []
    for item in evidence:
        number = numbers.get(item.revision_id)
        # ``numbers`` holds display numbers; compare like with like so an
        # artifact from a *newer* Snapshot never appears (as "stale") in an
        # older Snapshot's historical projection.
        if number is None or number > snapshot_number(revision):
            continue
        if not item.content_hash and item.kind not in _SUPPLY_CHAIN_KINDS:
            continue
        metadata = parse_evidence_metadata(item.detail) if item.kind in _SUPPLY_CHAIN_KINDS else {}
        artifacts.append(
            {
                "id": item.id,
                "kind": item.kind,
                "title": item.title,
                "state": "current" if item.revision_id == revision.id else "stale",
                "snapshot_id": item.revision_id,
                "snapshot_number": number,
                "digest": item.content_hash,
                "source": item.source,
                "source_ref": item.source_ref,
                "observed_at": item.created_at,
                "verification_status": _status(item.verification_status) if item.verification_status else "",
                "artifact_digest": str(metadata.get("artifact_digest") or ""),
                "standard": str(metadata.get("standard") or metadata.get("format") or ""),
                "format_version": str(metadata.get("format_version") or ""),
                "observation_source": str(metadata.get("observation_source") or item.source_ref or ""),
            }
        )
    artifacts.sort(
        key=lambda row: (
            1 if row["state"] == "stale" else 0,
            -int(row["snapshot_number"] or 0),
            str(row["kind"]),
            str(row["title"]),
        )
    )

    return {
        "review_id": revision.review_id,
        "snapshot_id": revision.id,
        "snapshot_number": snapshot_number(revision),
        "checks": checks,
        "summary": {
            "pass": statuses.count("PASS"),
            "fail": statuses.count("FAIL"),
            "not_run": statuses.count("NOT_RUN"),
            "unknown": statuses.count("UNKNOWN"),
            "stale": sum(1 for row in checks if row["state"] == "stale"),
        },
        "artifacts": artifacts,
        "browser": browser_summary,
    }


__all__ = ["build_verification_projection"]
