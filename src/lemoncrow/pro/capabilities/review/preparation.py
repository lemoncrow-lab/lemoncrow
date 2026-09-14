"""Automatic review preparation: compress the work before the human arrives.

This module deliberately separates *preparation* from *judgment*.  It may
summarise deterministic facts already present in a ReviewPacket and project a
small number of LemonCrow annotations that explain why a file deserves
attention.  It never emits approval/request-change verdicts and it never makes
an LLM call.

The human-facing invariant is simple: opening a review should begin with
orientation and evidence, not with reconstructing what the tool already knows.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from lemoncrow.pro.capabilities.review.models import ReviewPacket
from lemoncrow.pro.capabilities.review.session_models import Annotation, ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.targets import ReviewTarget

_MAX_AUTO_ANNOTATIONS = 6
_MAX_BRIEF_START = 5
_HIGH_SIGNAL_TERMS = (
    "public",
    "caller",
    "outside",
    "untouched",
    "high-centrality",
    "migration",
    "config",
    "failed",
    "removed",
    "signature",
    "contract",
)


def _strong_reasons(reasons: Sequence[str]) -> tuple[str, ...]:
    material = tuple(str(reason).strip() for reason in reasons if str(reason).strip())
    return tuple(reason for reason in material if any(term in reason.lower() for term in _HIGH_SIGNAL_TERMS))


def project_lemoncrow_annotations(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    packet: ReviewPacket,
    *,
    new_blobs: Mapping[str, str],
) -> int:
    """Project a *small* set of deterministic attention facts into annotations.

    These are not an AI review and not another queue of findings.  The top
    attention-ranked files get at most one file-level LemonCrow annotation when
    there is concrete evidence worth carrying beside the diff.  Identity uses
    the file-unit content fingerprint so repeated capture of the same bytes is
    idempotent while a rewritten file naturally receives fresh evidence.
    """

    units = store.list_units(revision.id)
    file_units = {unit.path: unit for unit in units if unit.kind == "file"}
    existing = {
        annotation.source_id
        for annotation in store.list_annotations(session.id)
        if annotation.source == "lemoncrow" and annotation.source_id
    }
    impact_by_source: dict[str, list[Any]] = {}
    for site in packet.impact:
        if site.source_path:
            impact_by_source.setdefault(site.source_path, []).append(site)

    from lemoncrow.pro.capabilities.review.sources.local import annotate

    created = 0
    for entry in packet.order:
        if created >= _MAX_AUTO_ANNOTATIONS:
            break
        unit = file_units.get(entry.path)
        if unit is None:
            continue
        reasons = _strong_reasons(entry.reasons)
        sites = impact_by_source.get(entry.path, [])
        outside = [site for site in sites if not site.in_patch]
        # A low-signal ordering row should not become another thing the human
        # must read merely because an ordering score exists.
        if not reasons and not outside:
            continue
        if entry.rank > 10 and not outside:
            continue

        source_id = f"attention:{entry.path}:{unit.content_fingerprint}"
        if source_id in existing:
            continue

        lines: list[str] = []
        if reasons:
            lines.extend(f"• {reason}" for reason in reasons[:3])
        if outside:
            named = [str(site.path) for site in outside[:3] if str(site.path)]
            lines.append(
                f"• {len(outside)} observed impact site{'s' if len(outside) != 1 else ''} outside this patch"
                + (f": {', '.join(named)}" if named else "")
            )
        title = "Why this file deserves attention"
        evidence = [f"review-order:{entry.rank}"]
        evidence.extend(f"impact:{site.path}" for site in outside[:4])
        annotate(
            store,
            session,
            revision,
            path=entry.path,
            start_line=0,
            end_line=0,
            body="\n".join(lines),
            title=title,
            created_by="lemoncrow",
            created_by_actor="unknown",
            source="lemoncrow",
            source_id=source_id,
            evidence=evidence,
            new_text=new_blobs.get(entry.path),
        )
        existing.add(source_id)
        created += 1
    return created


def _brief_reason(row: Mapping[str, Any]) -> str:
    reasons = [str(reason).strip() for reason in (row.get("reasons") or ()) if str(reason).strip()]
    meaningful = [reason for reason in reasons if not (reason.startswith("+") and " -" in reason)]
    return (meaningful or reasons or [""])[0]


def build_review_brief(
    *,
    packet: Mapping[str, Any] | None,
    groups: Sequence[Mapping[str, Any]],
    chapters: Mapping[str, Sequence[Mapping[str, Any]]],
    annotations: Sequence[Annotation],
    artifacts: Sequence[Any],
    revision_id: str = "",
    stale_artifact_count: int = 0,
    targets: Sequence[ReviewTarget] = (),
) -> dict[str, Any]:
    """Build the deterministic orientation block shown before detailed review.

    *artifacts* may arrive in any order. The newest run of each verification
    title is picked by sorting on ``(created_at, id)`` here -- the key
    ``ReviewStore.list_evidence`` itself orders by -- rather than by trusting
    the caller to hand the rows over newest-first.
    """

    rows = [row for group in groups for row in (group.get("rows") or ()) if isinstance(row, Mapping)]
    unique_paths = {str(row.get("path") or "") for row in rows if str(row.get("path") or "")}
    needs = [
        row
        for group in groups
        if str(group.get("key") or "") in {"needs_attention", "changed_since_my_review"}
        for row in (group.get("rows") or ())
        if isinstance(row, Mapping)
    ]
    needs.sort(
        key=lambda row: (
            1 if int(row.get("attention_rank") or 0) == 0 else 0,
            int(row.get("attention_rank") or 0),
            str(row.get("path") or ""),
        )
    )

    intent = list(chapters.get("intent") or ())
    meaningful = [
        item
        for item in intent
        if str(item.get("label") or "") not in {"Other", "Tests", "Docs", "Generated / vendored"}
    ] or intent
    meaningful.sort(key=lambda item: (-int(item.get("file_count") or 0), str(item.get("label") or "")))
    themes = [str(item.get("label") or "") for item in meaningful[:5] if str(item.get("label") or "")]

    commit_chapters = [item for item in (chapters.get("commits") or ()) if isinstance(item, Mapping)]
    commit_chapters.sort(
        key=lambda item: (
            -int(item.get("file_count") or 0),
            -int(item.get("attention_count") or 0),
            1 if int(item.get("min_attention_rank") or 0) == 0 else 0,
            int(item.get("min_attention_rank") or 0),
            str(item.get("label") or ""),
        )
    )
    targets_by_path = Counter(target.path for target in targets)
    major_changes = []
    for item in commit_chapters[:4]:
        label = str(item.get("label") or "")
        if not label:
            continue
        chapter_rows = [row for row in (item.get("rows") or ()) if isinstance(row, Mapping)]
        chapter_rows.sort(
            key=lambda row: (
                1 if int(row.get("attention_rank") or 0) == 0 else 0,
                int(row.get("attention_rank") or 0),
                str(row.get("path") or ""),
            )
        )
        chapter_paths = {str(row.get("path") or "") for row in chapter_rows if str(row.get("path") or "")}
        major_changes.append(
            {
                "key": str(item.get("key") or ""),
                "label": label,
                "file_count": int(item.get("file_count") or 0),
                "target_count": sum(targets_by_path[path] for path in chapter_paths),
                "attention_count": int(item.get("attention_count") or 0),
                "first_path": str(chapter_rows[0].get("path") or "") if chapter_rows else "",
            }
        )
    verification_by_name: dict[str, str] = {}
    if packet is not None:
        for item in packet.get("evidence") or ():
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "")
            if name:
                verification_by_name[name] = str(item.get("status") or "UNKNOWN")
    # The caller passes only artifacts whose captured code identity is current.
    # Revision row identity is intentionally not re-checked here: an analysis
    # refresh may produce a different row for the same bytes in older stores.
    current_artifacts = len(artifacts)
    # One status per check title, and it has to be the *newest* run of that
    # check: letting an older row win reported a stale PASS over a live FAIL,
    # so a failing re-run of a previously passing check counted as nothing
    # outstanding. Newest is re-derived here from the same key the store sorts
    # on -- `list_evidence` is `ORDER BY created_at DESC, id DESC` -- rather
    # than assumed of the caller: `artifacts` is a `Sequence[Any]`, so nothing
    # in this signature can hold a caller to handing the rows over newest-first,
    # and a first-wins dedupe silently inverts the answer if one does not.
    # Rows whose key ties keep the order they arrived in.
    newest_first = sorted(
        artifacts,
        key=lambda item: (str(getattr(item, "created_at", "") or ""), str(getattr(item, "id", "") or "")),
        reverse=True,
    )
    artifact_statuses: dict[str, str] = {}
    for item in newest_first:
        status = str(getattr(item, "verification_status", "") or "").upper()
        if status not in {"PASS", "FAIL", "NOT_RUN", "UNKNOWN"}:
            continue
        name = str(getattr(item, "title", "") or "Verification")
        if name in artifact_statuses:
            continue
        artifact_statuses[name] = status
    verification_by_name.update(artifact_statuses)
    evidence_counts = Counter(verification_by_name.values())
    annotation_counts = Counter(
        annotation.source for annotation in annotations if annotation.state != "obsolete" and not annotation.parent_id
    )

    start = []
    for row in needs[:_MAX_BRIEF_START]:
        start.append(
            {
                "path": str(row.get("path") or ""),
                "rank": int(row.get("attention_rank") or 0),
                "reason": _brief_reason(row),
            }
        )

    theme_text = ", ".join(themes[:3])
    if unique_paths:
        summary = f"{len(unique_paths)} files"
        if theme_text:
            summary += f" across {theme_text}"
        if needs:
            summary += f"; {len(needs)} currently deserve focused attention."
        else:
            summary += "; no file is currently elevated above normal review."
    else:
        summary = "No changed files are available in this revision."

    return {
        "summary": summary,
        "themes": themes,
        "major_changes": major_changes,
        "review_first": start,
        "verification": {
            "pass": evidence_counts.get("PASS", 0),
            "fail": evidence_counts.get("FAIL", 0),
            "not_run": evidence_counts.get("NOT_RUN", 0),
            "unknown": evidence_counts.get("UNKNOWN", 0),
        },
        "annotations": {
            "human": annotation_counts.get("human", 0),
            "author": annotation_counts.get("author", 0),
            "lemoncrow": annotation_counts.get("lemoncrow", 0),
            "ai_review": annotation_counts.get("ai_review", 0),
        },
        "artifacts": {
            "current": current_artifacts,
            "stale": max(0, int(stale_artifact_count)),
        },
    }


__all__ = ["build_review_brief", "project_lemoncrow_annotations"]
