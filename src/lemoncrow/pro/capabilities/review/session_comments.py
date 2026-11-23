"""Project hook-completed replies into Review without an agent tool call.

Discussion and import checkpoints are Change-owned. The session ledger remains
the capture authority; import is atomic, content-bound, and idempotent.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from lemoncrow.core.capabilities.session_comment_capture import (
    agent_role,
    finding_location,
    message_edit_scopes,
    message_locations,
)
from lemoncrow.core.foundation.paths import confine_to_root
from lemoncrow.pro.capabilities.review.anchors import AnchorResolution, blob_sha
from lemoncrow.pro.capabilities.review.models import review_packet_from_dict
from lemoncrow.pro.capabilities.review.session_models import Annotation, AnnotationAnchor, ReviewRevision, ReviewSession
from lemoncrow.pro.capabilities.review.store import ReviewStore, utc_now


def _identity(host: str, sid: str, payload: dict[str, Any]) -> str:
    return str(
        payload.get("visible_turn_id")
        or hashlib.sha256(
            f"{host}\0{sid}\0{payload.get('source_id', '')}\0{payload.get('message', '')}".encode()
        ).hexdigest()
    )


_PROGRESS_PREFIXES = (
    "i'll ",
    "i will ",
    "i'm ",
    "let me ",
    "checking ",
    "i'll check",
    "i'm checking",
)
_GENERIC_HEADINGS = frozenset({"summary", "implementation", "result", "results", "changes", "update", "status", "done"})


def _headline(text: str) -> str:
    """A conservative title from visible agent prose; never infer hidden intent."""

    raw = text.strip()
    if not raw:
        return ""
    try:
        structured = json.loads(raw)
    except (TypeError, ValueError):
        structured = None
    if isinstance(structured, dict) and isinstance(structured.get("findings"), list):
        return ""

    fence = chr(96) * 3
    for source in raw.splitlines():
        line = source.strip()
        if not line or line.startswith(fence) or line.startswith("|"):
            continue
        line = re.sub(r"^(?:#{1,6}\s+|[-*+]\s+|\d+[.)]\s+)", "", line).strip()
        line = line.strip("*_ " + chr(96))
        if not line:
            continue
        lowered = line.casefold().replace(chr(8217), "'")
        if lowered.rstrip(":") in _GENERIC_HEADINGS:
            continue
        if any(lowered.startswith(prefix) for prefix in _PROGRESS_PREFIXES):
            continue
        if len(re.findall(r"[A-Za-z]", line)) < 4:
            continue
        sentence = re.split(r"(?<=[.!?])\s+", line, maxsplit=1)[0].strip()
        sentence = sentence.rstrip(".!?;: ")
        if len(sentence) <= 120:
            return sentence
        clipped = sentence[:117].rsplit(" ", 1)[0].rstrip()
        return (clipped or sentence[:117]).rstrip() + "…"
    return ""


def _brief_excerpt(text: str, *, limit: int = 800) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    clipped = clean[: limit - 1].rsplit(" ", 1)[0].rstrip()
    return (clipped or clean[: limit - 1]).rstrip() + "…"


def brief_agent_context(
    store: ReviewStore,
    session: ReviewSession,
    revision: ReviewRevision,
    *,
    historical: bool = False,
) -> tuple[dict[str, str], ...]:
    """Visible agent context up to one Snapshot, compacted for Brief.

    Every message keeps its exact owning Snapshot. Earlier author context may
    orient a later Snapshot, but future output is never visible historically.
    This does not turn prose into guidance or verification.
    """

    del historical  # Ownership filtering below is correct for current and historical views.
    visible_revision_ids = {
        item.id for item in store.list_revisions(session.id) if item.revision_number <= revision.revision_number
    }
    discussions = store.list_discussion_comments_at_revision(session.id, revision.id)
    annotations = tuple(
        item
        for item in store.list_annotations_at_revision(session.id, revision.id)
        if item.revision_id in visible_revision_ids
    )

    rows: list[dict[str, str]] = []
    represented_messages: set[str] = set()

    def append(item: Annotation, *, structured: bool, origin: str) -> None:
        if item.created_by_actor != "agent":
            return
        message_id = item.source_message_id or item.id
        if not structured and message_id in represented_messages:
            return
        title = item.title.strip() if structured else _headline(item.body)
        if not title:
            return
        if not structured and item.source == "author" and not item.agent_role.casefold().startswith("plan"):
            from lemoncrow.pro.capabilities.review.brief_summary import is_change_summary

            # A later progress update must not displace the completed change
            # summary from this producer. The exact messages stay in discussion.
            if not is_change_summary(title):
                return
        host = item.agent_host or item.created_by
        session_id = item.source_id
        producer = f"{host}:{session_id}" if host and session_id else (host or session_id or "agent")
        role = item.agent_role or "unknown"
        rows.append(
            {
                "id": item.id,
                "message_id": message_id,
                "producer": producer,
                "role": role,
                "source": item.source,
                "kind": (
                    "intent"
                    if item.source == "author" and role.casefold().startswith("plan") and not structured
                    else "context"
                ),
                "title": title,
                "body": _brief_excerpt(item.body),
                "path": item.anchor.path,
                "created_at": item.created_at,
                "origin": origin,
                "structured": "true" if structured else "false",
                "stale": "true" if item.revision_id != revision.id else "false",
            }
        )
        if not structured:
            represented_messages.add(message_id)

    # Exact retained message wins over a projected annotation copy of the same
    # prose. Structured findings remain individual rows because one source
    # message may legitimately contain several distinct observations.
    for item in discussions:
        append(item, structured=False, origin="discussion")
    for item in annotations:
        append(item, structured=bool(item.title), origin="annotation")

    rows.sort(key=lambda row: (row["created_at"], row["id"]))
    intents: dict[str, dict[str, str]] = {}
    latest_context: dict[tuple[str, str, str], dict[str, str]] = {}
    structured_context: list[dict[str, str]] = []
    for row in rows:
        if row["kind"] == "intent":
            intents[row["producer"]] = row
        elif row["structured"] == "true":
            structured_context.append(row)
        else:
            latest_context[(row["producer"], row["role"], row["source"])] = row

    compacted = [*intents.values(), *latest_context.values(), *structured_context]
    compacted.sort(key=lambda row: (row["created_at"], row["id"]))
    return tuple(compacted)


class SessionCommentIngestor:
    def __init__(self, store: ReviewStore, source_root: Path):
        self.store = store
        self.source_root = source_root
        self._paths: set[Path] = set()
        self._discovered = -1e9
        self._lock = threading.RLock()
        self._seen: dict[tuple[str, Path], tuple[int, int, str]] = {}

    def sync(self, session: ReviewSession, *, paths: list[Path] | None = None, force: bool = False) -> int:
        if session.status != "open":
            self._seen = {key: value for key, value in self._seen.items() if key[0] != session.id}
            return 0
        with self._lock:
            now = time.monotonic()
            if force or now - self._discovered > 30:
                self._paths = set((self.source_root / "sessions").glob("*/*/*/*/*/run.json"))
                self._seen = {key: value for key, value in self._seen.items() if key[1] in self._paths}
                self._discovered = now
            # session_dir pins new activity within this same three-day window.
            # Discover new ledgers on ordinary reads as well as filesystem events;
            # retained history above is never limited to the recent window.
            for offset in range(3):
                day = date.today() - timedelta(days=offset)
                recent = self.source_root / "sessions" / day.strftime("%Y/%m/%d")
                self._paths.update(recent.glob("*/*/run.json"))
            self._paths.update(path for path in paths or [] if path.is_relative_to(self.source_root / "sessions"))
            revision = self.store.latest_revision(session.id)
            revision_id = revision.id if revision else ""
            created = 0
            for path in sorted(self._paths):
                try:
                    stat = path.stat()
                    stamp = (stat.st_mtime_ns, stat.st_size, revision_id)
                    key = (session.id, path)
                    if not force and self._seen.get(key) == stamp:
                        continue
                    created += self._import(session, path)
                    self._seen[key] = stamp
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    # Cursor stays unchanged, so the next hook/read retries.
                    continue
            latest = self.store.latest_revision(session.id)
            current = self.store.get_session(session.id) or session
            generic_change_title = current.title.strip().casefold() in {"", "working tree", "staged changes"}
            if created or generic_change_title or (latest is not None and not latest.title):
                self._refresh_titles(current)
            return created

    def _refresh_titles(self, session: ReviewSession) -> None:
        """Derive stable Change/Snapshot headlines from captured author-visible text."""

        current = self.store.get_session(session.id) or session
        rows: list[tuple[str, str, Annotation, str]] = []
        seen: set[str] = set()
        discussion = tuple(self.store.list_discussion_comments(session.id))
        annotations = tuple(self.store.list_annotations(session.id))
        for origin, item in [
            *(("discussion", item) for item in discussion),
            *(("annotation", item) for item in annotations),
        ]:
            if item.created_by_actor != "agent" or item.source != "author":
                continue
            if origin == "annotation" and item.title:
                # Structured findings carry finding prose, not the source
                # message's own overall summary.
                continue
            headline = _headline(item.body)
            if not headline:
                continue
            identity = item.source_message_id or item.id
            if identity in seen:
                continue
            seen.add(identity)
            rows.append((item.created_at, item.id, item, headline))
        rows.sort(key=lambda row: (row[0], row[1]))
        if not rows:
            return

        if current.title.strip().casefold() in {"", "working tree", "staged changes"}:
            plan = next((row for row in rows if row[2].agent_role.casefold() == "plan"), None)
            attached = [row for row in rows if row[2].revision_id]
            chosen = plan or (attached[-1] if attached else None)
            if chosen is not None:
                self.store.update_session(current.id, title=chosen[3])

        by_revision: dict[str, list[tuple[str, str, Annotation, str]]] = {}
        for row in rows:
            if row[2].revision_id:
                by_revision.setdefault(row[2].revision_id, []).append(row)
        for revision in self.store.list_revisions(session.id):
            candidates = by_revision.get(revision.id)
            if not candidates:
                continue
            title = candidates[-1][3]
            if not revision.title:
                self.store.set_revision_title(session.id, revision.id, title)

    def _bound(
        self, session: ReviewSession, payload: dict[str, Any], run: dict[str, Any], host: str, sid: str, at: str
    ) -> bool:
        context = payload.get("review_context") or {}
        if not isinstance(context, dict):
            return False
        captured_review_id = str(context.get("review_id") or "").strip()
        if captured_review_id:
            if captured_review_id == session.id:
                return True
            # Review ids are store-local identities. A ledger can survive a
            # local-store migration (legacy global DB -> server-owned per-repo
            # DB) while retaining the old id. Only reject a mismatched id when
            # that Review actually exists in this store; otherwise fall back to
            # the stable repository/subject/worktree identity captured beside it.
            if self.store.get_session(captured_review_id) is not None:
                return False
        if context.get("repository_identity"):
            subject_kind = str(context.get("subject_kind") or "")
            subject_key = str(context.get("subject_key") or "")
            worktree_key = str(context.get("worktree_key") or "")
            with self.store._transaction() as conn:
                rows = conn.execute(
                    "SELECT review_id, created_at, updated_at, active FROM review_bindings WHERE repository_identity=? "
                    "AND subject_kind=? AND subject_key=? AND worktree_key=? ORDER BY created_at, id",
                    (
                        context["repository_identity"],
                        subject_kind,
                        subject_key,
                        worktree_key,
                    ),
                ).fetchall()
                if not rows and worktree_key:
                    # Server-owned Review stores namespace repository identity as
                    # ``local:<repo_id>`` while host hooks capture the Git
                    # repository identity. The exact worktree hash + subject is
                    # stable across that namespace boundary, and this query is
                    # already scoped to one ReviewStore.
                    rows = conn.execute(
                        "SELECT review_id, created_at, updated_at, active FROM review_bindings "
                        "WHERE subject_kind=? AND subject_key=? AND worktree_key=? ORDER BY created_at, id",
                        (subject_kind, subject_key, worktree_key),
                    ).fetchall()
            # Exact checkout/subject membership uses the binding's ownership era.
            # This also works when the hook's trace root has no Review database.
            moment = datetime.fromisoformat(at.replace("Z", "+00:00"))

            def timestamp(value: Any) -> datetime:
                return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

            owners = [
                row
                for row in rows
                if timestamp(row["created_at"]) <= moment and (row["active"] or moment < timestamp(row["updated_at"]))
            ]
            if owners:
                return len(owners) == 1 and owners[0]["review_id"] == session.id
            # A pre-Review plan belongs to the first subsequent binding, never a
            # later Change that happens to reuse this branch.
            future = [row for row in rows if timestamp(row["created_at"]) >= moment]
            return bool(future and future[0]["review_id"] == session.id)
        # Retained pre-feature VisibleTurn ledgers lack branch metadata: only
        # exact snapshot attribution can bind them, never workspace/time guesses.
        revision = self.store.latest_revision(session.id)
        if revision is None:
            return False
        if revision.provenance_certainty == "exact" and (revision.provenance_host, revision.provenance_session_id) == (
            host,
            sid,
        ):
            return True
        return any(
            item.certainty == "exact" and (item.host, item.session_id) == (host, sid)
            for item in self.store.list_revision_attributions(revision.id)
        )

    def _fallback_file(self, session: ReviewSession, revision: Any, raw_path: str = "") -> tuple[str, str, str | None]:
        """Keep an explicit file reference when its exact line cannot be proven."""

        if not raw_path:
            return "", "new", None

        from lemoncrow.pro.capabilities.review.sources.local import read_packet_json

        frozen = self.store.read_blob_artifact(session.id, revision.id) or {}
        packet = read_packet_json(self.store, revision) or {}
        rows = tuple(row for row in (packet.get("files") or []) if isinstance(row, dict))
        paths = {unit.path for unit in self.store.list_units(revision.id) if unit.kind == "file"}
        rows = tuple(row for row in rows if str(row.get("path") or "") in paths)
        relative = raw_path if raw_path in paths else ""
        if not relative:
            try:
                relative = (
                    confine_to_root(Path(session.repo_root) / raw_path, Path(session.repo_root))
                    .relative_to(Path(session.repo_root))
                    .as_posix()
                )
            except (ValueError, OSError):
                relative = ""
            if relative not in paths:
                relative = next(
                    (str(row.get("path") or "") for row in rows if str(row.get("old_path") or "") == relative),
                    "",
                )
        if not relative:
            return "", "new", None
        selected = next((row for row in rows if str(row.get("path") or "") == relative), None)
        side = "old" if str((selected or {}).get("status") or "") == "deleted" else "new"
        return relative, side, frozen.get(relative) if side == "new" else None

    def _project_later(
        self,
        session: ReviewSession,
        revision: Any,
        revisions: Any,
        annotation: Annotation,
    ) -> None:
        from lemoncrow.pro.capabilities.review.sources.local import (
            _new_side,
            _persist_anchor,
            read_packet_json,
            reanchor_annotation,
        )

        for later in revisions:
            if later.revision_number <= revision.revision_number:
                continue
            packet = read_packet_json(self.store, later)
            resolution = AnchorResolution(
                "unresolved",
                "unresolved",
                annotation.anchor,
                "Revision content unavailable for location verification",
            )
            if packet:
                later_blobs = self.store.read_blob_artifact(session.id, later.id) or {}
                later_packet = review_packet_from_dict(packet)
                _, new_text, _ = _new_side(later_packet, later_blobs, annotation.anchor.path)
                if new_text is None or blob_sha(new_text) != annotation.anchor.blob_sha:
                    resolution = AnchorResolution(
                        "stale",
                        "stale",
                        annotation.anchor,
                        f"{annotation.source} annotation is revision-bound; the underlying file content changed",
                    )
                else:
                    resolution = reanchor_annotation(annotation, later_packet, later_blobs)
            _persist_anchor(self.store, annotation, later, resolution)
            annotation = self.store.get_annotation(annotation.id) or annotation
            if annotation.state == "obsolete":
                break

    def _import(self, session: ReviewSession, path: Path) -> int:
        host, sid = path.parent.parent.name, path.parent.name
        stat = path.stat()
        cursor = self.store.session_comment_cursor(session.id, host, sid)
        signature = [stat.st_mtime_ns, stat.st_size]
        revisions = self.store.list_revisions(session.id)
        revision_id = revisions[-1].id if revisions else ""
        if (
            cursor.get("version") == 3
            and cursor.get("signature") == signature
            and (cursor.get("revision_id") == revision_id or not cursor.get("pending"))
        ):
            return 0
        run = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(run, dict):
            return 0
        events = run.get("events") or []
        if not isinstance(events, list):
            return 0
        offset = int(cursor.get("count") or 0) if cursor.get("version") == 3 else 0
        pending = set(cursor.get("pending") or []) if offset else set()
        if offset > len(events) or (
            offset
            and cursor.get("tail")
            != hashlib.sha256(json.dumps(events[offset - 1], sort_keys=True).encode()).hexdigest()
        ):
            offset = 0
            pending.clear()
        # Legacy Stop captures stored every visible block; retain only their last
        # assistant message, and only with exact attribution.
        legacy = max(
            (
                i
                for i, event in enumerate(events)
                if isinstance(event, dict)
                and (event.get("payload") or {}).get("event") == "VisibleTurn"
                and (event.get("payload") or {}).get("role") == "assistant"
            ),
            default=-1,
        )
        edit_scopes = message_edit_scopes(events)
        created = 0
        retry_unbound = False
        retry_draft: list[int] = []
        with self.store.atomic_review_transition():
            for i in sorted(pending | set(range(offset, len(events)))):
                event = events[i]
                if not isinstance(event, dict) or event.get("kind") != "agent_message":
                    continue
                payload = event.get("payload") or {}
                if not isinstance(payload, dict) or payload.get("role") != "assistant":
                    continue
                if not payload.get("completed") and i != legacy:
                    continue
                at = str(event.get("at") or utc_now())
                if not self._bound(session, payload, run, host, sid, at):
                    retry_unbound = True
                    continue
                body = str(payload.get("message") or "")
                if not body.strip():
                    continue
                context = payload.get("review_context") or {}
                captured_at = datetime.fromisoformat(str(context.get("captured_at") or at).replace("Z", "+00:00"))
                reported = payload.get("event") == "ReportedFindings"
                pinned = str(context.get("revision_id") or "")
                # A reported review names the Snapshot it reviewed (possibly an
                # older one an unchanged tree reused). A native session has no
                # frozen Review target: its output belongs to the working draft
                # until the first subsequent Snapshot.
                revision = next(
                    (
                        item
                        for item in revisions
                        if (
                            item.id == pinned
                            if pinned
                            else datetime.fromisoformat(item.created_at.replace("Z", "+00:00")) >= captured_at
                        )
                    ),
                    None,
                )
                if revision is None:
                    retry_draft.append(i)
                role = agent_role(payload.get("agent_role"))
                identity = _identity(host, sid, payload)
                producer_agent_model = str(payload.get("model") or "")
                producer_parent_session_id = str(payload.get("parent_session_id") or run.get("parent_session_id") or "")
                producer_source_message_id = str(payload.get("source_id") or identity)
                base = Annotation(
                    id="dsc-" + hashlib.sha256(f"{session.id}\0{host}\0{sid}\0{identity}".encode()).hexdigest()[:32],
                    review_id=session.id,
                    revision_id="",
                    anchor=AnnotationAnchor("", "new", 0, 0),
                    body=body,
                    created_by=host,
                    created_by_actor="agent",
                    source="ai_review" if role.casefold() == "review" else "author",
                    source_id=sid,
                    comment_scope="change",
                    created_at=at,
                    updated_at=at,
                    anchor_method="unresolved",
                    anchor_detail="Draft discussion; awaiting revision",
                    agent_role=role,
                    agent_host=host,
                    agent_model=producer_agent_model,
                    parent_session_id=producer_parent_session_id,
                    source_message_id=producer_source_message_id,
                )
                # Version 2 guessed a file for every unlocated message. Retire
                # that false annotation, retaining its row/history and exact prose.
                guessed = self.store.get_annotation(base.id.replace("dsc-", "ann-", 1))
                if (
                    guessed is not None
                    and guessed.created_by_actor == "agent"
                    and guessed.source_message_id == producer_source_message_id
                    and guessed.anchor.start_line == 0
                    and not guessed.title
                    and guessed.state != "obsolete"
                ):
                    self.store.update_annotation(
                        guessed.id,
                        state="obsolete",
                        anchor_detail="Guessed file association retired; source message reimported with its own edit scope",
                    )
                findings = (
                    [item for item in payload.get("findings") or [] if isinstance(item, dict)]
                    if reported
                    else message_locations(body)
                )
                if not findings and not reported:
                    # Retained ledgers can predate edited_paths. Both routes use
                    # this output's interval, including boundaries with no edits.
                    edited_paths = context.get("edited_paths", edit_scopes.get(i, ()))
                    if isinstance(edited_paths, (list, tuple)):
                        findings = [
                            {"path": path, "body": body, "_message_reference": True}
                            for path in edited_paths
                            if isinstance(path, str) and path
                        ]
                if not findings:
                    # Visible prose without a location belongs to the Change.
                    # Diff size is not evidence that an agent discussed a file.
                    self.store.add_discussion_comment(base)
                    if revision is not None:
                        self.store.attach_discussion_comment(session.id, base.id, revision.id)
                    created += 1
                    continue
                # Preserve the exact verdict/summary alongside the individual findings.
                self.store.add_discussion_comment(
                    replace(base, title="" if findings[0].get("_message_reference") else "Agent summary")
                )
                created += 1
                if revision is None:
                    continue
                self.store.attach_discussion_comment(session.id, base.id, revision.id)
                frozen = self.store.read_blob_artifact(session.id, revision.id)
                for number, finding in enumerate(findings):
                    text = str(
                        finding.get("body") or finding.get("message") or finding.get("description") or ""
                    ).strip()
                    if not text:
                        continue
                    title = str(finding.get("title") or "")
                    key = str(finding.get("key") or "") if reported else ""
                    finding_id = "ann-" + (
                        hashlib.sha256(f"{session.id}\0{host}\0{sid}\0reported\0{key}".encode()).hexdigest()[:32]
                        if key
                        else hashlib.sha256(f"{base.id}\0{role}\0{number}".encode()).hexdigest()[:32]
                    )
                    if self.store.get_annotation(finding_id) or (reported and finding.get("outcome") == "fixed"):
                        # A re-report (``--fix`` outcomes) never duplicates its finding
                        # and never judges it: only the person marks an AI finding
                        # fixed, with evidence. The outcome is in the summary.
                        if finding.get("_message_reference"):
                            self.store.delete_discussion_comment(session.id, base.id)
                        continue
                    raw_path, line, end = finding_location(finding)
                    context = payload.get("review_context") or {}
                    targets = context.get("targets") or {}
                    relative = raw_path if raw_path in targets else ""
                    workspace = str(context.get("workspace") or "").strip()
                    if raw_path and not relative and workspace:
                        try:
                            workspace_root = Path(workspace).expanduser().resolve()
                            candidate = Path(raw_path)
                            target = candidate if candidate.is_absolute() else workspace_root / candidate
                            relative = confine_to_root(target, workspace_root).relative_to(workspace_root).as_posix()
                        except (ValueError, OSError):
                            relative = ""
                    if raw_path and not relative:
                        try:
                            relative = (
                                confine_to_root(Path(session.repo_root) / raw_path, Path(session.repo_root))
                                .relative_to(Path(session.repo_root))
                                .as_posix()
                            )
                        except (ValueError, OSError):
                            relative = ""
                    content = (frozen or {}).get(relative)
                    exact = (
                        content is not None
                        and targets.get(relative) == blob_sha(content)
                        and line > 0
                        and end <= len(content.splitlines())
                    )
                    if exact and revision:
                        from lemoncrow.pro.capabilities.review.sources.local import annotate

                        annotation = annotate(
                            self.store,
                            session,
                            revision,
                            path=relative,
                            start_line=line,
                            end_line=end,
                            body=text,
                            title=title,
                            source=base.source,
                            source_id=sid,
                            created_by=host,
                            created_by_actor="agent",
                            new_text=content,
                            annotation_id=finding_id,
                            created_at=at,
                            agent_role=role,
                            agent_host=host,
                            agent_model=producer_agent_model,
                            parent_session_id=producer_parent_session_id,
                            source_message_id=producer_source_message_id,
                        )
                        # Late discovery still owns the first subsequent Snapshot;
                        # project through later captures using the existing ladder.
                        from lemoncrow.pro.capabilities.review.sources.local import (
                            _new_side,
                            _persist_anchor,
                            read_packet_json,
                            reanchor_annotation,
                        )

                        for later in revisions:
                            if later.revision_number <= revision.revision_number:
                                continue
                            packet = read_packet_json(self.store, later)
                            resolution = AnchorResolution(
                                "unresolved",
                                "unresolved",
                                annotation.anchor,
                                "Revision content unavailable for location verification",
                            )
                            if packet:
                                later_blobs = self.store.read_blob_artifact(session.id, later.id) or {}
                                later_packet = review_packet_from_dict(packet)
                                _, new_text, _ = _new_side(later_packet, later_blobs, annotation.anchor.path)
                                if new_text is None or blob_sha(new_text) != annotation.anchor.blob_sha:
                                    resolution = AnchorResolution(
                                        "stale",
                                        "stale",
                                        annotation.anchor,
                                        f"{annotation.source} annotation is revision-bound; the underlying file content changed",
                                    )
                                else:
                                    resolution = reanchor_annotation(annotation, later_packet, later_blobs)
                            _persist_anchor(self.store, annotation, later, resolution)
                            annotation = self.store.get_annotation(annotation.id) or annotation
                            if annotation.state == "obsolete":
                                break
                    else:
                        fallback_path, fallback_side, fallback_text = self._fallback_file(
                            session, revision, relative or raw_path
                        )
                        if fallback_path:
                            from lemoncrow.pro.capabilities.review.sources.local import annotate

                            annotation = annotate(
                                self.store,
                                session,
                                revision,
                                path=fallback_path,
                                start_line=0,
                                end_line=0,
                                body=text,
                                title=title,
                                source=base.source,
                                source_id=sid,
                                created_by=host,
                                created_by_actor="agent",
                                side=fallback_side,
                                new_text=fallback_text,
                                annotation_id=finding_id,
                                created_at=at,
                                agent_role=role,
                                agent_host=host,
                                agent_model=producer_agent_model,
                                parent_session_id=producer_parent_session_id,
                                source_message_id=producer_source_message_id,
                            )
                            self._project_later(session, revision, revisions, annotation)
                        elif not finding.get("_message_reference"):
                            self.store.add_discussion_comment(
                                replace(
                                    base,
                                    id=finding_id.replace("ann-", "dsc-"),
                                    body=text,
                                    title=title,
                                    revision_id=revision.id,
                                    anchor_detail="Finding location could not be proven against this revision",
                                )
                            )
                    if finding.get("_message_reference") and self.store.get_annotation(finding_id):
                        self.store.delete_discussion_comment(session.id, base.id)
                    created += 1
            # Unbound candidates must be reconsidered when a Change is captured,
            # even if the ledger did not change. Existing IDs prevent duplication.
            if not retry_unbound:
                self.store.set_session_comment_cursor(
                    session.id,
                    host,
                    sid,
                    {
                        "version": 3,
                        "signature": signature,
                        "revision_id": revision_id,
                        "pending": retry_draft,
                        "count": len(events),
                        "tail": (
                            hashlib.sha256(json.dumps(events[-1], sort_keys=True).encode()).hexdigest()
                            if events
                            else ""
                        ),
                    },
                )
        return created


def ingestor_for(store: ReviewStore, source_root: Path) -> SessionCommentIngestor:
    # Lifecycle follows the store instance; no process-global registry or worker.
    # ReviewStore declares this cache in __init__ so mypyc-native instances never
    # rely on a dynamic __dict__.
    instances = cast(dict[str, SessionCommentIngestor], store._session_comment_ingestors)
    key = str(source_root.resolve())
    if key not in instances:
        instances[key] = SessionCommentIngestor(store, source_root)
    return instances[key]
