"""``lemoncrow_reviews.db`` -- the durable half of review.

The browser pane, the terminal viewer and the agent surface are all
disposable; this file is not. Everything a reviewer builds up by hand -- what
they marked reviewed, against which content, what they commented on and where
it was anchored -- lives here and must survive the process that produced it,
the next agent rewrite, and the next release.

Shape and placement follow the house conventions rather than inventing a
parallel storage stack:

* One physical SQLite file per concern (``infra.storage.bundle``), named
  ``lemoncrow_<concern>.db``. ``.sqlite`` is reserved for the per-workspace
  code-intel databases, so it is not used here.
* :class:`SqliteTableStore` owns connections, WAL, ``foreign_keys=ON``, the
  120s ``busy_timeout`` and the migration runner. This module adds a schema and
  a versioning policy, nothing else.
* Not a seventh ``StoreBundle`` member: ``StoreBundle`` is a fixed
  six-attribute dataclass whose ``init()``/``health_check()`` enumerate all six
  by name. Like ``session_recall``'s ``recall.db``, this store is constructed
  at its call site and calls its own :meth:`init` on first use. It *is*
  registered in ``gateway.cli.commands.db.SPLIT_DB_NAMES`` so ``lc db vacuum``
  reaches it.

Versioning, which is net-new for this repository (see :meth:`_check_version`):
forward changes go through the dormant ``MIGRATIONS`` mechanism, and a
database written by a *newer* build is refused outright instead of being
written into blindly. Review state is not a rebuildable cache -- a mark
mangled by a build that did not understand its schema is a mark that quietly
lies about what a human looked at.

Concurrency: WAL plus the inherited ``busy_timeout`` is the only guard; there
is no advisory lock on any ``SqliteTableStore``. One instance is **not** safe
to share across threads (``_connection`` is a single slot and ``_batch_depth``
a plain int), so a long-lived service constructs one store per request rather
than holding one open.
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, ClassVar, Protocol, cast

from lemoncrow.core.foundation.paths import (
    DEFAULT_STORE_DIRNAME,
    confine_to_root,
    ensure_dir_gitignore,
    safe_segment,
)
from lemoncrow.core.foundation.sqlite_base import SqliteTableStore
from lemoncrow.infra.storage.ids import make_uuid7
from lemoncrow.pro.capabilities.review.session_models import (
    ANCHOR_METHODS,
    REVIEW_CHANGE_PROPOSAL_STATES,
    REVIEW_OUTCOME_KINDS,
    SESSION_SCHEMA_VERSION,
    ActorType,
    AnchorMethod,
    AnchorMove,
    Annotation,
    AnnotationAnchor,
    AnnotationVersion,
    AnnotationVersionKind,
    DeliveryRecord,
    DiscardedMark,
    ReviewActivityEvent,
    ReviewActivityKind,
    ReviewChangeProposal,
    ReviewEvidence,
    ReviewMark,
    ReviewMarkEvent,
    ReviewMarkEventKind,
    ReviewOutcome,
    ReviewOutcomeKind,
    ReviewRevision,
    ReviewSession,
    ReviewUnit,
    validate_annotation_semantics,
)

DB_NAME = "lemoncrow_reviews.db"

# Packet artifacts live under the store's existing ``review/`` directory (the
# static HTML report already writes there) rather than a second, adjacent
# ``review-artifacts/`` tree. One review directory, not two.
ARTIFACT_DIRNAME = "artifacts"
ARTIFACT_FILENAME = "packet.json.gz"
BLOB_ARTIFACT_FILENAME = "blobs.json.gz"
MEDIA_ARTIFACT_FILENAME = "media.zip"
SUBMODULE_ARTIFACT_FILENAME = "submodules.json.gz"
SOURCE_TREE_ARTIFACT_FILENAME = "source-tree.json.gz"
OLD_SOURCE_TREE_ARTIFACT_FILENAME = "source-tree-old.json.gz"


_SESSION_REF_PREFIX = "r/"
_REVISION_REF_PREFIX = "rr/"
_META_IDENTITY_VERSION_KEY = "review_identity_version"
_META_IDENTITY_MAP_KEY = "review_identity_migration_map"
_ANNOTATION_ID_PREFIX = "ann"
_ANCHOR_EVENT_ID_PREFIX = "ane"
_DELIVERY_ID_PREFIX = "dlv"
_EVIDENCE_ID_PREFIX = "evd"
_MARK_EVENT_ID_PREFIX = "rme"
_ACTIVITY_EVENT_ID_PREFIX = "rae"
_ANNOTATION_VERSION_ID_PREFIX = "anv"
_OUTCOME_ID_PREFIX = "rot"
_CHANGE_PROPOSAL_ID_PREFIX = "rcp"
_META_SCHEMA_VERSION_KEY = "schema_version"

_META_DDL = "CREATE TABLE IF NOT EXISTS review_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"


class AnnotationVersionConflict(RuntimeError):
    """A semantic comment changed after the caller last read its version."""


class ReviewArtifactBackend(Protocol):
    """Byte storage behind Review's deterministic artifact keys.

    Local Review leaves this unset and keeps the existing atomic filesystem
    writes. Hosted Review supplies an org-scoped content backend, while packet,
    blob, media and evidence serialization remain here in the shared core.
    ``write`` returns the SHA-256 identity of exactly ``payload``.
    """

    def write(self, key: str, payload: bytes) -> str: ...

    def read(self, key: str) -> bytes | None: ...

    def delete_prefix(self, prefix: str) -> None: ...


SCHEMA = """
CREATE TABLE IF NOT EXISTS review_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_sessions (
  id           TEXT PRIMARY KEY,
  subject_type TEXT NOT NULL,
  title        TEXT NOT NULL DEFAULT '',
  repo_root    TEXT NOT NULL,
  source_ref   TEXT NOT NULL DEFAULT '',
  range_mode   TEXT NOT NULL,
  actor_type   TEXT NOT NULL DEFAULT 'unknown',
  status       TEXT NOT NULL DEFAULT 'open',
  reviewer_id  TEXT NOT NULL DEFAULT 'local',
  current_revision_id TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_sessions_subject
  ON review_sessions(repo_root, subject_type, source_ref, range_mode);
CREATE INDEX IF NOT EXISTS idx_review_sessions_updated
  ON review_sessions(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS review_revisions (
  id                    TEXT PRIMARY KEY,
  review_id             TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  revision_number       INTEGER NOT NULL,
  range_mode            TEXT NOT NULL,
  base_sha              TEXT NOT NULL DEFAULT '',
  head_sha              TEXT NOT NULL DEFAULT '',
  merge_base_sha        TEXT NOT NULL DEFAULT '',
  dirty                 INTEGER NOT NULL DEFAULT 0,
  tree_fingerprint      TEXT NOT NULL,
  packet_schema_version INTEGER NOT NULL,
  packet_path           TEXT NOT NULL DEFAULT '',
  packet_sha256         TEXT NOT NULL DEFAULT '',
  packet_bytes          INTEGER NOT NULL DEFAULT 0,
  degraded_json         TEXT NOT NULL DEFAULT '[]',
  provenance_host       TEXT NOT NULL DEFAULT '',
  provenance_model      TEXT NOT NULL DEFAULT '',
  provenance_session_id TEXT NOT NULL DEFAULT '',
  provenance_certainty  TEXT NOT NULL DEFAULT 'none',
  created_at            TEXT NOT NULL,
  source_fingerprint    TEXT NOT NULL DEFAULT '',
  UNIQUE(review_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_review_revisions_review
  ON review_revisions(review_id, revision_number DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_revisions_tree
  ON review_revisions(review_id, tree_fingerprint);

CREATE TABLE IF NOT EXISTS review_units (
  revision_id         TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  unit_key            TEXT NOT NULL,
  kind                TEXT NOT NULL,
  path                TEXT NOT NULL,
  symbol              TEXT NOT NULL DEFAULT '',
  ordinal             INTEGER NOT NULL DEFAULT 0,
  start_line          INTEGER NOT NULL DEFAULT 0,
  end_line            INTEGER NOT NULL DEFAULT 0,
  content_fingerprint TEXT NOT NULL,
  fingerprint_method  TEXT NOT NULL DEFAULT 'unknown',
  attention_rank      INTEGER NOT NULL DEFAULT 0,
  attention_group     TEXT NOT NULL DEFAULT 'production',
  reasons_json        TEXT NOT NULL DEFAULT '[]',
  PRIMARY KEY (revision_id, unit_key)
);
CREATE INDEX IF NOT EXISTS idx_review_units_path ON review_units(revision_id, path);
CREATE INDEX IF NOT EXISTS idx_review_units_fp   ON review_units(content_fingerprint);

CREATE TABLE IF NOT EXISTS review_marks (
  review_id            TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  reviewer_id          TEXT NOT NULL DEFAULT 'local',
  unit_key             TEXT NOT NULL,
  state                TEXT NOT NULL,
  reviewed_revision_id TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  content_fingerprint  TEXT NOT NULL,
  actor_type           TEXT NOT NULL DEFAULT 'human',
  note                 TEXT NOT NULL DEFAULT '',
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  PRIMARY KEY (review_id, reviewer_id, unit_key)
);
CREATE INDEX IF NOT EXISTS idx_review_marks_state
  ON review_marks(review_id, state, updated_at DESC);

CREATE TABLE IF NOT EXISTS review_mark_events (
  id                    TEXT PRIMARY KEY,
  review_id             TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  reviewer_id           TEXT NOT NULL DEFAULT 'local',
  unit_key              TEXT NOT NULL,
  revision_id           TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  reviewed_revision_id  TEXT NOT NULL DEFAULT '',
  event_kind            TEXT NOT NULL,
  from_state            TEXT NOT NULL DEFAULT '',
  to_state              TEXT NOT NULL DEFAULT '',
  content_fingerprint   TEXT NOT NULL DEFAULT '',
  previous_unit_key     TEXT NOT NULL DEFAULT '',
  actor_type            TEXT NOT NULL DEFAULT 'human',
  note                  TEXT NOT NULL DEFAULT '',
  reason                TEXT NOT NULL DEFAULT '',
  created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_mark_events_review
  ON review_mark_events(review_id, reviewer_id, created_at ASC, id ASC);
CREATE INDEX IF NOT EXISTS idx_review_mark_events_unit
  ON review_mark_events(review_id, reviewer_id, unit_key, created_at ASC, id ASC);
CREATE INDEX IF NOT EXISTS idx_review_mark_events_revision
  ON review_mark_events(revision_id, created_at ASC, id ASC);

CREATE TABLE IF NOT EXISTS review_activity_events (
  id            TEXT PRIMARY KEY,
  review_id     TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  revision_id   TEXT NOT NULL DEFAULT '',
  kind          TEXT NOT NULL,
  actor_id      TEXT NOT NULL DEFAULT '',
  actor_type    TEXT NOT NULL DEFAULT 'unknown',
  subject_type  TEXT NOT NULL DEFAULT '',
  subject_id    TEXT NOT NULL DEFAULT '',
  summary       TEXT NOT NULL DEFAULT '',
  detail_json   TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_activity_events_review
  ON review_activity_events(review_id, created_at ASC, id ASC);
CREATE INDEX IF NOT EXISTS idx_review_activity_events_kind
  ON review_activity_events(review_id, kind, created_at ASC, id ASC);

-- A reviewer's frontier: the newest revision they have actually recorded a
-- verdict against. Durable in its own right, and deliberately not derivable
-- from ``review_marks``: reconciliation *deletes* a mark whose unit left the
-- review (a renamed symbol, a deleted file), and a frontier reconstructed from
-- the marks table would then forget that the reviewer had ever looked --
-- reporting "nothing reviewed yet" about code they demonstrably reviewed, for
-- whichever command happened to run the reconciliation first.
CREATE TABLE IF NOT EXISTS review_frontiers (
  review_id   TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  reviewer_id TEXT NOT NULL DEFAULT 'local',
  revision_id TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  updated_at  TEXT NOT NULL,
  PRIMARY KEY (review_id, reviewer_id)
);

-- Verdicts reconciliation deleted. Clearing the mark is right -- its unit is
-- not in the review any more -- but clearing the *evidence that a human looked*
-- is what made "your approval was thrown away" the one thing this tool did
-- silently, and what let an undo re-present content the reviewer had already
-- approved as brand new work. Append-only; the primary key makes re-running a
-- reconciliation a no-op rather than a second announcement.
CREATE TABLE IF NOT EXISTS review_discarded_marks (
  review_id             TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  reviewer_id           TEXT NOT NULL DEFAULT 'local',
  unit_key              TEXT NOT NULL,
  discarded_revision_id TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  state                 TEXT NOT NULL,
  content_fingerprint   TEXT NOT NULL,
  reviewed_revision_id  TEXT NOT NULL DEFAULT '',
  kind                  TEXT NOT NULL DEFAULT 'file',
  path                  TEXT NOT NULL DEFAULT '',
  symbol                TEXT NOT NULL DEFAULT '',
  ordinal               INTEGER NOT NULL DEFAULT 0,
  start_line            INTEGER NOT NULL DEFAULT 0,
  reason                TEXT NOT NULL DEFAULT '',
  discarded_at          TEXT NOT NULL,
  PRIMARY KEY (review_id, reviewer_id, unit_key, content_fingerprint, discarded_revision_id)
);
CREATE INDEX IF NOT EXISTS idx_review_discarded_revision
  ON review_discarded_marks(discarded_revision_id);

CREATE TABLE IF NOT EXISTS annotations (
  id                   TEXT PRIMARY KEY,
  review_id            TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  revision_id          TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  parent_id            TEXT NOT NULL DEFAULT '',
  anchor_json          TEXT NOT NULL,
  unit_key             TEXT NOT NULL DEFAULT '',
  path                 TEXT NOT NULL DEFAULT '',
  kind                 TEXT NOT NULL DEFAULT 'comment',
  body                 TEXT NOT NULL DEFAULT '',
  state                TEXT NOT NULL DEFAULT 'open',
  created_by           TEXT NOT NULL DEFAULT 'local',
  created_by_actor     TEXT NOT NULL DEFAULT 'human',
  resolved_revision_id TEXT NOT NULL DEFAULT '',
  anchor_method        TEXT NOT NULL DEFAULT 'identical_blob',
  anchor_detail        TEXT NOT NULL DEFAULT '',
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  source               TEXT NOT NULL DEFAULT 'human',
  source_id            TEXT NOT NULL DEFAULT '',
  title                TEXT NOT NULL DEFAULT '',
  evidence_json        TEXT NOT NULL DEFAULT '[]',
  confidence           REAL,
  author_response      TEXT NOT NULL DEFAULT 'none',
  author_response_source_id TEXT NOT NULL DEFAULT '',
  author_response_at   TEXT NOT NULL DEFAULT '',
  turn_owner_kind      TEXT NOT NULL DEFAULT 'none',
  turn_owner_id        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_annotations_review
  ON annotations(review_id, state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_annotations_path ON annotations(review_id, path);

CREATE TABLE IF NOT EXISTS review_change_proposals (
  id                         TEXT PRIMARY KEY,
  review_id                  TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  base_revision_id           TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  path                       TEXT NOT NULL,
  start_line                 INTEGER NOT NULL,
  end_line                   INTEGER NOT NULL,
  original_text              TEXT NOT NULL DEFAULT '',
  replacement_text           TEXT NOT NULL DEFAULT '',
  patch_text                 TEXT NOT NULL DEFAULT '',
  base_file_sha256           TEXT NOT NULL,
  state                      TEXT NOT NULL DEFAULT 'proposed',
  target_unit_key            TEXT NOT NULL DEFAULT '',
  annotation_id              TEXT NOT NULL DEFAULT '',
  intent                     TEXT NOT NULL DEFAULT '',
  conflict_reason            TEXT NOT NULL DEFAULT '',
  created_by                 TEXT NOT NULL DEFAULT 'local',
  created_at                 TEXT NOT NULL,
  updated_at                 TEXT NOT NULL,
  applied_at                 TEXT NOT NULL DEFAULT '',
  applied_source_fingerprint TEXT NOT NULL DEFAULT '',
  result_revision_id         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_review_change_proposals_review
  ON review_change_proposals(review_id, created_at ASC, id ASC);
CREATE INDEX IF NOT EXISTS idx_review_change_proposals_state
  ON review_change_proposals(review_id, state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_change_proposals_path
  ON review_change_proposals(review_id, path, start_line, end_line);

CREATE TABLE IF NOT EXISTS annotation_versions (
  id                  TEXT PRIMARY KEY,
  annotation_id       TEXT NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
  review_id           TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  revision_id         TEXT NOT NULL DEFAULT '',
  version_number      INTEGER NOT NULL,
  body                TEXT NOT NULL DEFAULT '',
  kind                TEXT NOT NULL DEFAULT 'comment',
  state               TEXT NOT NULL DEFAULT 'open',
  author_response     TEXT NOT NULL DEFAULT 'none',
  author_response_source_id TEXT NOT NULL DEFAULT '',
  author_response_at  TEXT NOT NULL DEFAULT '',
  resolved_revision_id TEXT NOT NULL DEFAULT '',
  changed_by          TEXT NOT NULL DEFAULT '',
  changed_by_actor    TEXT NOT NULL DEFAULT 'unknown',
  change_kind         TEXT NOT NULL DEFAULT 'edited',
  turn_owner_kind     TEXT NOT NULL DEFAULT 'none',
  turn_owner_id       TEXT NOT NULL DEFAULT '',
  created_at          TEXT NOT NULL,
  UNIQUE(annotation_id, version_number)
);
CREATE INDEX IF NOT EXISTS idx_annotation_versions_annotation
  ON annotation_versions(annotation_id, version_number ASC);
CREATE INDEX IF NOT EXISTS idx_annotation_versions_review
  ON annotation_versions(review_id, created_at ASC, id ASC);

CREATE TABLE IF NOT EXISTS annotation_anchor_events (
  id            TEXT PRIMARY KEY,
  annotation_id TEXT NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
  revision_id   TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  method        TEXT NOT NULL,
  status        TEXT NOT NULL,
  detail        TEXT NOT NULL DEFAULT '',
  anchor_json   TEXT NOT NULL DEFAULT '{}',
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_anchor_events_annotation
  ON annotation_anchor_events(annotation_id, created_at DESC);
-- The idempotency lookup: "has this comment already been resolved against this
-- revision?" is asked before every re-anchor, on every path.
CREATE INDEX IF NOT EXISTS idx_anchor_events_revision
  ON annotation_anchor_events(revision_id, annotation_id);

CREATE TABLE IF NOT EXISTS deliveries (
  id            TEXT PRIMARY KEY,
  annotation_id TEXT NOT NULL REFERENCES annotations(id) ON DELETE CASCADE,
  target_type   TEXT NOT NULL,
  target_ref    TEXT NOT NULL DEFAULT '',
  state         TEXT NOT NULL DEFAULT 'pending',
  remote_ref    TEXT NOT NULL DEFAULT '',
  last_error    TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL,
  updated_at    TEXT NOT NULL,
  operation_id  TEXT NOT NULL DEFAULT '',
  revision_id   TEXT NOT NULL DEFAULT '',
  feedback_hash TEXT NOT NULL DEFAULT '',
  annotation_version INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_deliveries_state ON deliveries(state, updated_at DESC);
-- Indexes that depend on v3_009 columns live in that migration, not the base
-- script. On an existing database ``CREATE TABLE IF NOT EXISTS`` leaves the
-- old deliveries shape untouched, so creating those indexes here would run
-- before migrations and fail with "no such column: operation_id".

CREATE TABLE IF NOT EXISTS review_evidence (
  id            TEXT PRIMARY KEY,
  review_id     TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  revision_id   TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL,
  title         TEXT NOT NULL DEFAULT '',
  path          TEXT NOT NULL DEFAULT '',
  artifact_path TEXT NOT NULL DEFAULT '',
  url           TEXT NOT NULL DEFAULT '',
  content_hash  TEXT NOT NULL DEFAULT '',
  mime_type     TEXT NOT NULL DEFAULT '',
  bytes         INTEGER NOT NULL DEFAULT 0,
  source        TEXT NOT NULL DEFAULT 'human',
  source_ref    TEXT NOT NULL DEFAULT '',
  verification_status TEXT NOT NULL DEFAULT '',
  detail        TEXT NOT NULL DEFAULT '',
  created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_evidence_review ON review_evidence(review_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_evidence_revision ON review_evidence(revision_id, created_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS annotations_fts USING fts5(
  body, path, content=''
);
"""

_SESSION_UPDATABLE = (
    "subject_type",
    "title",
    "repo_root",
    "source_ref",
    "range_mode",
    "actor_type",
    "status",
    "reviewer_id",
    "current_revision_id",
)
_ANNOTATION_UPDATABLE = (
    "parent_id",
    "kind",
    "body",
    "state",
    "created_by",
    "created_by_actor",
    "source_id",
    "title",
    "evidence_json",
    "confidence",
    "author_response",
    "author_response_source_id",
    "author_response_at",
    "resolved_revision_id",
    "anchor_method",
    "anchor_detail",
    "turn_owner_kind",
    "turn_owner_id",
)


def utc_now() -> str:
    """Aware-UTC ISO-8601, the only timestamp format these tables accept.

    Naive local time is unsyncable: two machines write the same string for
    different instants and no later reader can tell them apart.
    """
    return datetime.now(UTC).isoformat()


def _uuid_payload(identifier: str) -> str:
    """Return one canonical 32-hex UUID payload, or ``""`` when invalid."""

    payload = identifier.replace("-", "").lower()
    return payload if len(payload) == 32 and all(char in "0123456789abcdef" for char in payload) else ""


def _legacy_storage_payload(identifier: str, prefixes: tuple[str, ...]) -> str:
    """Extract the UUID payload from one pre-v1 Review storage id."""

    for prefix in prefixes:
        marker = f"{prefix}-"
        if identifier.startswith(marker):
            return _uuid_payload(identifier[len(marker) :])
    return ""


def _canonical_storage_id(identifier: str, *, revision: bool) -> str:
    """Canonicalise old prefixed UUID ids to the new bare storage id."""

    if payload := _uuid_payload(identifier):
        return payload
    prefixes = ("rr", "rrv") if revision else ("r", "rev")
    return _legacy_storage_payload(identifier, prefixes) or identifier


def public_review_reference(review_id: str) -> str:
    """Canonical public Review reference for one internal Review id."""

    return f"{_SESSION_REF_PREFIX}{review_id}"


def public_revision_reference(revision_id: str) -> str:
    """Canonical public Review-revision reference for one internal revision id."""

    return f"{_REVISION_REF_PREFIX}{revision_id}"


def new_session_id() -> str:
    return make_uuid7().replace("-", "")


def new_revision_id() -> str:
    return make_uuid7().replace("-", "")


def new_annotation_id() -> str:
    return f"{_ANNOTATION_ID_PREFIX}-{make_uuid7()}"


def _new_anchor_event_id() -> str:
    return f"{_ANCHOR_EVENT_ID_PREFIX}-{make_uuid7()}"


def new_delivery_id() -> str:
    return f"{_DELIVERY_ID_PREFIX}-{make_uuid7()}"


def new_evidence_id() -> str:
    return f"{_EVIDENCE_ID_PREFIX}-{make_uuid7()}"


def _new_mark_event_id() -> str:
    return f"{_MARK_EVENT_ID_PREFIX}-{make_uuid7()}"


def _new_activity_event_id() -> str:
    return f"{_ACTIVITY_EVENT_ID_PREFIX}-{make_uuid7()}"


def _new_annotation_version_id() -> str:
    return f"{_ANNOTATION_VERSION_ID_PREFIX}-{make_uuid7()}"


def _new_outcome_id() -> str:
    return f"{_OUTCOME_ID_PREFIX}-{make_uuid7()}"


def new_change_proposal_id() -> str:
    return f"{_CHANGE_PROPOSAL_ID_PREFIX}-{make_uuid7()}"


def _json_tuple(raw: object) -> tuple[str, ...]:
    """Decode a ``TEXT`` JSON array column into a tuple of strings.

    Never raises: a column corrupted by hand-editing degrades to ``()`` rather
    than taking down a review the reader can otherwise still use.
    """
    if not isinstance(raw, str) or not raw:
        return ()
    try:
        loaded = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(str(item) for item in loaded)


def _dump_tuple(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _anchor_to_json(anchor: AnnotationAnchor) -> str:
    return json.dumps(asdict(anchor), ensure_ascii=False, sort_keys=True)


def _anchor_from_json(raw: object) -> AnnotationAnchor:
    """Rebuild an anchor from its stored JSON, tolerating drift.

    Unknown keys are dropped and missing ones fall back to the dataclass
    default, so a row written by a build with one extra anchor field still
    reads back as a usable anchor here.
    """
    payload: dict[str, Any] = {}
    if isinstance(raw, str) and raw:
        try:
            loaded = json.loads(raw)
        except (TypeError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            payload = loaded
    known = {f.name for f in dataclass_fields(AnnotationAnchor)}
    kwargs = {key: value for key, value in payload.items() if key in known}
    kwargs.setdefault("path", "")
    kwargs.setdefault("side", "new")
    kwargs.setdefault("start_line", 0)
    kwargs.setdefault("end_line", 0)
    return AnnotationAnchor(**kwargs)


@dataclass(frozen=True)
class AnnotationHistoryContext:
    revision_id: str = ""
    changed_by: str = ""
    changed_by_actor: ActorType = "unknown"
    change_kind: AnnotationVersionKind | None = None
    expected_version: int | None = None


def _insert_activity_event(conn: sqlite3.Connection, event: ReviewActivityEvent) -> ReviewActivityEvent:
    now = event.created_at or utc_now()
    record = replace(event, id=event.id or _new_activity_event_id(), created_at=now)
    conn.execute(
        """
        INSERT INTO review_activity_events (
            id, review_id, revision_id, kind, actor_id, actor_type,
            subject_type, subject_id, summary, detail_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.id,
            record.review_id,
            record.revision_id,
            record.kind,
            record.actor_id,
            record.actor_type,
            record.subject_type,
            record.subject_id,
            record.summary,
            record.detail_json,
            record.created_at,
        ),
    )
    return record


def _insert_mark_event(conn: sqlite3.Connection, event: ReviewMarkEvent) -> ReviewMarkEvent:
    now = event.created_at or utc_now()
    record = replace(event, id=event.id or _new_mark_event_id(), created_at=now)
    conn.execute(
        """
        INSERT INTO review_mark_events (
            id, review_id, reviewer_id, unit_key, revision_id, reviewed_revision_id,
            event_kind, from_state, to_state, content_fingerprint, previous_unit_key,
            actor_type, note, reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.id,
            record.review_id,
            record.reviewer_id,
            record.unit_key,
            record.revision_id,
            record.reviewed_revision_id,
            record.event_kind,
            record.from_state,
            record.to_state,
            record.content_fingerprint,
            record.previous_unit_key,
            record.actor_type,
            record.note,
            record.reason,
            record.created_at,
        ),
    )
    return record


def _insert_annotation_version(conn: sqlite3.Connection, version: AnnotationVersion) -> AnnotationVersion:
    row = conn.execute(
        "SELECT COALESCE(MAX(version_number), 0) AS n FROM annotation_versions WHERE annotation_id = ?",
        (version.annotation_id,),
    ).fetchone()
    number = version.version_number if version.version_number > 0 else (int(row["n"]) + 1 if row is not None else 1)
    now = version.created_at or utc_now()
    record = replace(
        version,
        id=version.id or _new_annotation_version_id(),
        version_number=number,
        created_at=now,
    )
    conn.execute(
        """
        INSERT INTO annotation_versions (
            id, annotation_id, review_id, revision_id, version_number, body, kind,
            state, author_response, author_response_source_id, author_response_at,
            resolved_revision_id, changed_by, changed_by_actor, change_kind,
            turn_owner_kind, turn_owner_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            record.id,
            record.annotation_id,
            record.review_id,
            record.revision_id,
            record.version_number,
            record.body,
            record.kind,
            record.state,
            record.author_response,
            record.author_response_source_id,
            record.author_response_at,
            record.resolved_revision_id,
            record.changed_by,
            record.changed_by_actor,
            record.change_kind,
            record.turn_owner_kind,
            record.turn_owner_id,
            record.created_at,
        ),
    )
    return record


class ReviewStore(SqliteTableStore):
    """Durable review sessions, revisions, units, marks and annotations."""

    SCHEMA = SCHEMA
    MIGRATIONS: ClassVar[tuple[str, ...]] = (
        "v3_001_review_annotations.sql",
        "v3_002_review_source_fingerprint.sql",
        "v3_003_review_author_response.sql",
        "v3_004_review_evidence_verification.sql",
        "v3_005_review_history.sql",
        "v3_006_review_collaboration.sql",
        "v3_009_review_feedback_operations.sql",
        "v3_011_review_outcomes.sql",
        "v3_012_review_current_revision.sql",
        "v3_013_review_change_proposals.sql",
    )
    """Additive review-store migrations.

    Schema version 1 intentionally remains readable because these migrations only
    append columns with defaults. ``_schema_migrations`` records whether each
    additive migration ran; the review_meta version is reserved for incompatible
    field removals/type changes.
    """
    REQUIRED_TABLES: ClassVar[tuple[str, ...]] = (
        "review_meta",
        "review_sessions",
        "review_revisions",
        "review_units",
        "review_marks",
        "review_mark_events",
        "review_activity_events",
        "review_outcomes",
        "review_frontiers",
        "review_discarded_marks",
        "annotations",
        "review_change_proposals",
        "annotation_versions",
        "annotation_anchor_events",
        "deliveries",
        "review_evidence",
        "annotations_fts",
    )

    def __init__(
        self,
        root: Path | str,
        *,
        artifact_backend: ReviewArtifactBackend | None = None,
        source_content_reader: Callable[[str], bytes | None] | None = None,
    ) -> None:
        super().__init__(root, db_name=DB_NAME)
        self.review_dir = self.root / "review"
        self.artifacts_dir = self.review_dir / ARTIFACT_DIRNAME
        self._artifact_backend = artifact_backend
        self._source_content_reader = source_content_reader
        self._ready = False

    # ----- lifecycle ------------------------------------------------------ #

    def init(self) -> None:
        """Create the schema, run migrations, and record the schema version.

        The version check runs *before* any DDL: refusing a newer database is
        pointless if we have already written into it.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        with self._transaction() as conn:
            conn.executescript(_META_DDL)
            self._check_version(conn)
            stored = self._stored_version(conn)
        super().init()
        self._migrate_review_identity()
        if stored != SESSION_SCHEMA_VERSION:
            # Only written when it is actually wrong. Rewriting it every time
            # would make constructing a store a *write*, so a read-only request
            # in a workspace process would take the write lock (and hold it for
            # the length of an enclosing read_scope) purely to say nothing
            # changed.
            with self._transaction() as conn:
                conn.execute(
                    "INSERT INTO review_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (_META_SCHEMA_VERSION_KEY, str(SESSION_SCHEMA_VERSION)),
                )
        if self._artifact_backend is None:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            self._ignore_our_own_tree()
        self._ready = True

    def _migrate_review_identity(self) -> None:
        """Migrate prefixed Review/revision PKs to bare UUID payloads once.

        Public namespaces belong to refs (``r/<id>`` and ``rr/<id>``), not to
        storage keys.  The migration rewrites every hard and soft Review/
        revision reference with foreign-key enforcement temporarily disabled,
        records the old->new map durably, then moves deterministic artifacts.
        The persisted map makes the filesystem/object-store phase resumable.
        """

        conn = sqlite3.connect(self.db_path, timeout=120)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 120000")
        try:
            version_row = conn.execute(
                "SELECT value FROM review_meta WHERE key = ?", (_META_IDENTITY_VERSION_KEY,)
            ).fetchone()
            if version_row is not None and str(version_row["value"]) == "1":
                return

            map_row = conn.execute("SELECT value FROM review_meta WHERE key = ?", (_META_IDENTITY_MAP_KEY,)).fetchone()
            if map_row is None:
                review_rows = conn.execute("SELECT id FROM review_sessions ORDER BY id").fetchall()
                revision_rows = conn.execute("SELECT id, review_id FROM review_revisions ORDER BY id").fetchall()
                review_map = {
                    str(row["id"]): _canonical_storage_id(str(row["id"]), revision=False) for row in review_rows
                }
                revision_map = {
                    str(row["id"]): {
                        "new": _canonical_storage_id(str(row["id"]), revision=True),
                        "old_review": str(row["review_id"]),
                        "new_review": review_map.get(str(row["review_id"]), str(row["review_id"])),
                    }
                    for row in revision_rows
                }
                migration = {"reviews": review_map, "revisions": revision_map}
                self._rewrite_review_identity_db(conn, migration)
            else:
                migration = json.loads(str(map_row["value"]))
                if not isinstance(migration, dict):
                    raise RuntimeError("invalid persisted Review identity migration map")
        finally:
            conn.close()

        self._migrate_review_identity_artifacts(migration)
        with self._transaction() as done:
            done.execute(
                "INSERT INTO review_meta (key, value) VALUES (?, '1') "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_META_IDENTITY_VERSION_KEY,),
            )
            done.execute("DELETE FROM review_meta WHERE key = ?", (_META_IDENTITY_MAP_KEY,))

    def _identity_review_reference_columns(self) -> tuple[tuple[str, str], ...]:
        """Review-id columns owned by this store composition.

        Hosted Review extends this list with private collaboration/provider
        tables. Keeping the hook here lets one migration rewrite the complete
        aggregate without teaching the public store about private schemas.
        """

        return (
            ("review_revisions", "review_id"),
            ("review_marks", "review_id"),
            ("review_mark_events", "review_id"),
            ("review_activity_events", "review_id"),
            ("review_frontiers", "review_id"),
            ("review_discarded_marks", "review_id"),
            ("annotations", "review_id"),
            ("review_change_proposals", "review_id"),
            ("annotation_versions", "review_id"),
            ("review_evidence", "review_id"),
            ("review_outcomes", "review_id"),
        )

    def _identity_revision_reference_columns(self) -> tuple[tuple[str, str], ...]:
        """Revision-id columns owned by this store composition."""

        return (
            ("review_sessions", "current_revision_id"),
            ("review_units", "revision_id"),
            ("review_marks", "reviewed_revision_id"),
            ("review_mark_events", "revision_id"),
            ("review_mark_events", "reviewed_revision_id"),
            ("review_activity_events", "revision_id"),
            ("review_frontiers", "revision_id"),
            ("review_discarded_marks", "discarded_revision_id"),
            ("review_discarded_marks", "reviewed_revision_id"),
            ("annotations", "revision_id"),
            ("annotations", "resolved_revision_id"),
            ("review_change_proposals", "base_revision_id"),
            ("review_change_proposals", "result_revision_id"),
            ("annotation_versions", "revision_id"),
            ("annotation_versions", "resolved_revision_id"),
            ("annotation_anchor_events", "revision_id"),
            ("deliveries", "revision_id"),
            ("review_evidence", "revision_id"),
            ("review_outcomes", "revision_id"),
        )

    def _rewrite_review_identity_db(self, conn: sqlite3.Connection, migration: Mapping[str, Any]) -> None:
        review_map = {str(k): str(v) for k, v in dict(migration.get("reviews") or {}).items()}
        revision_info = dict(migration.get("revisions") or {})
        revision_map = {
            str(old): str(info.get("new") or old) for old, info in revision_info.items() if isinstance(info, Mapping)
        }
        if len(set(review_map.values())) != len(review_map):
            raise RuntimeError("Review identity migration would create duplicate Review ids")
        if len(set(revision_map.values())) != len(revision_map):
            raise RuntimeError("Review identity migration would create duplicate revision ids")

        existing_tables = {
            str(row["name"]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        review_refs = tuple(
            (table, column) for table, column in self._identity_review_reference_columns() if table in existing_tables
        )
        revision_refs = tuple(
            (table, column) for table, column in self._identity_revision_reference_columns() if table in existing_tables
        )

        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO review_meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_META_IDENTITY_MAP_KEY, json.dumps(migration, sort_keys=True)),
            )
            for old, new in review_map.items():
                if old == new:
                    continue
                for table, column in review_refs:
                    conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (new, old))
                conn.execute(
                    "UPDATE review_activity_events SET subject_id = ? "
                    "WHERE subject_type = 'review' AND subject_id = ?",
                    (new, old),
                )
                conn.execute("UPDATE review_sessions SET id = ? WHERE id = ?", (new, old))
            for old, new in revision_map.items():
                if old == new:
                    continue
                for table, column in revision_refs:
                    conn.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (new, old))
                conn.execute(
                    "UPDATE review_activity_events SET subject_id = ? "
                    "WHERE subject_type = 'revision' AND subject_id = ?",
                    (new, old),
                )
                conn.execute("UPDATE review_revisions SET id = ? WHERE id = ?", (new, old))
            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"Review identity migration left foreign-key violations: {violations!r}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("PRAGMA foreign_keys = ON")

    def _migrate_review_identity_artifacts(self, migration: Mapping[str, Any]) -> None:
        revision_info = dict(migration.get("revisions") or {})
        moves: list[tuple[str, str]] = []
        old_review_prefixes: set[str] = set()
        for old_revision, raw_info in revision_info.items():
            if not isinstance(raw_info, Mapping):
                continue
            new_revision = str(raw_info.get("new") or old_revision)
            old_review = str(raw_info.get("old_review") or "")
            new_review = str(raw_info.get("new_review") or old_review)
            if old_review == new_review and old_revision == new_revision:
                continue
            old_prefix = f"review/{ARTIFACT_DIRNAME}/{old_review}/{old_revision}/"
            new_prefix = f"review/{ARTIFACT_DIRNAME}/{new_review}/{new_revision}/"
            moves.append((old_prefix, new_prefix))
            old_review_prefixes.add(f"review/{ARTIFACT_DIRNAME}/{old_review}/")

        if not moves:
            return

        if self._artifact_backend is None:
            for old_prefix, new_prefix in moves:
                old_dir = self.root / old_prefix.rstrip("/")
                new_dir = self.root / new_prefix.rstrip("/")
                if not old_dir.exists():
                    continue
                new_dir.parent.mkdir(parents=True, exist_ok=True)
                if new_dir.exists():
                    for child in old_dir.iterdir():
                        target = new_dir / child.name
                        if not target.exists():
                            shutil.move(str(child), str(target))
                    with contextlib.suppress(OSError):
                        old_dir.rmdir()
                else:
                    shutil.move(str(old_dir), str(new_dir))
            for old_review_prefix in old_review_prefixes:
                with contextlib.suppress(OSError):
                    (self.root / old_review_prefix.rstrip("/")).rmdir()
        else:
            deterministic = (
                ARTIFACT_FILENAME,
                BLOB_ARTIFACT_FILENAME,
                MEDIA_ARTIFACT_FILENAME,
                SUBMODULE_ARTIFACT_FILENAME,
                SOURCE_TREE_ARTIFACT_FILENAME,
                OLD_SOURCE_TREE_ARTIFACT_FILENAME,
            )
            with self._transaction() as conn:
                evidence_paths = [
                    str(row["artifact_path"])
                    for row in conn.execute(
                        "SELECT artifact_path FROM review_evidence WHERE artifact_path <> ''"
                    ).fetchall()
                ]
            for old_prefix, new_prefix in moves:
                keys = [f"{old_prefix}{name}" for name in deterministic]
                keys.extend(path for path in evidence_paths if path.startswith(old_prefix))
                for old_key in keys:
                    payload = self._artifact_backend.read(old_key)
                    if payload is None:
                        continue
                    self._artifact_backend.write(new_prefix + old_key[len(old_prefix) :], payload)
            for old_review_prefix in old_review_prefixes:
                self._artifact_backend.delete_prefix(old_review_prefix)

        with self._transaction() as conn:
            for old_prefix, new_prefix in moves:
                conn.execute(
                    "UPDATE review_revisions SET packet_path = replace(packet_path, ?, ?) " "WHERE packet_path LIKE ?",
                    (old_prefix, new_prefix, f"{old_prefix}%"),
                )
                conn.execute(
                    "UPDATE review_evidence SET artifact_path = replace(artifact_path, ?, ?) "
                    "WHERE artifact_path LIKE ?",
                    (old_prefix, new_prefix, f"{old_prefix}%"),
                )

    def _ignore_our_own_tree(self) -> None:
        """Keep review artifacts out of a user's commits (spec §3.3).

        The store root is frequently ``~/.lemoncrow``, but nothing stops it
        being project-local -- ``LEMONCROW_ROOT``, ``lc --root``, a workspace
        store dir -- and a packet artifact or a rendered HTML report that lands
        in ``git status`` is a review leaking into the change it reviews.

        Two scopes, deliberately unequal:

        * ``<root>/review/`` always. We create that directory and nothing else
          writes into it, so blanket-ignoring it can never touch a user's file.
        * ``<root>`` itself **only** when it carries our own directory name.
          Writing ``*`` into a directory somebody pointed ``LEMONCROW_ROOT`` at
          would silently untrack their work, and no artifact is worth that.

        Never raises: a read-only or unwritable store root is a reason to skip
        the courtesy, not to refuse the review.
        """

        with contextlib.suppress(OSError):
            if self.root.name == DEFAULT_STORE_DIRNAME:
                ensure_dir_gitignore(self.root)
            ensure_dir_gitignore(self.review_dir)

    def _check_version(self, conn: sqlite3.Connection) -> None:
        """Refuse a database written by a build that knew more than this one.

        ``PRAGMA user_version`` is not used: it is a bare integer with no
        provenance, and this repository has no precedent for reading it. A
        ``review_meta`` row is inspectable with the same ``sqlite3`` shell a
        support conversation already reaches for.

        stored == code  -> proceed.
        stored <  code  -> ``MIGRATIONS`` runs, then the row is rewritten.
        stored >  code  -> refuse. Review state is not a rebuildable cache, and
                           a half-understood write to it is silent data loss.
        """
        stored = self._stored_version(conn)
        if stored is None or stored <= SESSION_SCHEMA_VERSION:
            return
        raise RuntimeError(
            f"{DB_NAME} was written by a newer LemonCrow "
            f"(schema {stored}, this build understands {SESSION_SCHEMA_VERSION}) "
            "— upgrade lemoncrow or move the file aside"
        )

    def _stored_version(self, conn: sqlite3.Connection) -> int | None:
        try:
            row = conn.execute(
                "SELECT value FROM review_meta WHERE key = ?",
                (_META_SCHEMA_VERSION_KEY,),
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        try:
            return int(str(row["value"]).strip())
        except (TypeError, ValueError):
            # An unreadable version is not a newer version. Treat it as absent
            # and let init() rewrite it rather than bricking the store.
            return None

    def _ensure_ready(self) -> None:
        """Self-initialise on first use.

        This store is not a ``StoreBundle`` member, so nothing else calls
        ``init()`` for it. Doing it here also means the newer-database check
        runs on every fresh instance, not only where someone remembered to.
        """
        if not self._ready:
            self.init()

    # ----- sessions ------------------------------------------------------- #

    def create_session(self, session: ReviewSession) -> ReviewSession:
        """Insert a session, filling id and timestamps when blank.

        A duplicate ``(repo_root, subject_type, source_ref, range_mode)`` hits
        ``ux_review_sessions_subject`` and raises ``sqlite3.IntegrityError``.
        That is the reopen identity doing its job: callers reopen with
        :meth:`find_session` instead of creating a rival session that would
        strand the first one's marks.
        """
        self._ensure_ready()
        now = utc_now()
        record = ReviewSession(
            id=session.id or new_session_id(),
            subject_type=session.subject_type,
            repo_root=session.repo_root,
            range_mode=session.range_mode,
            title=session.title,
            source_ref=session.source_ref,
            actor_type=session.actor_type,
            status=session.status,
            reviewer_id=session.reviewer_id,
            current_revision_id=session.current_revision_id,
            created_at=session.created_at or now,
            updated_at=session.updated_at or now,
        )
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO review_sessions (
                    id, subject_type, title, repo_root, source_ref, range_mode,
                    actor_type, status, reviewer_id, current_revision_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.subject_type,
                    record.title,
                    record.repo_root,
                    record.source_ref,
                    record.range_mode,
                    record.actor_type,
                    record.status,
                    record.reviewer_id,
                    record.current_revision_id,
                    record.created_at,
                    record.updated_at,
                ),
            )
            _insert_activity_event(
                conn,
                ReviewActivityEvent(
                    id="",
                    review_id=record.id,
                    revision_id="",
                    kind="review.created",
                    actor_id=record.reviewer_id,
                    actor_type="human",
                    subject_type="review",
                    subject_id=record.id,
                    summary="Review created",
                    detail_json=json.dumps(
                        {"subject_type": record.subject_type, "range_mode": record.range_mode},
                        sort_keys=True,
                    ),
                    created_at=record.created_at,
                ),
            )
        return record

    def get_session(self, review_id: str) -> ReviewSession | None:
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM review_sessions WHERE id = ?", (review_id,)).fetchone()
        return None if row is None else _row_to_session(row)

    def resolve_session(self, reference: str) -> ReviewSession | None:
        """Resolve one exact internal id or canonical ``r/<id>`` public ref."""

        exact = self.get_session(reference)
        if exact is not None:
            return exact
        if not reference.startswith(_SESSION_REF_PREFIX):
            return None
        review_id = reference[len(_SESSION_REF_PREFIX) :]
        return self.get_session(review_id) if review_id else None

    def session_reference(self, review_id: str) -> str:
        """Return the canonical public ref for one durable Review id."""

        return public_review_reference(review_id)

    def find_session(
        self,
        *,
        repo_root: str,
        subject_type: str,
        source_ref: str,
        range_mode: str,
    ) -> ReviewSession | None:
        """Look a session up by its reopen identity."""
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM review_sessions
                 WHERE repo_root = ? AND subject_type = ? AND source_ref = ? AND range_mode = ?
                """,
                (repo_root, subject_type, source_ref, range_mode),
            ).fetchone()
        return None if row is None else _row_to_session(row)

    def list_sessions(self, *, status: str = "open", limit: int | None = 50) -> tuple[ReviewSession, ...]:
        """Most-recently-updated sessions; ``status=""`` lists every status.

        ``limit=None`` is reserved for higher-level cursor pagination that must
        merge several repository stores without imposing a hidden per-store
        truncation first.
        """
        self._ensure_ready()
        sql = "SELECT * FROM review_sessions"
        params: list[Any] = []
        if status:
            sql += " WHERE status = ?"
            params.append(status)
        sql += " ORDER BY updated_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, limit))
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(_row_to_session(row) for row in rows)

    def update_session(self, review_id: str, **fields: object) -> None:
        """Patch named session columns; unknown names raise rather than no-op.

        A silently ignored field name is a caller that thinks it saved
        something it did not.
        """
        self._ensure_ready()
        unknown = sorted(set(fields) - set(_SESSION_UPDATABLE))
        if unknown:
            raise ValueError(f"unknown review_sessions column(s): {', '.join(unknown)}")
        if not fields:
            return
        assignments = ", ".join(f"{name} = ?" for name in fields)
        params: list[Any] = [fields[name] for name in fields]
        params.append(utc_now())
        params.append(review_id)
        with self._transaction() as conn:
            conn.execute(
                f"UPDATE review_sessions SET {assignments}, updated_at = ? WHERE id = ?",
                params,
            )

    # ----- revisions + units ---------------------------------------------- #

    @staticmethod
    def _lock_for_write(conn: sqlite3.Connection, *, key: str = "") -> None:
        """Take SQLite's write lock *before* the read this write depends on.

        :meth:`SqliteTableStore._transaction` opens a deferred transaction, so
        sqlite3 issues its implicit ``BEGIN`` only just before the first write
        statement -- any ``SELECT`` taken beforehand is an unlocked snapshot
        another process can invalidate. ``BEGIN IMMEDIATE`` makes a
        read-then-insert pair atomic instead; the inherited 120s
        ``busy_timeout`` covers the wait for the other writer.

        Inside ``batch_mode``/``read_scope`` the transaction is already open and
        owned by that scope, so this is a no-op there.
        """

        del key  # SQLite has one database-wide writer lock.
        if conn.in_transaction:
            return
        with contextlib.suppress(sqlite3.OperationalError):
            conn.execute("BEGIN IMMEDIATE")

    def _discard_revision_artifacts(self, review_id: str, revision_id: str) -> None:
        """Delete the artifact directory of a revision that never got a row.

        ``record_revision`` writes ``packet.json.gz`` and ``blobs.json.gz``
        under a freshly minted revision id *before* it calls
        :meth:`add_revision`. When that id loses the tree race the row is never
        stored, so nothing can ever reference those bytes and nothing will ever
        collect them either -- :meth:`prune` reaches artifacts only for a
        session somebody archived. Revision ids are unique, so the directory
        holds this call's orphans and nothing else.
        """

        try:
            review_seg = safe_segment(review_id, field="review_id")
            revision_seg = safe_segment(revision_id, field="revision_id")
        except ValueError:
            return
        self._delete_artifact_prefix(f"review/{ARTIFACT_DIRNAME}/{review_seg}/{revision_seg}/")

    def add_revision(self, revision: ReviewRevision, units: Sequence[ReviewUnit]) -> ReviewRevision:
        """Write a revision and all of its units in **one** transaction.

        A revision row without its units is worse than no revision at all: the
        frontier would read it as "this revision reviewed nothing", so every
        mark carried against it would look stale. Either both land or neither
        does.

        ``revision_number`` is assigned here when the caller leaves it at 0, so
        density is the store's problem rather than every call site's.

        One production caller, and it must stay one:
        ``sources.local.record_revision``, which reconciles the reviewer's marks
        and comment anchors onto the row in the same breath. A second caller that
        wrote a revision on its own would put the store one revision ahead of the
        reconciliation, and the reconciling path would then compare that revision
        against itself and report that nothing ever changed -- leaving a
        ``reviewed`` mark standing over rewritten code with nothing left that
        could ever reopen it. Pinned by
        ``test_review_local_source.py::test_only_the_reconciling_seam_can_write_a_revision``.

        The numbering runs under ``BEGIN IMMEDIATE`` because it is a
        read-then-write: ``MAX(revision_number) + 1`` is only still true for as
        long as no other process commits a revision for this review. ``lc
        review`` and a serving ``lc review --workspace`` are two processes on
        one database, and under the deferred transaction ``_transaction``
        opens, the loser of that race died on a raw ``sqlite3.IntegrityError``
        (``UNIQUE review_id, revision_number``) with its packet and blob
        artifacts already written.

        The tree lookup is repeated *after* taking the write lock. A rival may
        have created the same tree while this caller was serializing artifacts
        before entering this method; the lock + recheck turns that race into the
        same warm-hit result as :meth:`find_revision_by_tree` without relying on
        backend-specific constraint-error transaction semantics.
        """
        self._ensure_ready()
        revision_id = revision.id or new_revision_id()
        created_at = revision.created_at or utc_now()
        winner: ReviewRevision | None = None
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"revision:{revision.review_id}")
            existing = conn.execute(
                "SELECT * FROM review_revisions WHERE review_id = ? AND tree_fingerprint = ?",
                (revision.review_id, revision.tree_fingerprint),
            ).fetchone()
            if existing is not None:
                winner = _row_to_revision(existing)
                record = winner
            else:
                number = revision.revision_number
                if number <= 0:
                    row = conn.execute(
                        "SELECT COALESCE(MAX(revision_number), 0) AS n FROM review_revisions WHERE review_id = ?",
                        (revision.review_id,),
                    ).fetchone()
                    number = int(row["n"]) + 1 if row is not None else 1
                record = ReviewRevision(
                    id=revision_id,
                    review_id=revision.review_id,
                    revision_number=number,
                    range_mode=revision.range_mode,
                    tree_fingerprint=revision.tree_fingerprint,
                    packet_schema_version=revision.packet_schema_version,
                    base_sha=revision.base_sha,
                    head_sha=revision.head_sha,
                    merge_base_sha=revision.merge_base_sha,
                    dirty=revision.dirty,
                    packet_path=revision.packet_path,
                    packet_sha256=revision.packet_sha256,
                    packet_bytes=revision.packet_bytes,
                    degraded=tuple(revision.degraded),
                    provenance_host=revision.provenance_host,
                    provenance_model=revision.provenance_model,
                    provenance_session_id=revision.provenance_session_id,
                    provenance_certainty=revision.provenance_certainty,
                    created_at=created_at,
                    source_fingerprint=revision.source_fingerprint,
                )
            if winner is None:
                conn.execute(
                    """
                    INSERT INTO review_revisions (
                        id, review_id, revision_number, range_mode, base_sha, head_sha,
                        merge_base_sha, dirty, tree_fingerprint, packet_schema_version,
                        packet_path, packet_sha256, packet_bytes, degraded_json,
                        provenance_host, provenance_model, provenance_session_id,
                        provenance_certainty, created_at, source_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.review_id,
                        record.revision_number,
                        record.range_mode,
                        record.base_sha,
                        record.head_sha,
                        record.merge_base_sha,
                        1 if record.dirty else 0,
                        record.tree_fingerprint,
                        record.packet_schema_version,
                        record.packet_path,
                        record.packet_sha256,
                        record.packet_bytes,
                        _dump_tuple(record.degraded),
                        record.provenance_host,
                        record.provenance_model,
                        record.provenance_session_id,
                        record.provenance_certainty,
                        record.created_at,
                        record.source_fingerprint,
                    ),
                )
            if winner is None:
                for unit in units:
                    conn.execute(
                        """
                        INSERT INTO review_units (
                            revision_id, unit_key, kind, path, symbol, ordinal,
                            start_line, end_line, content_fingerprint, fingerprint_method,
                            attention_rank, attention_group, reasons_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.id,
                            unit.unit_key,
                            unit.kind,
                            unit.path,
                            unit.symbol,
                            unit.ordinal,
                            unit.start_line,
                            unit.end_line,
                            unit.content_fingerprint,
                            unit.fingerprint_method,
                            unit.attention_rank,
                            unit.attention_group,
                            _dump_tuple(unit.reasons),
                        ),
                    )
                _insert_activity_event(
                    conn,
                    ReviewActivityEvent(
                        id="",
                        review_id=record.review_id,
                        revision_id=record.id,
                        kind="revision.recorded",
                        actor_id=record.provenance_session_id,
                        actor_type="agent" if record.provenance_host else "unknown",
                        subject_type="revision",
                        subject_id=record.id,
                        summary=f"Revision {record.revision_number} recorded",
                        detail_json=json.dumps(
                            {
                                "base_sha": record.base_sha,
                                "head_sha": record.head_sha,
                                "degraded": list(record.degraded),
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        ),
                        created_at=record.created_at,
                    ),
                )
        if winner is not None:
            self._discard_revision_artifacts(revision.review_id, revision_id)
            return winner
        return record

    def get_revision(self, revision_id: str) -> ReviewRevision | None:
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM review_revisions WHERE id = ?", (revision_id,)).fetchone()
        return None if row is None else _row_to_revision(row)

    def resolve_revision(self, reference: str) -> ReviewRevision | None:
        """Resolve one exact internal id or canonical ``rr/<id>`` public ref."""

        exact = self.get_revision(reference)
        if exact is not None:
            return exact
        if not reference.startswith(_REVISION_REF_PREFIX):
            return None
        revision_id = reference[len(_REVISION_REF_PREFIX) :]
        return self.get_revision(revision_id) if revision_id else None

    def revision_reference(self, revision_id: str) -> str:
        """Return the canonical public ref for one durable Review revision id."""

        return public_revision_reference(revision_id)

    def set_revision_source_fingerprint(self, revision_id: str, fingerprint: str) -> ReviewRevision | None:
        """Backfill the advisory source identity on an existing revision."""

        self._ensure_ready()
        with self._transaction() as conn:
            conn.execute(
                "UPDATE review_revisions SET source_fingerprint = ? WHERE id = ?",
                (fingerprint, revision_id),
            )
            row = conn.execute("SELECT * FROM review_revisions WHERE id = ?", (revision_id,)).fetchone()
        return None if row is None else _row_to_revision(row)

    def update_revision_projection(
        self,
        revision_id: str,
        *,
        packet_payload: bytes,
        packet_schema_version: int,
        degraded: Sequence[str],
        provenance_host: str,
        provenance_model: str,
        provenance_session_id: str,
        provenance_certainty: str,
        units: Sequence[ReviewUnit],
    ) -> ReviewRevision:
        """Enrich analysis metadata without changing the code/target identity.

        Progressive Review startup persists the exact source and its stable
        file/hunk/symbol identities first. Expensive impact analysis may later
        improve ranking, reasons, provenance and packet evidence, but it is not
        allowed to move a target the human could already be reading. Any identity
        mismatch is therefore a hard refusal rather than an in-place rewrite.
        """

        current = self.get_revision(revision_id)
        if current is None:
            raise ValueError(f"no review revision {revision_id!r}")
        stored = {unit.unit_key: unit for unit in self.list_units(revision_id)}
        projected = {unit.unit_key: unit for unit in units}

        def identity(unit: ReviewUnit) -> tuple[object, ...]:
            return (
                unit.kind,
                unit.path,
                unit.symbol,
                unit.ordinal,
                unit.start_line,
                unit.end_line,
                unit.content_fingerprint,
                unit.fingerprint_method,
            )

        if stored.keys() != projected.keys() or any(
            identity(stored[key]) != identity(projected[key]) for key in stored
        ):
            raise ValueError("review enrichment changed target identity; refusing to rewrite a visible revision")

        rel, sha256, size = self.write_packet_artifact(current.review_id, current.id, packet_payload)
        self._ensure_ready()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE review_revisions
                   SET packet_schema_version = ?, packet_path = ?, packet_sha256 = ?, packet_bytes = ?,
                       degraded_json = ?, provenance_host = ?, provenance_model = ?,
                       provenance_session_id = ?, provenance_certainty = ?
                 WHERE id = ?
                """,
                (
                    packet_schema_version,
                    rel,
                    sha256,
                    size,
                    _dump_tuple(degraded),
                    provenance_host,
                    provenance_model,
                    provenance_session_id,
                    provenance_certainty,
                    revision_id,
                ),
            )
            for unit in units:
                conn.execute(
                    """
                    UPDATE review_units
                       SET attention_rank = ?, attention_group = ?, reasons_json = ?
                     WHERE revision_id = ? AND unit_key = ?
                    """,
                    (
                        unit.attention_rank,
                        unit.attention_group,
                        _dump_tuple(unit.reasons),
                        revision_id,
                        unit.unit_key,
                    ),
                )
            row = conn.execute("SELECT * FROM review_revisions WHERE id = ?", (revision_id,)).fetchone()
        if row is None:  # pragma: no cover - row was read immediately before this transaction
            raise RuntimeError("review revision disappeared while it was being enriched")
        return _row_to_revision(row)

    def set_current_revision(self, review_id: str, revision_id: str) -> bool:
        """Point the Review at the source revision that is active *now*.

        Revision numbers are immutable history, not a monotonic source-state
        cursor: an undo may legitimately reactivate revision 1 after revision 2
        was captured. Returns ``True`` only when the pointer actually changed.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT current_revision_id FROM review_sessions WHERE id = ?",
                (review_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"no such review session {review_id!r}")
            if str(row["current_revision_id"] or "") == revision_id:
                return False
            owned = conn.execute(
                "SELECT 1 FROM review_revisions WHERE id = ? AND review_id = ?",
                (revision_id, review_id),
            ).fetchone()
            if owned is None:
                raise ValueError(f"revision {revision_id!r} does not belong to review {review_id!r}")
            conn.execute(
                "UPDATE review_sessions SET current_revision_id = ?, updated_at = ? WHERE id = ?",
                (revision_id, utc_now(), review_id),
            )
        return True

    def latest_revision(self, review_id: str) -> ReviewRevision | None:
        """The source revision active now, with legacy fallback to highest number."""

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT r.*
                  FROM review_sessions AS s
                  JOIN review_revisions AS r ON r.id = s.current_revision_id
                 WHERE s.id = ? AND r.review_id = s.id
                """,
                (review_id,),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT * FROM review_revisions WHERE review_id = ? ORDER BY revision_number DESC LIMIT 1",
                    (review_id,),
                ).fetchone()
        return None if row is None else _row_to_revision(row)

    def find_revision_by_tree(self, review_id: str, tree_fingerprint: str) -> ReviewRevision | None:
        """The idempotency lookup: identical file content is never a new revision."""
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM review_revisions WHERE review_id = ? AND tree_fingerprint = ?",
                (review_id, tree_fingerprint),
            ).fetchone()
        return None if row is None else _row_to_revision(row)

    def find_latest_revision_by_source_fingerprint(
        self, review_id: str, source_fingerprint: str
    ) -> ReviewRevision | None:
        """Find the latest row for exact source written under any analysis recipe."""

        if not source_fingerprint:
            return None
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM review_revisions "
                "WHERE review_id = ? AND source_fingerprint = ? "
                "ORDER BY revision_number DESC LIMIT 1",
                (review_id, source_fingerprint),
            ).fetchone()
        return None if row is None else _row_to_revision(row)

    def list_revisions(self, review_id: str) -> tuple[ReviewRevision, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM review_revisions WHERE review_id = ? ORDER BY revision_number ASC",
                (review_id,),
            ).fetchall()
        return tuple(_row_to_revision(row) for row in rows)

    def list_units(self, revision_id: str) -> tuple[ReviewUnit, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM review_units WHERE revision_id = ?
                 ORDER BY attention_rank ASC, path ASC, ordinal ASC, unit_key ASC
                """,
                (revision_id,),
            ).fetchall()
        return tuple(_row_to_unit(row) for row in rows)

    # ----- marks ---------------------------------------------------------- #

    def set_mark(
        self,
        mark: ReviewMark,
        *,
        event_kind: ReviewMarkEventKind = "judgment",
        event_revision_id: str = "",
        event_reason: str = "",
        previous_unit_key: str = "",
    ) -> ReviewMark:
        """Upsert the live mark and append its transition in one transaction.

        ``review_marks`` remains the hot current projection. ``review_mark_events``
        is the append-only authority for history; neither is reconstructed from
        the other during a write. A crash can therefore leave neither update or
        both, never a current verdict whose provenance vanished.
        """
        self._ensure_ready()
        now = utc_now()
        record = ReviewMark(
            review_id=mark.review_id,
            unit_key=mark.unit_key,
            state=mark.state,
            reviewed_revision_id=mark.reviewed_revision_id,
            content_fingerprint=mark.content_fingerprint,
            reviewer_id=mark.reviewer_id,
            actor_type=mark.actor_type,
            note=mark.note,
            created_at=mark.created_at or now,
            updated_at=mark.updated_at or now,
        )
        with self._transaction() as conn:
            before_row = conn.execute(
                "SELECT * FROM review_marks WHERE review_id = ? AND reviewer_id = ? AND unit_key = ?",
                (record.review_id, record.reviewer_id, record.unit_key),
            ).fetchone()
            conn.execute(
                """
                INSERT INTO review_marks (
                    review_id, reviewer_id, unit_key, state, reviewed_revision_id,
                    content_fingerprint, actor_type, note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_id, reviewer_id, unit_key) DO UPDATE SET
                    state = excluded.state,
                    reviewed_revision_id = excluded.reviewed_revision_id,
                    content_fingerprint = excluded.content_fingerprint,
                    actor_type = excluded.actor_type,
                    note = excluded.note,
                    updated_at = excluded.updated_at
                """,
                (
                    record.review_id,
                    record.reviewer_id,
                    record.unit_key,
                    record.state,
                    record.reviewed_revision_id,
                    record.content_fingerprint,
                    record.actor_type,
                    record.note,
                    record.created_at,
                    record.updated_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM review_marks WHERE review_id = ? AND reviewer_id = ? AND unit_key = ?",
                (record.review_id, record.reviewer_id, record.unit_key),
            ).fetchone()
            stored = record if row is None else _row_to_mark(row)
            before = None if before_row is None else _row_to_mark(before_row)
            event_revision = event_revision_id or stored.reviewed_revision_id
            event = _insert_mark_event(
                conn,
                ReviewMarkEvent(
                    id="",
                    review_id=stored.review_id,
                    reviewer_id=stored.reviewer_id,
                    unit_key=stored.unit_key,
                    revision_id=event_revision,
                    reviewed_revision_id=stored.reviewed_revision_id,
                    event_kind=event_kind,
                    from_state="" if before is None else before.state,
                    to_state=stored.state,
                    content_fingerprint=stored.content_fingerprint,
                    previous_unit_key=previous_unit_key,
                    actor_type=stored.actor_type,
                    note=stored.note,
                    reason=event_reason,
                ),
            )
            activity_kind = cast(
                ReviewActivityKind,
                {
                    "judgment": "mark.judgment",
                    "reconciled": "mark.reconciled",
                    "discarded": "mark.discarded",
                    "migrated": "mark.migrated",
                }[event_kind],
            )
            _insert_activity_event(
                conn,
                ReviewActivityEvent(
                    id="",
                    review_id=stored.review_id,
                    revision_id=event.revision_id,
                    kind=activity_kind,
                    actor_id=stored.reviewer_id,
                    actor_type=stored.actor_type,
                    subject_type="review_target",
                    subject_id=stored.unit_key,
                    summary=f"{stored.reviewer_id}: {event.from_state or 'none'} → {event.to_state}",
                    detail_json=json.dumps(
                        {
                            "mark_event_id": event.id,
                            "previous_unit_key": previous_unit_key,
                            "reason": event_reason,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                ),
            )
        return stored

    def list_marks(self, review_id: str, *, reviewer_id: str = "local") -> tuple[ReviewMark, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM review_marks WHERE review_id = ? AND reviewer_id = ? ORDER BY updated_at DESC",
                (review_id, reviewer_id),
            ).fetchall()
        return tuple(_row_to_mark(row) for row in rows)

    def list_mark_events(
        self,
        review_id: str,
        *,
        reviewer_id: str = "local",
        unit_key: str = "",
    ) -> tuple[ReviewMarkEvent, ...]:
        """Append-only judgment transitions in the order they happened."""

        self._ensure_ready()
        sql = "SELECT * FROM review_mark_events WHERE review_id = ? AND reviewer_id = ?"
        params: list[Any] = [review_id, reviewer_id]
        if unit_key:
            sql += " AND (unit_key = ? OR previous_unit_key = ?)"
            params.extend((unit_key, unit_key))
        sql += " ORDER BY created_at ASC, id ASC"
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(_row_to_mark_event(row) for row in rows)

    def marks_at_revision(
        self,
        review_id: str,
        revision_id: str,
        *,
        reviewer_id: str = "local",
    ) -> tuple[ReviewMark, ...]:
        """Replay the reviewer's projection as this revision's first active era ended.

        The cutoff is the creation time of the next *new* ReviewRevision. This
        includes judgments made while the selected revision was current and
        excludes later events from an undo that revisits an older revision id.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            target = conn.execute(
                "SELECT revision_number FROM review_revisions WHERE id = ? AND review_id = ?",
                (revision_id, review_id),
            ).fetchone()
            if target is None:
                return ()
            nxt = conn.execute(
                """
                SELECT created_at FROM review_revisions
                 WHERE review_id = ? AND revision_number > ?
                 ORDER BY revision_number ASC LIMIT 1
                """,
                (review_id, int(target["revision_number"])),
            ).fetchone()
            sql = "SELECT * FROM review_mark_events WHERE review_id = ? AND reviewer_id = ?"
            params: list[Any] = [review_id, reviewer_id]
            if nxt is not None:
                sql += " AND created_at < ?"
                params.append(str(nxt["created_at"]))
            sql += " ORDER BY created_at ASC, id ASC"
            rows = conn.execute(sql, params).fetchall()

        current: dict[str, ReviewMark] = {}
        for row in rows:
            event = _row_to_mark_event(row)
            if not event.to_state:
                current.pop(event.unit_key, None)
                continue
            current[event.unit_key] = ReviewMark(
                review_id=event.review_id,
                unit_key=event.unit_key,
                state=event.to_state,
                reviewed_revision_id=event.reviewed_revision_id,
                content_fingerprint=event.content_fingerprint,
                reviewer_id=event.reviewer_id,
                actor_type=event.actor_type,
                note=event.note,
                created_at=event.created_at,
                updated_at=event.created_at,
            )
        return tuple(sorted(current.values(), key=lambda mark: mark.unit_key))

    def _record_activity_in_transaction(
        self, conn: sqlite3.Connection, event: ReviewActivityEvent
    ) -> ReviewActivityEvent:
        """Protected extension seam for atomic service-specific activity.

        Hosted compositions may persist private state and append a generic
        Review activity event in the same SQLite transaction without importing
        module-private helpers or duplicating the public event encoding.
        """

        return _insert_activity_event(conn, event)

    def record_activity(self, event: ReviewActivityEvent) -> ReviewActivityEvent:
        self._ensure_ready()
        with self._transaction() as conn:
            return self._record_activity_in_transaction(conn, event)

    def list_activity_events(self, review_id: str, *, limit: int = 500) -> tuple[ReviewActivityEvent, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM review_activity_events
                 WHERE review_id = ?
                 ORDER BY created_at ASC, id ASC LIMIT ?
                """,
                (review_id, max(1, min(limit, 5000))),
            ).fetchall()
        return tuple(_row_to_activity_event(row) for row in rows)

    # ----- reviewer outcomes --------------------------------------------- #

    def record_outcome(
        self,
        review_id: str,
        revision_id: str,
        reviewer_id: str,
        *,
        outcome: ReviewOutcomeKind,
        summary: str = "",
        actor_type: ActorType = "human",
    ) -> ReviewOutcome:
        """Append one reviewer verdict for one immutable revision."""

        self._ensure_ready()
        if outcome not in REVIEW_OUTCOME_KINDS:
            raise ValueError(f"unknown review outcome {outcome!r}")
        reviewer = reviewer_id.strip()
        if not reviewer:
            raise ValueError("review outcome reviewer_id must not be empty")
        note = summary.strip()[:16_384]
        now = utc_now()
        record = ReviewOutcome(
            id=_new_outcome_id(),
            review_id=review_id,
            revision_id=revision_id,
            reviewer_id=reviewer,
            outcome=outcome,
            summary=note,
            created_at=now,
        )
        with self._transaction() as conn:
            revision = conn.execute(
                "SELECT review_id FROM review_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None or str(revision["review_id"]) != review_id:
                raise ValueError("review outcome revision does not belong to this review")
            conn.execute(
                """
                INSERT INTO review_outcomes (
                    id, review_id, revision_id, reviewer_id, outcome, summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.review_id,
                    record.revision_id,
                    record.reviewer_id,
                    record.outcome,
                    record.summary,
                    record.created_at,
                ),
            )
            _insert_activity_event(
                conn,
                ReviewActivityEvent(
                    id="",
                    review_id=review_id,
                    revision_id=revision_id,
                    kind="outcome.recorded",
                    actor_id=reviewer,
                    actor_type=actor_type,
                    subject_type="outcome",
                    subject_id=record.id,
                    summary={
                        "lgtm": f"{reviewer} marked this revision LGTM",
                        "changes_requested": f"{reviewer} requested changes on this revision",
                        "comment": f"{reviewer} published a review without an overall verdict",
                    }[outcome],
                    detail_json=json.dumps(
                        {"outcome": outcome, "summary": note},
                        sort_keys=True,
                    ),
                ),
            )
        return record

    def list_outcomes(self, review_id: str, *, reviewer_id: str = "") -> tuple[ReviewOutcome, ...]:
        """Append-only reviewer verdict history, newest first."""

        self._ensure_ready()
        sql = "SELECT * FROM review_outcomes WHERE review_id = ?"
        params: list[Any] = [review_id]
        if reviewer_id:
            sql += " AND reviewer_id = ?"
            params.append(reviewer_id)
        sql += " ORDER BY created_at DESC, id DESC"
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(_row_to_outcome(row) for row in rows)

    def latest_outcome(self, review_id: str, reviewer_id: str) -> ReviewOutcome | None:
        """Newest verdict by one reviewer, regardless of whether it is stale."""

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM review_outcomes
                 WHERE review_id = ? AND reviewer_id = ?
                 ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (review_id, reviewer_id),
            ).fetchone()
        return None if row is None else _row_to_outcome(row)

    def latest_outcomes(self, review_id: str) -> tuple[ReviewOutcome, ...]:
        """Newest verdict for every reviewer who has ever recorded one."""

        latest: dict[str, ReviewOutcome] = {}
        for row in self.list_outcomes(review_id):
            latest.setdefault(row.reviewer_id, row)
        return tuple(sorted(latest.values(), key=lambda item: item.reviewer_id))

    def clear_mark(
        self,
        review_id: str,
        reviewer_id: str,
        unit_key: str,
        *,
        event_kind: ReviewMarkEventKind = "discarded",
        event_revision_id: str = "",
        event_reason: str = "",
    ) -> None:
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM review_marks WHERE review_id = ? AND reviewer_id = ? AND unit_key = ?",
                (review_id, reviewer_id, unit_key),
            ).fetchone()
            if row is None:
                return
            mark = _row_to_mark(row)
            event = _insert_mark_event(
                conn,
                ReviewMarkEvent(
                    id="",
                    review_id=mark.review_id,
                    reviewer_id=mark.reviewer_id,
                    unit_key=mark.unit_key,
                    revision_id=event_revision_id or mark.reviewed_revision_id,
                    reviewed_revision_id=mark.reviewed_revision_id,
                    event_kind=event_kind,
                    from_state=mark.state,
                    to_state="",
                    content_fingerprint=mark.content_fingerprint,
                    actor_type=mark.actor_type,
                    note=mark.note,
                    reason=event_reason,
                ),
            )
            activity_kind: ReviewActivityKind = "mark.migrated" if event_kind == "migrated" else "mark.discarded"
            _insert_activity_event(
                conn,
                ReviewActivityEvent(
                    id="",
                    review_id=mark.review_id,
                    revision_id=event.revision_id,
                    kind=activity_kind,
                    actor_id=mark.reviewer_id,
                    actor_type=mark.actor_type,
                    subject_type="review_target",
                    subject_id=mark.unit_key,
                    summary=(
                        f"{mark.reviewer_id}: migrated {mark.unit_key}"
                        if event_kind == "migrated"
                        else f"{mark.reviewer_id}: discarded {mark.state} judgment"
                    ),
                    detail_json=json.dumps({"mark_event_id": event.id, "reason": event_reason}, sort_keys=True),
                ),
            )
            conn.execute(
                "DELETE FROM review_marks WHERE review_id = ? AND reviewer_id = ? AND unit_key = ?",
                (review_id, reviewer_id, unit_key),
            )

    # ----- the reviewer's frontier ---------------------------------------- #

    def record_frontier(self, review_id: str, reviewer_id: str, revision_id: str) -> None:
        """Remember that *reviewer_id* has now looked at *revision_id*.

        Called wherever a reviewer records a verdict, and **only** there: a
        frontier advanced by merely running a command is
        :meth:`latest_revision` wearing a different name, and keying "since my
        review" on that is what makes the answer depend on who typed what
        first.

        Durable on purpose. The obvious alternative -- derive it from
        ``max(reviewed_revision_id)`` over the reviewer's marks -- reads a
        frontier out of rows that reconciliation is entitled to *delete*: a
        mark whose unit left the review (a renamed symbol, a deleted file) is
        cleared, and if that was their only mark the reviewer's whole history
        vanishes with it. The next command then says "nothing reviewed yet"
        about code they reviewed, and which command that is depends on typing
        A verdict may be discarded; the fact that a human looked may not.

        **Last verdict wins, and deliberately not the highest
        ``revision_number``.** A revision number counts the distinct trees this
        review has ever seen, in the order it first saw them; it is not a
        timeline the reviewer walked, and a high-water mark over it is a
        high-water mark of nothing they experienced. An undo puts the tree back
        on a revision already on file, and a frontier pinned to the larger
        number then tells a reviewer "you last saw revision 2" underneath a
        header reading revision 1 -- a sentence with no reading -- and reports
        every unit revision 2 happened not to contain as new.

        Nothing is lost by moving backwards. "What changed since I looked?" is
        a question about *content*, and the content the reviewer last looked at
        is the revision they last recorded a verdict against, whatever number
        it was issued. What they saw on the way there is remembered where it
        belongs -- on the marks, and on ``review_discarded_marks`` for the ones
        reconciliation had to delete.

        Silently does nothing for a revision that is not on file.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT id FROM review_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
            if row is None:
                return
            conn.execute(
                """
                INSERT INTO review_frontiers (review_id, reviewer_id, revision_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(review_id, reviewer_id) DO UPDATE SET
                    revision_id = excluded.revision_id,
                    updated_at  = excluded.updated_at
                """,
                (review_id, reviewer_id, revision_id, utc_now()),
            )

    def list_reviewer_ids(self, review_id: str) -> tuple[str, ...]:
        """Every reviewer with durable judgment/frontier state in this Review.

        Hosted capture must reconcile a new revision for *all* humans who have
        reviewed it, not merely for the principal who happened to publish that
        revision. Frontiers are included because reconciliation may legitimately
        delete a reviewer's last live mark while the fact that they reviewed a
        previous revision must remain durable.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT reviewer_id FROM review_frontiers WHERE review_id = ?
                UNION
                SELECT reviewer_id FROM review_marks WHERE review_id = ?
                ORDER BY reviewer_id
                """,
                (review_id, review_id),
            ).fetchall()
        return tuple(str(row["reviewer_id"]) for row in rows if str(row["reviewer_id"]))

    def frontier_revision(self, review_id: str, reviewer_id: str = "local") -> str:
        """The revision id this reviewer last recorded a verdict against, or ``""``.

        ``""`` is the honest answer before their first verdict: a reviewer who
        has recorded nothing has seen no revision, so everything is new to them
        and nothing is stale.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT revision_id FROM review_frontiers WHERE review_id = ? AND reviewer_id = ?",
                (review_id, reviewer_id),
            ).fetchone()
        return "" if row is None else str(row["revision_id"])

    # ----- verdicts reconciliation had to delete --------------------------- #

    def record_discarded_mark(self, record: DiscardedMark) -> DiscardedMark:
        """Keep a verdict whose row is about to be deleted.

        Written *before* :meth:`clear_mark`, so a crash between the two leaves
        the evidence without the verdict rather than neither. Idempotent on
        ``(review, reviewer, unit, content, revision)``: reconciliation is a
        fixed point, and re-running it must not add a second row that a surface
        would read as a second discard.
        """

        self._ensure_ready()
        stored = record if record.discarded_at else replace(record, discarded_at=utc_now())
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO review_discarded_marks (
                    review_id, reviewer_id, unit_key, discarded_revision_id, state,
                    content_fingerprint, reviewed_revision_id, kind, path, symbol,
                    ordinal, start_line, reason, discarded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_id, reviewer_id, unit_key, content_fingerprint, discarded_revision_id)
                DO NOTHING
                """,
                (
                    stored.review_id,
                    stored.reviewer_id,
                    stored.unit_key,
                    stored.discarded_revision_id,
                    stored.state,
                    stored.content_fingerprint,
                    stored.reviewed_revision_id,
                    stored.kind,
                    stored.path,
                    stored.symbol,
                    stored.ordinal,
                    stored.start_line,
                    stored.reason,
                    stored.discarded_at,
                ),
            )
        return stored

    def discarded_marks_on(self, revision_id: str, *, reviewer_id: str = "local") -> tuple[DiscardedMark, ...]:
        """Every verdict deleted while reconciling onto *revision_id*.

        A property of the revision rather than of an invocation, so a surface
        that opens later can still say what was lost. Ordered by unit key so
        two readers of the same revision see the same list.

        The single-revision primitive, and **not** what a surface should ask.
        Reconciliation writes the row on whichever revision it happened to be
        standing on, while every "what happened since I looked" list is scoped
        to the reviewer's baseline; asking this one narrows the answer to a
        single revision and the verdict vanishes from the next one. Surfaces
        use :meth:`discarded_marks_since`.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM review_discarded_marks WHERE discarded_revision_id = ? AND reviewer_id = ?"
                " ORDER BY unit_key",
                (revision_id, reviewer_id),
            ).fetchall()
        return tuple(_row_to_discarded(row) for row in rows)

    def discarded_marks_since(
        self,
        review_id: str,
        *,
        reviewer_id: str = "local",
        after_revision_number: int,
        absent_from_revision_id: str,
    ) -> tuple[DiscardedMark, ...]:
        """The reviewer's destroyed verdicts that are still news, on any revision.

        The scope of every "since I reviewed" list is the window between the
        reviewer's baseline -- the revision they last recorded a verdict against
        -- and the revision in front of them. Reading discards one revision at a
        time (:meth:`discarded_marks_on`) is the mismatch that made a destroyed
        approval last exactly one command: reconciliation writes the row on
        revision 2, the reviewer's baseline stays on revision 1, and from
        revision 3 onwards the frontier still names the unit as gone while every
        surface answers "discarded: []" underneath it -- ``LEFT THE REVIEW``
        then printing "nothing here to re-read" over an approval this tool
        deleted. Same window as the frontier, so the two cannot disagree.

        Two conditions, and both are the reviewer's own position rather than the
        tree's history:

        * **Still absent.** *absent_from_revision_id* is the revision being
          reported on; a unit that is back in it has something to look at again,
          and an undo that restores a symbol byte for byte must not keep
          announcing a loss the reviewer can now see undone. (The verdict stays
          discarded either way -- :func:`sources.local.unseen_units` is what
          keeps the restored unit honestly "not new and not reviewed".)
        * **The frontier has not passed it.** *after_revision_number* is the
          baseline's ``revision_number``; 0 -- "nothing reviewed yet" -- lets
          every row through. **A discard stops being news the moment the
          reviewer records a verdict against a revision at or after the one that
          destroyed it**: recording a verdict is the act of having taken this
          revision in, the banner was on screen when they did, and repeating it
          forever would make it furniture. That is the whole rule; nothing here
          expires on a clock or a count of revisions.

        One row per unit -- the newest discard wins -- so a verdict does not
        print twice merely because the window happens to span two revisions.
        Ordered by unit key, so two readers of the same window see one list.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT d.* FROM review_discarded_marks AS d
                  JOIN review_revisions AS r ON r.id = d.discarded_revision_id
                 WHERE d.review_id = ?
                   AND d.reviewer_id = ?
                   AND r.revision_number > ?
                   AND NOT EXISTS (
                         SELECT 1 FROM review_units AS u
                          WHERE u.revision_id = ? AND u.unit_key = d.unit_key
                       )
                 ORDER BY d.unit_key ASC, r.revision_number DESC, d.discarded_at DESC
                """,
                (review_id, reviewer_id, after_revision_number, absent_from_revision_id),
            ).fetchall()
        # The ORDER BY already groups a unit's rows together, newest first, so
        # the first row seen for a key is the one to keep and insertion order is
        # unit-key order.
        newest: dict[str, DiscardedMark] = {}
        for row in rows:
            record = _row_to_discarded(row)
            newest.setdefault(record.unit_key, record)
        return tuple(newest.values())

    def attested_fingerprints(self, review_id: str, *, reviewer_id: str = "local") -> frozenset[tuple[str, str]]:
        """``(unit_key, content_fingerprint)`` for every unit this reviewer has looked at.

        The question this answers is "have you seen this content", not "did you
        approve it", so every mark counts regardless of its state, and so does
        every verdict reconciliation later deleted. That second half is the
        point: without it, content a reviewer approved, an agent renamed away
        and an undo brought back reads as work they have never laid eyes on.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            marks = conn.execute(
                "SELECT unit_key, content_fingerprint FROM review_marks WHERE review_id = ? AND reviewer_id = ?",
                (review_id, reviewer_id),
            ).fetchall()
            discarded = conn.execute(
                "SELECT unit_key, content_fingerprint FROM review_discarded_marks"
                " WHERE review_id = ? AND reviewer_id = ?",
                (review_id, reviewer_id),
            ).fetchall()
        return frozenset((str(row["unit_key"]), str(row["content_fingerprint"])) for row in (*marks, *discarded))

    # ----- reviewer-authored source proposals ---------------------------- #

    def add_change_proposal(self, proposal: ReviewChangeProposal) -> ReviewChangeProposal:
        self._ensure_ready()
        if proposal.state not in REVIEW_CHANGE_PROPOSAL_STATES:
            raise ValueError(f"unknown change proposal state {proposal.state!r}")
        if not proposal.path or proposal.start_line < 1 or proposal.end_line < proposal.start_line:
            raise ValueError("change proposal requires a valid source range")
        if not proposal.base_file_sha256:
            raise ValueError("change proposal requires a base file identity")
        now = utc_now()
        record = replace(
            proposal,
            id=proposal.id or new_change_proposal_id(),
            created_at=proposal.created_at or now,
            updated_at=proposal.updated_at or now,
        )
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO review_change_proposals (
                    id, review_id, base_revision_id, path, start_line, end_line,
                    original_text, replacement_text, patch_text, base_file_sha256,
                    state, target_unit_key, annotation_id, intent, conflict_reason,
                    created_by, created_at, updated_at, applied_at,
                    applied_source_fingerprint, result_revision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.review_id,
                    record.base_revision_id,
                    record.path,
                    record.start_line,
                    record.end_line,
                    record.original_text,
                    record.replacement_text,
                    record.patch_text,
                    record.base_file_sha256,
                    record.state,
                    record.target_unit_key,
                    record.annotation_id,
                    record.intent,
                    record.conflict_reason,
                    record.created_by,
                    record.created_at,
                    record.updated_at,
                    record.applied_at,
                    record.applied_source_fingerprint,
                    record.result_revision_id,
                ),
            )
        return record

    def get_change_proposal(self, proposal_id: str) -> ReviewChangeProposal | None:
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM review_change_proposals WHERE id = ?", (proposal_id,)).fetchone()
        return _row_to_change_proposal(row) if row is not None else None

    def list_change_proposals(self, review_id: str) -> tuple[ReviewChangeProposal, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM review_change_proposals WHERE review_id = ? ORDER BY created_at ASC, id ASC",
                (review_id,),
            ).fetchall()
        return tuple(_row_to_change_proposal(row) for row in rows)

    def update_change_proposal(
        self,
        proposal_id: str,
        *,
        state: str | None = None,
        conflict_reason: str | None = None,
        applied_at: str | None = None,
        applied_source_fingerprint: str | None = None,
        result_revision_id: str | None = None,
    ) -> ReviewChangeProposal | None:
        self._ensure_ready()
        if state is not None and state not in REVIEW_CHANGE_PROPOSAL_STATES:
            raise ValueError(f"unknown change proposal state {state!r}")
        fields: dict[str, object] = {"updated_at": utc_now()}
        if state is not None:
            fields["state"] = state
        if conflict_reason is not None:
            fields["conflict_reason"] = conflict_reason
        if applied_at is not None:
            fields["applied_at"] = applied_at
        if applied_source_fingerprint is not None:
            fields["applied_source_fingerprint"] = applied_source_fingerprint
        if result_revision_id is not None:
            fields["result_revision_id"] = result_revision_id
        assignments = ", ".join(f"{key} = ?" for key in fields)
        with self._transaction() as conn:
            conn.execute(
                f"UPDATE review_change_proposals SET {assignments} WHERE id = ?",
                (*fields.values(), proposal_id),
            )
            row = conn.execute("SELECT * FROM review_change_proposals WHERE id = ?", (proposal_id,)).fetchone()
        return _row_to_change_proposal(row) if row is not None else None

    def link_applied_change_proposals(
        self,
        review_id: str,
        *,
        source_fingerprint: str,
        result_revision_id: str,
    ) -> tuple[ReviewChangeProposal, ...]:
        """Bind source writes to the immutable revision that captured exactly them."""

        self._ensure_ready()
        if not source_fingerprint:
            return ()
        now = utc_now()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT id FROM review_change_proposals
                 WHERE review_id = ? AND state = 'applied' AND result_revision_id = ''
                   AND applied_source_fingerprint = ?
                """,
                (review_id, source_fingerprint),
            ).fetchall()
            ids = [str(row["id"]) for row in rows]
            for proposal_id in ids:
                conn.execute(
                    "UPDATE review_change_proposals SET result_revision_id = ?, updated_at = ? WHERE id = ?",
                    (result_revision_id, now, proposal_id),
                )
            linked = [
                conn.execute("SELECT * FROM review_change_proposals WHERE id = ?", (proposal_id,)).fetchone()
                for proposal_id in ids
            ]
        return tuple(_row_to_change_proposal(row) for row in linked if row is not None)

    # ----- annotations ---------------------------------------------------- #

    def _annotation_search_insert(self, conn: sqlite3.Connection, annotation_id: str, body: str, path: str) -> None:
        row = conn.execute("SELECT rowid FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
        if row is not None:
            _fts_insert(conn, int(row["rowid"]), body, path)

    def _annotation_search_replace(
        self,
        conn: sqlite3.Connection,
        annotation_id: str,
        old_body: str,
        old_path: str,
        new_body: str,
        new_path: str,
    ) -> None:
        row = conn.execute("SELECT rowid FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
        if row is None:
            return
        rowid = int(row["rowid"] or 0)
        _fts_delete(conn, rowid, old_body, old_path)
        _fts_insert(conn, rowid, new_body, new_path)

    def _annotation_search_delete(self, conn: sqlite3.Connection, annotation_id: str, body: str, path: str) -> None:
        row = conn.execute("SELECT rowid FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
        if row is not None:
            _fts_delete(conn, int(row["rowid"]), body, path)

    def add_annotation(self, annotation: Annotation) -> Annotation:
        """Insert an annotation and index its body for search.

        ``unit_key`` and ``path`` are denormalised out of the anchor onto the
        row so the two hot lookups (everything on this path, everything on this
        unit) are index scans instead of a JSON extract per row.
        """
        self._ensure_ready()
        validate_annotation_semantics(annotation)
        now = utc_now()
        record = Annotation(
            id=annotation.id or new_annotation_id(),
            review_id=annotation.review_id,
            revision_id=annotation.revision_id,
            anchor=annotation.anchor,
            body=annotation.body,
            kind=annotation.kind,
            state=annotation.state,
            parent_id=annotation.parent_id,
            created_by=annotation.created_by,
            created_by_actor=annotation.created_by_actor,
            source=annotation.source,
            source_id=annotation.source_id,
            title=annotation.title,
            evidence=tuple(annotation.evidence),
            confidence=annotation.confidence,
            author_response=annotation.author_response,
            author_response_source_id=annotation.author_response_source_id,
            author_response_at=annotation.author_response_at,
            resolved_revision_id=annotation.resolved_revision_id,
            turn_owner_kind=(
                "author"
                if annotation.kind == "request_change"
                and not annotation.parent_id
                and annotation.turn_owner_kind == "none"
                else annotation.turn_owner_kind
            ),
            turn_owner_id=annotation.turn_owner_id,
            anchor_method=annotation.anchor_method,
            anchor_detail=annotation.anchor_detail,
            created_at=annotation.created_at or now,
            updated_at=annotation.updated_at or now,
        )
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO annotations (
                    id, review_id, revision_id, parent_id, anchor_json, unit_key, path,
                    kind, body, state, created_by, created_by_actor,
                    source, source_id, title, evidence_json, confidence,
                    author_response, author_response_source_id, author_response_at,
                    resolved_revision_id, anchor_method, anchor_detail,
                    turn_owner_kind, turn_owner_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.review_id,
                    record.revision_id,
                    record.parent_id,
                    _anchor_to_json(record.anchor),
                    record.anchor.unit_key,
                    record.anchor.path,
                    record.kind,
                    record.body,
                    record.state,
                    record.created_by,
                    record.created_by_actor,
                    record.source,
                    record.source_id,
                    record.title,
                    json.dumps(list(record.evidence), ensure_ascii=False),
                    record.confidence,
                    record.author_response,
                    record.author_response_source_id,
                    record.author_response_at,
                    record.resolved_revision_id,
                    record.anchor_method,
                    record.anchor_detail,
                    record.turn_owner_kind,
                    record.turn_owner_id,
                    record.created_at,
                    record.updated_at,
                ),
            )
            self._annotation_search_insert(conn, record.id, record.body, record.anchor.path)
            # The first rung of the audit trail, written in the same transaction
            # as the comment itself. Creation is an anchoring like any other, and
            # a history that starts at the first *refresh* cannot show a reader
            # where the comment originally pointed -- nor let anything work out
            # what line it moved *from*. Every door that creates an annotation
            # goes through here, so no door can open one without its origin.
            conn.execute(
                """
                INSERT INTO annotation_anchor_events (
                    id, annotation_id, revision_id, method, status, detail, anchor_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_anchor_event_id(),
                    record.id,
                    record.revision_id,
                    record.anchor_method,
                    "unresolved" if record.anchor_method == "unresolved" else "unchanged",
                    record.anchor_detail,
                    _anchor_to_json(record.anchor),
                    record.created_at,
                ),
            )
            _insert_annotation_version(
                conn,
                AnnotationVersion(
                    id="",
                    annotation_id=record.id,
                    review_id=record.review_id,
                    revision_id=record.revision_id,
                    version_number=0,
                    body=record.body,
                    kind=record.kind,
                    state=record.state,
                    author_response=record.author_response,
                    author_response_source_id=record.author_response_source_id,
                    author_response_at=record.author_response_at,
                    resolved_revision_id=record.resolved_revision_id,
                    changed_by=record.created_by,
                    changed_by_actor=record.created_by_actor,
                    change_kind="created",
                    turn_owner_kind=record.turn_owner_kind,
                    turn_owner_id=record.turn_owner_id,
                    created_at=record.created_at,
                ),
            )
            _insert_activity_event(
                conn,
                ReviewActivityEvent(
                    id="",
                    review_id=record.review_id,
                    revision_id=record.revision_id,
                    kind="annotation.created",
                    actor_id=record.created_by,
                    actor_type=record.created_by_actor,
                    subject_type="annotation",
                    subject_id=record.id,
                    summary=f"{record.created_by}: {record.kind}",
                    detail_json=json.dumps({"path": record.anchor.path, "parent_id": record.parent_id}, sort_keys=True),
                    created_at=record.created_at,
                ),
            )

            # A human reply changes the semantic state of the whole thread even
            # though the root body itself did not change. Delivery binds to the
            # root annotation version, so advance that version in the same
            # transaction as the reply. This makes a follow-up such as "still
            # broken" publishable exactly once and prevents an older author
            # "addressed" claim from staying current after the reviewer speaks.
            if record.source == "human" and record.parent_id:
                root_row = conn.execute(
                    "SELECT * FROM annotations WHERE id = ?",
                    (record.parent_id,),
                ).fetchone()
                seen: set[str] = set()
                while root_row is not None and str(root_row["parent_id"]):
                    root_id = str(root_row["id"])
                    if root_id in seen:
                        raise RuntimeError(f"annotation thread cycle at {root_id}")
                    seen.add(root_id)
                    root_row = conn.execute(
                        "SELECT * FROM annotations WHERE id = ?",
                        (str(root_row["parent_id"]),),
                    ).fetchone()
                if (
                    root_row is not None
                    and str(root_row["review_id"]) == record.review_id
                    and str(root_row["source"]) == "human"
                ):
                    root_id = str(root_row["id"])
                    next_state = "open" if str(root_row["state"]) == "resolved" else str(root_row["state"])
                    resolved_revision_id = "" if next_state == "open" else str(root_row["resolved_revision_id"])
                    conn.execute(
                        """
                        UPDATE annotations
                        SET state = ?,
                            resolved_revision_id = ?,
                            author_response = 'none',
                            author_response_source_id = '',
                            author_response_at = '',
                            turn_owner_kind = 'author',
                            turn_owner_id = '',
                            updated_at = ?
                        WHERE id = ?
                        """,
                        (next_state, resolved_revision_id, record.created_at, root_id),
                    )
                    refreshed_root_row = conn.execute(
                        "SELECT * FROM annotations WHERE id = ?",
                        (root_id,),
                    ).fetchone()
                    if refreshed_root_row is None:  # pragma: no cover - same transaction
                        raise RuntimeError(f"annotation root {root_id!r} disappeared while adding a reply")
                    root = _row_to_annotation(refreshed_root_row)
                    _insert_annotation_version(
                        conn,
                        AnnotationVersion(
                            id="",
                            annotation_id=root.id,
                            review_id=root.review_id,
                            revision_id=record.revision_id,
                            version_number=0,
                            body=root.body,
                            kind=root.kind,
                            state=root.state,
                            author_response=root.author_response,
                            author_response_source_id=root.author_response_source_id,
                            author_response_at=root.author_response_at,
                            resolved_revision_id=root.resolved_revision_id,
                            changed_by=record.created_by,
                            changed_by_actor=record.created_by_actor,
                            change_kind="thread_reply",
                            turn_owner_kind=root.turn_owner_kind,
                            turn_owner_id=root.turn_owner_id,
                            created_at=record.created_at,
                        ),
                    )
                    _insert_activity_event(
                        conn,
                        ReviewActivityEvent(
                            id="",
                            review_id=root.review_id,
                            revision_id=record.revision_id,
                            kind="annotation.thread_reply",
                            actor_id=record.created_by,
                            actor_type=record.created_by_actor,
                            subject_type="annotation",
                            subject_id=root.id,
                            summary=f"{record.created_by}: replied",
                            detail_json=json.dumps({"reply_id": record.id, "path": root.anchor.path}, sort_keys=True),
                            created_at=record.created_at,
                        ),
                    )
        return record

    def update_annotation(
        self,
        annotation_id: str,
        history: AnnotationHistoryContext | None = None,
        /,
        **fields: object,
    ) -> Annotation | None:
        """Patch an annotation; returns the stored row, or ``None`` if absent.

        ``anchor=<AnnotationAnchor>`` is accepted and rewrites ``anchor_json``
        plus the denormalised ``unit_key``/``path``, so a relocation can never
        leave the row's index columns describing the old position.
        """
        self._ensure_ready()
        if fields.get("state") in {"resolved", "obsolete"}:
            # Terminal reviewer states own no next turn. Keep this invariant in
            # the store so local, hosted and reconciliation callers cannot drift.
            fields["turn_owner_kind"] = "none"
            fields["turn_owner_id"] = ""
        allowed = set(_ANNOTATION_UPDATABLE) | {"anchor"}
        unknown = sorted(set(fields) - allowed)
        if unknown:
            raise ValueError(f"unknown annotations column(s): {', '.join(unknown)}")
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM annotations WHERE id = ?",
                (annotation_id,),
            ).fetchone()
            if row is None:
                return None
            if history is not None and history.expected_version is not None:
                version_row = conn.execute(
                    "SELECT COALESCE(MAX(version_number), 0) AS n FROM annotation_versions WHERE annotation_id = ?",
                    (annotation_id,),
                ).fetchone()
                current_version = 0 if version_row is None else int(version_row["n"])
                if current_version != history.expected_version:
                    raise AnnotationVersionConflict(
                        f"annotation {annotation_id} is version {current_version}, expected {history.expected_version}"
                    )
            if fields:
                assignments: list[str] = []
                params: list[Any] = []
                for name in fields:
                    if name == "anchor":
                        anchor = fields[name]
                        if not isinstance(anchor, AnnotationAnchor):
                            raise ValueError("anchor must be an AnnotationAnchor")
                        assignments.extend(("anchor_json = ?", "unit_key = ?", "path = ?"))
                        params.extend((_anchor_to_json(anchor), anchor.unit_key, anchor.path))
                        continue
                    assignments.append(f"{name} = ?")
                    params.append(fields[name])
                params.append(utc_now())
                params.append(annotation_id)
                conn.execute(
                    f"UPDATE annotations SET {', '.join(assignments)}, updated_at = ? WHERE id = ?",
                    params,
                )
                updated = conn.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
                if updated is not None:
                    # The FTS table is contentless, so its rows are maintained by
                    # replaying the old values into a 'delete' command -- there is
                    # no UPDATE and no plain DELETE for one.
                    self._annotation_search_replace(
                        conn,
                        annotation_id,
                        str(row["body"]),
                        str(row["path"]),
                        str(updated["body"]),
                        str(updated["path"]),
                    )
                    semantic = {
                        "body",
                        "kind",
                        "state",
                        "author_response",
                        "author_response_source_id",
                        "author_response_at",
                        "turn_owner_kind",
                        "turn_owner_id",
                    }
                    changed_semantic = any(name in fields and row[name] != updated[name] for name in semantic)
                    if changed_semantic:
                        current_revision = history.revision_id if history is not None else ""
                        if not current_revision:
                            latest = conn.execute(
                                "SELECT id FROM review_revisions WHERE review_id = ? ORDER BY revision_number DESC LIMIT 1",
                                (str(updated["review_id"]),),
                            ).fetchone()
                            current_revision = "" if latest is None else str(latest["id"])
                        resolved_kind: AnnotationVersionKind
                        if history is not None and history.change_kind is not None:
                            resolved_kind = history.change_kind
                        elif "author_response" in fields:
                            resolved_kind = "author_response"
                        elif "state" in fields and all(name not in fields for name in ("body", "kind")):
                            resolved_kind = "state_changed"
                        else:
                            resolved_kind = "edited"
                        version = _insert_annotation_version(
                            conn,
                            AnnotationVersion(
                                id="",
                                annotation_id=str(updated["id"]),
                                review_id=str(updated["review_id"]),
                                revision_id=current_revision,
                                version_number=0,
                                body=str(updated["body"]),
                                kind=str(updated["kind"]),  # type: ignore[arg-type]
                                state=str(updated["state"]),  # type: ignore[arg-type]
                                author_response=str(updated["author_response"]),  # type: ignore[arg-type]
                                author_response_source_id=str(updated["author_response_source_id"]),
                                author_response_at=str(updated["author_response_at"]),
                                resolved_revision_id=str(updated["resolved_revision_id"]),
                                changed_by=history.changed_by if history is not None else "",
                                changed_by_actor=history.changed_by_actor if history is not None else "unknown",
                                change_kind=resolved_kind,
                                turn_owner_kind=str(updated["turn_owner_kind"]),  # type: ignore[arg-type]
                                turn_owner_id=str(updated["turn_owner_id"]),
                            ),
                        )
                        activity_kind = cast(
                            ReviewActivityKind,
                            {
                                "edited": "annotation.updated",
                                "state_changed": "annotation.state_changed",
                                "author_response": "annotation.author_response",
                                "thread_reply": "annotation.thread_reply",
                                "created": "annotation.created",
                            }[resolved_kind],
                        )
                        _insert_activity_event(
                            conn,
                            ReviewActivityEvent(
                                id="",
                                review_id=version.review_id,
                                revision_id=version.revision_id,
                                kind=activity_kind,
                                actor_id=history.changed_by if history is not None else "",
                                actor_type=history.changed_by_actor if history is not None else "unknown",
                                subject_type="annotation",
                                subject_id=version.annotation_id,
                                summary=f"Comment {resolved_kind.replace('_', ' ')}",
                                detail_json=json.dumps(
                                    {
                                        "annotation_version_id": version.id,
                                        "turn_owner_kind": version.turn_owner_kind,
                                        "turn_owner_id": version.turn_owner_id,
                                    },
                                    sort_keys=True,
                                ),
                            ),
                        )
                    row = updated
        return _row_to_annotation(row)

    def list_annotations(self, review_id: str, *, state: str | None = None) -> tuple[Annotation, ...]:
        self._ensure_ready()
        sql = "SELECT * FROM annotations WHERE review_id = ?"
        params: list[Any] = [review_id]
        if state:
            sql += " AND state = ?"
            params.append(state)
        sql += " ORDER BY created_at ASC"
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(_row_to_annotation(row) for row in rows)

    def list_annotations_at_revision(self, review_id: str, revision_id: str) -> tuple[Annotation, ...]:
        """Comments as the selected revision's first active era ended.

        Semantic fields replay from ``annotation_versions`` and geometry replays
        independently from ``annotation_anchor_events``. The cutoff is the next
        newly-created ReviewRevision, so a later undo that revisits this revision
        cannot rewrite what its original historical view says happened.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            target = conn.execute(
                "SELECT revision_number, created_at FROM review_revisions WHERE id = ? AND review_id = ?",
                (revision_id, review_id),
            ).fetchone()
            if target is None:
                return ()
            nxt = conn.execute(
                """
                SELECT created_at FROM review_revisions
                 WHERE review_id = ? AND revision_number > ?
                 ORDER BY revision_number ASC LIMIT 1
                """,
                (review_id, int(target["revision_number"])),
            ).fetchone()
            cutoff = "" if nxt is None else str(nxt["created_at"])
            sql = "SELECT * FROM annotations WHERE review_id = ?"
            params: list[Any] = [review_id]
            if cutoff:
                sql += " AND created_at < ?"
                params.append(cutoff)
            sql += " ORDER BY created_at ASC"
            rows = conn.execute(sql, params).fetchall()
            out: list[Annotation] = []
            for row in rows:
                annotation = _row_to_annotation(row)
                version_sql = "SELECT * FROM annotation_versions WHERE annotation_id = ?"
                version_params: list[Any] = [annotation.id]
                if cutoff:
                    version_sql += " AND created_at < ?"
                    version_params.append(cutoff)
                version_sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
                version_row = conn.execute(version_sql, version_params).fetchone()
                if version_row is not None:
                    version = _row_to_annotation_version(version_row)
                    annotation = replace(
                        annotation,
                        body=version.body,
                        kind=version.kind,
                        state=version.state,
                        author_response=version.author_response,
                        author_response_source_id=version.author_response_source_id,
                        author_response_at=version.author_response_at,
                        resolved_revision_id=version.resolved_revision_id,
                        turn_owner_kind=version.turn_owner_kind,
                        turn_owner_id=version.turn_owner_id,
                    )

                event_sql = "SELECT method, detail, anchor_json FROM annotation_anchor_events WHERE annotation_id = ?"
                event_params: list[Any] = [annotation.id]
                if cutoff:
                    event_sql += " AND created_at < ?"
                    event_params.append(cutoff)
                event_sql += " ORDER BY created_at DESC, id DESC LIMIT 1"
                event = conn.execute(event_sql, event_params).fetchone()
                if event is not None:
                    raw_method = str(event["method"] or annotation.anchor_method)
                    anchor_method = cast(AnchorMethod, raw_method if raw_method in ANCHOR_METHODS else "unresolved")
                    annotation = replace(
                        annotation,
                        anchor=_anchor_from_json(str(event["anchor_json"] or "{}")),
                        anchor_method=anchor_method,
                        anchor_detail=str(event["detail"] or ""),
                    )
                out.append(annotation)
        return tuple(out)

    def list_annotation_versions(self, annotation_id: str) -> tuple[AnnotationVersion, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM annotation_versions WHERE annotation_id = ? ORDER BY version_number ASC",
                (annotation_id,),
            ).fetchall()
        return tuple(_row_to_annotation_version(row) for row in rows)

    def annotation_version_number(self, annotation_id: str) -> int:
        """Latest semantic version number, or 0 when the annotation is absent."""

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(version_number), 0) AS n FROM annotation_versions WHERE annotation_id = ?",
                (annotation_id,),
            ).fetchone()
        return 0 if row is None else int(row["n"])

    def get_annotation(self, annotation_id: str) -> Annotation | None:
        """One annotation by id, or ``None``.

        Needed because ``PATCH /api/annotations/{id}`` is addressed by the
        annotation alone: the route has to load the row before it can decide
        whether the caller is allowed to touch the review it belongs to.
        """
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM annotations WHERE id = ?", (annotation_id,)).fetchone()
        return None if row is None else _row_to_annotation(row)

    def get_annotation_by_source(self, review_id: str, source: str, source_id: str) -> Annotation | None:
        """Return one external signal already ingested under its stable source id."""

        self._ensure_ready()
        if not source_id:
            return None
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM annotations
                 WHERE review_id = ? AND source = ? AND source_id = ?
                 ORDER BY created_at ASC LIMIT 1
                """,
                (review_id, source, source_id),
            ).fetchone()
        return None if row is None else _row_to_annotation(row)

    def delete_annotation(self, annotation_id: str) -> int:
        """Delete an annotation and every reply beneath it; return the count.

        Threads cascade in Python rather than through a self-referential foreign
        key, because ``parent_id`` defaults to ``''`` for a root comment and
        SQLite would have to find a row with that id to satisfy the constraint.
        Making the column nullable to buy a database-level cascade would trade a
        readable schema for a delete this method does in one recursive pass.

        The anchor events and deliveries under each deleted annotation *do*
        cascade in SQLite: they carry real foreign keys to ``annotations(id)``
        and ``PRAGMA foreign_keys`` is on.
        """
        self._ensure_ready()
        with self._transaction() as conn:
            doomed: list[Any] = []
            frontier = [annotation_id]
            seen: set[str] = set()
            while frontier:
                current = frontier.pop()
                if current in seen:
                    continue
                seen.add(current)
                row = conn.execute(
                    "SELECT * FROM annotations WHERE id = ?",
                    (current,),
                ).fetchone()
                if row is None:
                    continue
                doomed.append(row)
                frontier.extend(
                    str(child["id"])
                    for child in conn.execute("SELECT id FROM annotations WHERE parent_id = ?", (current,)).fetchall()
                )
            for row in doomed:
                self._annotation_search_delete(conn, str(row["id"]), str(row["body"]), str(row["path"]))
                conn.execute("DELETE FROM annotations WHERE id = ?", (str(row["id"]),))
        return len(doomed)

    def search_annotations(self, review_id: str, query: str, *, limit: int = 50) -> tuple[Annotation, ...]:
        """Free-text search over annotation bodies within one review.

        Uses the shared ``_build_fts_prefix_query`` so a half-typed word still
        matches, the same behaviour trace and playbook search already have. A
        malformed FTS expression returns nothing rather than raising -- a search
        box is not a place to surface a SQL error.
        """
        self._ensure_ready()
        text = query.strip()
        if not text:
            return ()
        match = self._build_fts_prefix_query(text)
        with self._transaction() as conn:
            try:
                rows = conn.execute(
                    """
                    SELECT annotations.* FROM annotations_fts
                      JOIN annotations ON annotations.rowid = annotations_fts.rowid
                     WHERE annotations_fts MATCH ? AND annotations.review_id = ?
                     ORDER BY annotations_fts.rank
                     LIMIT ?
                    """,
                    (match, review_id, max(1, limit)),
                ).fetchall()
            except sqlite3.OperationalError:
                return ()
        return tuple(_row_to_annotation(row) for row in rows)

    def record_anchor_event(
        self,
        annotation_id: str,
        revision_id: str,
        method: str,
        status: str,
        detail: str,
        anchor: AnnotationAnchor | None,
    ) -> None:
        """Append one anchor-resolution attempt.

        Append-only and never pruned with the annotation still alive: an
        annotation that moved without a recorded rung is a move nobody can
        audit, which is the failure mode this table exists to make impossible.
        """
        self._ensure_ready()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO annotation_anchor_events (
                    id, annotation_id, revision_id, method, status, detail, anchor_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _new_anchor_event_id(),
                    annotation_id,
                    revision_id,
                    method,
                    status,
                    detail,
                    "{}" if anchor is None else _anchor_to_json(anchor),
                    utc_now(),
                ),
            )

    def annotations_anchored_at(self, revision_id: str) -> frozenset[str]:
        """Annotations whose **current** position was derived from *revision_id*.

        The idempotency key for re-anchoring, and the distinction it draws is
        the whole point: an annotation qualifies only when its *latest* anchor
        event -- across every revision -- is one recorded against this
        revision. Then, and only then, is the row's stored geometry already an
        answer about this content, and re-deriving it would change nothing but
        the audit table's size.

        The predecessor of this method asked a weaker question -- "is there
        *any* event on this revision?" -- and that is not the same question. A
        revert makes ``find_revision_by_tree`` return an *older* revision that
        the annotation already carries an event for (its own origin event, if
        the comment was written on it), so re-anchoring was skipped and the row
        kept the geometry of a *different* revision: a comment pointing at a
        line and a function name that exist nowhere in the tree, and an orphan
        reported for text sitting untouched at its original line. Skipping a
        duplicate audit row is not the same as skipping re-anchoring.

        Ordered by ``(created_at, id)`` -- ids are UUIDv7, so both components
        move forward together and ties inside one transaction still resolve.
        """
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT e.annotation_id AS annotation_id
                  FROM annotation_anchor_events AS e
                 WHERE e.revision_id = ?
                   AND NOT EXISTS (
                       SELECT 1 FROM annotation_anchor_events AS later
                        WHERE later.annotation_id = e.annotation_id
                          AND (later.created_at > e.created_at
                               OR (later.created_at = e.created_at AND later.id > e.id))
                   )
                """,
                (revision_id,),
            ).fetchall()
        return frozenset(str(row["annotation_id"]) for row in rows)

    def anchor_moves_on(self, revision_id: str) -> tuple[AnchorMove, ...]:
        """How every comment came to sit where it does on *revision_id*.

        A property of the revision, read back out of the audit table, and
        deliberately **not** "what this invocation just did". Which command
        happened to record the revision is an accident of typing order, and a
        ``COMMENT ANCHORS`` block that appears for whoever got there first is a
        report about the tool rather than about the code.

        ``from_line`` comes from the annotation's previous event, so the report
        reads the same whether it is rendered the moment the anchor moved or a
        week later. Never raises: a row whose anchor JSON was corrupted by hand
        degrades to line ``0`` rather than taking down the listing.

        **One move per annotation, and it is the annotation's *last* event on
        this revision.** A revision can carry two events for the same comment --
        the origin event written when the comment was created on it, and a
        later re-resolution recorded when the tree came back to that same
        content after an edit and an undo. Replaying the earlier one would put
        the origin's verdict ("file unchanged since the comment") next to the
        current line, which is the exact sentence the undo case was reported
        for. The newest event is the one that describes the row as it stands.
        """
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT annotation_id, revision_id, method, status, detail, anchor_json
                  FROM annotation_anchor_events
                 WHERE annotation_id IN (
                       SELECT annotation_id FROM annotation_anchor_events WHERE revision_id = ?
                 )
                 ORDER BY annotation_id ASC, created_at ASC, id ASC
                """,
                (revision_id,),
            ).fetchall()

        moves: dict[str, AnchorMove] = {}
        current = ""
        previous_line = 0
        for row in rows:
            annotation_id = str(row["annotation_id"])
            if annotation_id != current:
                current = annotation_id
                previous_line = 0
            anchor = _anchor_from_json(row["anchor_json"])
            if str(row["revision_id"]) == revision_id:
                moves[annotation_id] = AnchorMove(
                    annotation_id=annotation_id,
                    path=anchor.path,
                    status=str(row["status"]),
                    method=str(row["method"]),
                    detail=str(row["detail"] or ""),
                    from_line=previous_line or anchor.start_line,
                    to_line=anchor.start_line,
                )
            previous_line = anchor.start_line
        return tuple(moves.values())

    def record_delivery(self, delivery: DeliveryRecord) -> DeliveryRecord:
        """Insert a delivery attempt. Schema-only in this scope (PR-R6 owns it)."""
        self._ensure_ready()
        now = utc_now()
        record = DeliveryRecord(
            id=delivery.id or new_delivery_id(),
            annotation_id=delivery.annotation_id,
            target_type=delivery.target_type,
            target_ref=delivery.target_ref,
            state=delivery.state,
            remote_ref=delivery.remote_ref,
            last_error=delivery.last_error,
            created_at=delivery.created_at or now,
            updated_at=delivery.updated_at or now,
            operation_id=delivery.operation_id,
            revision_id=delivery.revision_id,
            feedback_hash=delivery.feedback_hash,
            annotation_version=delivery.annotation_version,
        )
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO deliveries (
                    id, annotation_id, target_type, target_ref, state,
                    remote_ref, last_error, created_at, updated_at,
                    operation_id, revision_id, feedback_hash, annotation_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.annotation_id,
                    record.target_type,
                    record.target_ref,
                    record.state,
                    record.remote_ref,
                    record.last_error,
                    record.created_at,
                    record.updated_at,
                    record.operation_id,
                    record.revision_id,
                    record.feedback_hash,
                    record.annotation_version,
                ),
            )
        return record

    def list_deliveries(self, annotation_id: str) -> tuple[DeliveryRecord, ...]:
        """Delivery attempts for one annotation, oldest first."""
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE annotation_id = ? ORDER BY created_at ASC",
                (annotation_id,),
            ).fetchall()
        return tuple(_row_to_delivery(row) for row in rows)

    def list_delivery_operation(self, operation_id: str) -> tuple[DeliveryRecord, ...]:
        """All annotation rows belonging to one feedback delivery operation."""
        self._ensure_ready()
        if not operation_id:
            return ()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE operation_id = ? ORDER BY created_at ASC, id ASC",
                (operation_id,),
            ).fetchall()
        return tuple(_row_to_delivery(row) for row in rows)

    def begin_delivery_operation(self, deliveries: Sequence[DeliveryRecord]) -> tuple[tuple[DeliveryRecord, ...], bool]:
        """Atomically create one idempotent feedback operation.

        Returns ``(records, created)`` so only the transaction winner may launch
        the external agent. A known-not-dispatched blocked/failed operation may
        be atomically reclaimed when the immutable operation identity still
        matches; sent, dispatching, uncertain, or remotely-started attempts are
        replay-only.
        """
        if not deliveries:
            return (), False
        operation_id = deliveries[0].operation_id
        if not operation_id or any(item.operation_id != operation_id for item in deliveries):
            raise ValueError("a delivery operation requires one non-empty operation_id")
        self._ensure_ready()
        now = utc_now()
        records = tuple(
            DeliveryRecord(
                id=item.id or new_delivery_id(),
                annotation_id=item.annotation_id,
                target_type=item.target_type,
                target_ref=item.target_ref,
                state=item.state,
                remote_ref=item.remote_ref,
                last_error=item.last_error,
                created_at=item.created_at or now,
                updated_at=item.updated_at or now,
                operation_id=item.operation_id,
                revision_id=item.revision_id,
                feedback_hash=item.feedback_hash,
                annotation_version=item.annotation_version,
            )
            for item in deliveries
        )
        with self._transaction() as conn:
            # Serialize the read-then-insert so two browser retries cannot both
            # conclude that this operation is new and race to launch an agent.
            self._lock_for_write(conn, key=f"delivery:{operation_id}")
            existing = conn.execute(
                "SELECT * FROM deliveries WHERE operation_id = ? ORDER BY created_at ASC, id ASC",
                (operation_id,),
            ).fetchall()
            if existing:
                existing_records = tuple(_row_to_delivery(row) for row in existing)
                retryable = all(
                    item.state in {"blocked", "failed"} and not item.remote_ref for item in existing_records
                )
                same_identity = {
                    (
                        item.annotation_id,
                        item.target_type,
                        item.target_ref,
                        item.revision_id,
                        item.feedback_hash,
                        item.annotation_version,
                    )
                    for item in existing_records
                } == {
                    (
                        item.annotation_id,
                        item.target_type,
                        item.target_ref,
                        item.revision_id,
                        item.feedback_hash,
                        item.annotation_version,
                    )
                    for item in records
                }
                if retryable and same_identity:
                    conn.execute(
                        """
                        UPDATE deliveries
                        SET state = 'dispatching',
                            remote_ref = '',
                            last_error = '',
                            updated_at = ?
                        WHERE operation_id = ?
                        """,
                        (now, operation_id),
                    )
                    claimed = conn.execute(
                        "SELECT * FROM deliveries WHERE operation_id = ? ORDER BY created_at ASC, id ASC",
                        (operation_id,),
                    ).fetchall()
                    return tuple(_row_to_delivery(row) for row in claimed), True
                return existing_records, False
            for record in records:
                conn.execute(
                    """
                    INSERT INTO deliveries (
                        id, annotation_id, target_type, target_ref, state, remote_ref, last_error,
                        created_at, updated_at, operation_id, revision_id, feedback_hash, annotation_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.id,
                        record.annotation_id,
                        record.target_type,
                        record.target_ref,
                        record.state,
                        record.remote_ref,
                        record.last_error,
                        record.created_at,
                        record.updated_at,
                        record.operation_id,
                        record.revision_id,
                        record.feedback_hash,
                        record.annotation_version,
                    ),
                )
        return records, True

    def update_delivery_operation(
        self, operation_id: str, *, state: str, remote_ref: str = "", last_error: str = ""
    ) -> tuple[DeliveryRecord, ...]:
        """Move every row in an operation to the same externally-observed state."""
        self._ensure_ready()
        now = utc_now()
        with self._transaction() as conn:
            conn.execute(
                "UPDATE deliveries SET state = ?, remote_ref = ?, last_error = ?, updated_at = ? WHERE operation_id = ?",
                (state, remote_ref, last_error, now, operation_id),
            )
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE operation_id = ? ORDER BY created_at ASC, id ASC",
                (operation_id,),
            ).fetchall()
        return tuple(_row_to_delivery(row) for row in rows)

    # ----- review evidence ------------------------------------------------ #

    def add_evidence(self, evidence: ReviewEvidence) -> ReviewEvidence:
        """Persist one immutable artifact/link against one ReviewRevision."""

        self._ensure_ready()
        now = utc_now()
        record = ReviewEvidence(
            id=evidence.id or new_evidence_id(),
            review_id=evidence.review_id,
            revision_id=evidence.revision_id,
            kind=evidence.kind,
            title=evidence.title,
            path=evidence.path,
            artifact_path=evidence.artifact_path,
            url=evidence.url,
            content_hash=evidence.content_hash,
            mime_type=evidence.mime_type,
            bytes=max(0, int(evidence.bytes)),
            source=evidence.source,
            source_ref=evidence.source_ref,
            verification_status=evidence.verification_status,
            detail=evidence.detail,
            created_at=evidence.created_at or now,
        )
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO review_evidence (
                    id, review_id, revision_id, kind, title, path,
                    artifact_path, url, content_hash, mime_type, bytes,
                    source, source_ref, verification_status, detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.id,
                    record.review_id,
                    record.revision_id,
                    record.kind,
                    record.title,
                    record.path,
                    record.artifact_path,
                    record.url,
                    record.content_hash,
                    record.mime_type,
                    record.bytes,
                    record.source,
                    record.source_ref,
                    record.verification_status,
                    record.detail,
                    record.created_at,
                ),
            )
        return record

    def get_evidence(self, evidence_id: str) -> ReviewEvidence | None:
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM review_evidence WHERE id = ?", (evidence_id,)).fetchone()
        return None if row is None else _row_to_evidence(row)

    def get_evidence_by_source(self, review_id: str, source: str, source_ref: str) -> ReviewEvidence | None:
        """Return one external check/artifact already ingested under its stable source ref."""

        self._ensure_ready()
        if not source_ref:
            return None
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM review_evidence
                 WHERE review_id = ? AND source = ? AND source_ref = ?
                 ORDER BY created_at ASC LIMIT 1
                """,
                (review_id, source, source_ref),
            ).fetchone()
        return None if row is None else _row_to_evidence(row)

    def list_evidence(self, review_id: str) -> tuple[ReviewEvidence, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM review_evidence WHERE review_id = ? ORDER BY created_at DESC, id DESC",
                (review_id,),
            ).fetchall()
        return tuple(_row_to_evidence(row) for row in rows)

    def delete_evidence(self, evidence_id: str) -> int:
        """Delete one evidence row and its managed artifact, if any."""

        record = self.get_evidence(evidence_id)
        if record is None:
            return 0
        with self._transaction() as conn:
            cursor = conn.execute("DELETE FROM review_evidence WHERE id = ?", (evidence_id,))
        if record.artifact_path:
            self._delete_artifact_prefix(record.artifact_path)
        return int(cursor.rowcount or 0)

    # ----- artifacts ------------------------------------------------------ #

    def _write_artifact_bytes(self, key: str, payload: bytes) -> str:
        """Persist *payload* behind one deterministic Review artifact key."""

        backend = self._artifact_backend
        if backend is not None:
            return backend.write(key, payload)
        target = confine_to_root(self.root / key, self.root)
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".~review_artifact_")
        try:
            with os.fdopen(handle, "wb") as fh:
                fh.write(payload)
            os.replace(tmp_name, target)
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
        return hashlib.sha256(payload).hexdigest()

    def _read_artifact_bytes(self, key: str) -> bytes | None:
        backend = self._artifact_backend
        if backend is not None:
            return backend.read(key)
        try:
            target = confine_to_root(self.root / key, self.root)
        except ValueError:
            return None
        if not target.is_file():
            return None
        try:
            return target.read_bytes()
        except OSError:
            return None

    def _delete_artifact_prefix(self, prefix: str) -> None:
        backend = self._artifact_backend
        if backend is not None:
            backend.delete_prefix(prefix)
            return
        try:
            target = confine_to_root(self.root / prefix, self.root)
        except ValueError:
            return
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                target.unlink(missing_ok=True)

    def artifact_relpath(self, review_id: str, revision_id: str) -> str:
        """Store-root-relative location of a revision's packet artifact.

        Both ids go through ``safe_segment`` first. ``confine_to_root`` alone is
        not enough: ``<root>/review/artifacts/../../etc/passwd`` resolves back
        *inside* the root and would be accepted, so traversal is rejected at the
        segment, before it ever becomes a path.
        """
        review_seg = safe_segment(review_id, field="review_id")
        revision_seg = safe_segment(revision_id, field="revision_id")
        return f"review/{ARTIFACT_DIRNAME}/{review_seg}/{revision_seg}/{ARTIFACT_FILENAME}"

    def write_packet_artifact(self, review_id: str, revision_id: str, payload: bytes) -> tuple[str, str, int]:
        """Persist a revision's packet; returns ``(rel_path, sha256, bytes)``."""

        self._ensure_ready()
        rel = self.artifact_relpath(review_id, revision_id)
        digest = self._write_artifact_bytes(rel, payload)
        return rel, digest, len(payload)

    def read_packet_artifact(self, revision: ReviewRevision) -> bytes | None:
        """Read a revision's packet back, or ``None`` when it is unavailable."""

        self._ensure_ready()
        if not revision.packet_path:
            return None
        return self._read_artifact_bytes(revision.packet_path)

    def evidence_artifact_relpath(self, review_id: str, revision_id: str, evidence_id: str) -> str:
        """Deterministic content path; original filenames remain display metadata only."""

        review_seg = safe_segment(review_id, field="review_id")
        revision_seg = safe_segment(revision_id, field="revision_id")
        evidence_seg = safe_segment(evidence_id, field="evidence_id")
        return f"review/{ARTIFACT_DIRNAME}/{review_seg}/{revision_seg}/evidence/{evidence_seg}.bin"

    def write_evidence_artifact(
        self, review_id: str, revision_id: str, evidence_id: str, payload: bytes
    ) -> tuple[str, str, int]:
        """Write uploaded evidence and return its deterministic key/hash/size."""

        self._ensure_ready()
        rel = self.evidence_artifact_relpath(review_id, revision_id, evidence_id)
        digest = self._write_artifact_bytes(rel, payload)
        return rel, digest, len(payload)

    def read_evidence_artifact(self, evidence: ReviewEvidence) -> bytes | None:
        self._ensure_ready()
        if not evidence.artifact_path:
            return None
        return self._read_artifact_bytes(evidence.artifact_path)

    def blob_artifact_relpath(self, review_id: str, revision_id: str) -> str:
        """Deterministic location of the exact new-side text snapshot."""

        review_seg = safe_segment(review_id, field="review_id")
        revision_seg = safe_segment(revision_id, field="revision_id")
        return f"review/{ARTIFACT_DIRNAME}/{review_seg}/{revision_seg}/{BLOB_ARTIFACT_FILENAME}"

    def write_blob_artifact(self, review_id: str, revision_id: str, blobs: Mapping[str, str]) -> tuple[str, str, int]:
        """Persist exact changed-file text used to build one ReviewRevision."""

        self._ensure_ready()
        rel = self.blob_artifact_relpath(review_id, revision_id)
        serialisable = {str(path): text for path, text in sorted(blobs.items()) if isinstance(text, str)}
        raw = json.dumps(serialisable, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        payload = gzip.compress(raw, mtime=0)
        digest = self._write_artifact_bytes(rel, payload)
        return rel, digest, len(payload)

    def read_blob_artifact(self, review_id: str, revision_id: str) -> dict[str, str] | None:
        """Return frozen new-side text for *revision_id*, or ``None`` if absent/corrupt."""

        self._ensure_ready()
        try:
            rel = self.blob_artifact_relpath(review_id, revision_id)
        except ValueError:
            return None
        raw = self._read_artifact_bytes(rel)
        if raw is None:
            return None
        try:
            payload = json.loads(gzip.decompress(raw).decode("utf-8"))
        except (EOFError, UnicodeDecodeError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        out: dict[str, str] = {}
        for path, text in payload.items():
            if not isinstance(path, str) or not isinstance(text, str):
                return None
            out[path] = text
        return out

    def submodule_artifact_relpath(self, review_id: str, revision_id: str) -> str:
        review_seg = safe_segment(review_id, field="review_id")
        revision_seg = safe_segment(revision_id, field="revision_id")
        return f"review/{ARTIFACT_DIRNAME}/{review_seg}/{revision_seg}/{SUBMODULE_ARTIFACT_FILENAME}"

    def write_submodule_artifact(self, review_id: str, revision_id: str, payload: bytes) -> tuple[str, str, int]:
        """Persist exact frozen nested-submodule snapshot metadata and bytes."""

        self._ensure_ready()
        rel = self.submodule_artifact_relpath(review_id, revision_id)
        digest = self._write_artifact_bytes(rel, payload)
        return rel, digest, len(payload)

    def read_submodule_artifact(self, review_id: str, revision_id: str) -> bytes | None:
        """Return the frozen nested-submodule artifact, or ``None`` when absent."""

        self._ensure_ready()
        try:
            rel = self.submodule_artifact_relpath(review_id, revision_id)
        except ValueError:
            return None
        return self._read_artifact_bytes(rel)

    def media_artifact_relpath(self, review_id: str, revision_id: str) -> str:
        review_seg = safe_segment(review_id, field="review_id")
        revision_seg = safe_segment(revision_id, field="revision_id")
        return f"review/{ARTIFACT_DIRNAME}/{review_seg}/{revision_seg}/{MEDIA_ARTIFACT_FILENAME}"

    def write_media_artifact(
        self, review_id: str, revision_id: str, blobs: Mapping[str, bytes]
    ) -> tuple[str, str, int]:
        """Persist exact changed binary/media bytes for one review revision."""

        self._ensure_ready()
        rel = self.media_artifact_relpath(review_id, revision_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for raw_path, payload in sorted(blobs.items()):
                path = str(raw_path)
                rel_path = PurePosixPath(path)
                if not path or rel_path.is_absolute() or ".." in rel_path.parts:
                    raise ValueError(f"unsafe media artifact path: {path!r}")
                if not isinstance(payload, bytes):
                    raise TypeError(f"media artifact payload for {path!r} is not bytes")
                info = zipfile.ZipInfo(path)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, payload)
        payload = buffer.getvalue()
        digest = self._write_artifact_bytes(rel, payload)
        return rel, digest, len(payload)

    def read_media_artifact(self, review_id: str, revision_id: str) -> dict[str, bytes] | None:
        """Return frozen changed binary/media bytes, or ``None`` when unavailable."""

        self._ensure_ready()
        try:
            rel = self.media_artifact_relpath(review_id, revision_id)
        except ValueError:
            return None
        payload = self._read_artifact_bytes(rel)
        if payload is None:
            return None
        try:
            out: dict[str, bytes] = {}
            with zipfile.ZipFile(io.BytesIO(payload), "r") as archive:
                for name in archive.namelist():
                    rel_path = PurePosixPath(name)
                    if not name or rel_path.is_absolute() or ".." in rel_path.parts:
                        return None
                    out[name] = archive.read(name)
            return out
        except (ValueError, zipfile.BadZipFile):
            return None

    def source_tree_artifact_relpath(self, review_id: str, revision_id: str, *, side: str = "new") -> str:
        """Deterministic location of one exact hosted Review-side tree manifest."""

        if side not in {"old", "new"}:
            raise ValueError("source tree artifact side must be old or new")
        review_seg = safe_segment(review_id, field="review_id")
        revision_seg = safe_segment(revision_id, field="revision_id")
        filename = SOURCE_TREE_ARTIFACT_FILENAME if side == "new" else OLD_SOURCE_TREE_ARTIFACT_FILENAME
        return f"review/{ARTIFACT_DIRNAME}/{review_seg}/{revision_seg}/{filename}"

    def write_source_tree_artifact(
        self,
        review_id: str,
        revision_id: str,
        entries: Mapping[str, Mapping[str, Any]],
        *,
        side: str = "new",
    ) -> tuple[str, str, int]:
        """Persist a full revision tree as path -> content identity metadata.

        The artifact deliberately stores digests rather than duplicate file
        bytes. Hosted artifact backends may expose ``record_references`` so the
        referenced source blobs stay live after the transient enterprise View
        that captured the Review expires.
        """

        self._ensure_ready()
        serialisable: dict[str, dict[str, int | str]] = {}
        referenced: set[str] = set()
        for raw_path, raw in sorted(entries.items()):
            path = str(raw_path)
            rel_path = PurePosixPath(path)
            if not path or rel_path.is_absolute() or ".." in rel_path.parts:
                raise ValueError(f"unsafe source tree artifact path: {path!r}")
            digest = str(raw.get("content_digest") or "")
            mode = int(raw.get("mode") or 0)
            size = int(raw.get("size") or 0)
            if not digest:
                raise ValueError(f"source tree artifact path has no content digest: {path!r}")
            serialisable[path] = {"content_digest": digest, "mode": mode, "size": size}
            referenced.add(digest)
        encoded = json.dumps(serialisable, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        payload = gzip.compress(encoded, mtime=0)
        rel = self.source_tree_artifact_relpath(review_id, revision_id, side=side)
        digest = self._write_artifact_bytes(rel, payload)
        recorder = getattr(self._artifact_backend, "record_references", None)
        if callable(recorder):
            recorder(rel, tuple(sorted(referenced)))
        return rel, digest, len(payload)

    def read_source_tree_artifact(
        self, review_id: str, revision_id: str, *, side: str = "new"
    ) -> dict[str, dict[str, int | str]] | None:
        """Return one immutable full-tree manifest, or ``None`` when unavailable."""

        self._ensure_ready()
        try:
            rel = self.source_tree_artifact_relpath(review_id, revision_id, side=side)
        except ValueError:
            return None
        payload = self._read_artifact_bytes(rel)
        if payload is None:
            return None
        try:
            decoded = json.loads(gzip.decompress(payload).decode("utf-8"))
        except (EOFError, UnicodeDecodeError, ValueError):
            return None
        if not isinstance(decoded, dict):
            return None
        out: dict[str, dict[str, int | str]] = {}
        for path, raw in decoded.items():
            if not isinstance(path, str) or not isinstance(raw, dict):
                return None
            digest = raw.get("content_digest")
            mode = raw.get("mode")
            size = raw.get("size")
            if not isinstance(digest, str) or not digest or not isinstance(mode, int) or not isinstance(size, int):
                return None
            out[path] = {"content_digest": digest, "mode": mode, "size": size}
        return out

    def read_source_content(self, digest: str) -> bytes | None:
        """Resolve one source-tree digest through the configured content source."""

        if self._source_content_reader is not None:
            payload = self._source_content_reader(digest)
            return payload if isinstance(payload, bytes) else None
        reader = getattr(self._artifact_backend, "read_content", None)
        if not callable(reader):
            return None
        payload = reader(digest)
        return payload if isinstance(payload, bytes) else None

    def prune(self, *, older_than_days: int = 90) -> int:
        """Delete archived sessions older than the cutoff; returns the count.

        Only ``archived`` sessions are touched. An open review is somebody's
        unfinished work no matter how old it is, and deleting it to reclaim
        space would throw away the one thing this store exists to keep. A
        *finished* one is spared for the same reason: finishing a review is not
        discarding it.

        ``archived`` is the internal status written only by an explicit human
        discard -- ``lc review --discard-review`` or ``POST /reviews/{id}/finish
        {"status": "archived"}`` -- and this is called only from the worker's
        ``retention_cleanup`` job. Both ends matter: with no writer nothing is
        ever collected, and with no caller the disposable state is a label on a
        store that still grows without bound.

        Rows go by FK cascade; artifacts and the contentless FTS index are not
        reached by cascade and are cleaned explicitly here.
        """
        self._ensure_ready()
        cutoff = (datetime.now(UTC) - timedelta(days=max(0, older_than_days))).isoformat()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT id FROM review_sessions WHERE status = 'archived' AND updated_at <= ?",
                (cutoff,),
            ).fetchall()
            review_ids = [str(row["id"]) for row in rows]
            if not review_ids:
                return 0
            for review_id in review_ids:
                for annotation in conn.execute(
                    "SELECT id, body, path FROM annotations WHERE review_id = ?",
                    (review_id,),
                ).fetchall():
                    self._annotation_search_delete(
                        conn,
                        str(annotation["id"]),
                        str(annotation["body"]),
                        str(annotation["path"]),
                    )
                conn.execute("DELETE FROM review_sessions WHERE id = ?", (review_id,))
        for review_id in review_ids:
            self._remove_artifacts(review_id)
        return len(review_ids)

    def _remove_artifacts(self, review_id: str) -> None:
        try:
            segment = safe_segment(review_id, field="review_id")
        except ValueError:
            return
        self._delete_artifact_prefix(f"review/{ARTIFACT_DIRNAME}/{segment}/")


def _fts_insert(conn: sqlite3.Connection, rowid: int, body: str, path: str) -> None:
    conn.execute(
        "INSERT INTO annotations_fts (rowid, body, path) VALUES (?, ?, ?)",
        (rowid, body, path),
    )


def _fts_delete(conn: sqlite3.Connection, rowid: int, body: str, path: str) -> None:
    """Remove one row from the contentless FTS index.

    ``content=''`` tables store no column values, so SQLite cannot work out
    what to un-index from a ``DELETE``; the original values have to be handed
    back in through the special ``'delete'`` command.
    """
    conn.execute(
        "INSERT INTO annotations_fts (annotations_fts, rowid, body, path) VALUES ('delete', ?, ?, ?)",
        (rowid, body, path),
    )


def _row_to_session(row: sqlite3.Row) -> ReviewSession:
    return ReviewSession(
        id=str(row["id"]),
        subject_type=str(row["subject_type"]),  # type: ignore[arg-type]
        repo_root=str(row["repo_root"]),
        range_mode=str(row["range_mode"]),  # type: ignore[arg-type]
        title=str(row["title"]),
        source_ref=str(row["source_ref"]),
        actor_type=str(row["actor_type"]),  # type: ignore[arg-type]
        status=str(row["status"]),  # type: ignore[arg-type]
        reviewer_id=str(row["reviewer_id"]),
        current_revision_id=str(row["current_revision_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_revision(row: sqlite3.Row) -> ReviewRevision:
    return ReviewRevision(
        id=str(row["id"]),
        review_id=str(row["review_id"]),
        revision_number=int(row["revision_number"]),
        range_mode=str(row["range_mode"]),  # type: ignore[arg-type]
        tree_fingerprint=str(row["tree_fingerprint"]),
        packet_schema_version=int(row["packet_schema_version"]),
        base_sha=str(row["base_sha"]),
        head_sha=str(row["head_sha"]),
        merge_base_sha=str(row["merge_base_sha"]),
        dirty=bool(row["dirty"]),
        packet_path=str(row["packet_path"]),
        packet_sha256=str(row["packet_sha256"]),
        packet_bytes=int(row["packet_bytes"]),
        degraded=_json_tuple(row["degraded_json"]),
        provenance_host=str(row["provenance_host"]),
        provenance_model=str(row["provenance_model"]),
        provenance_session_id=str(row["provenance_session_id"]),
        provenance_certainty=str(row["provenance_certainty"]),  # type: ignore[arg-type]
        created_at=str(row["created_at"]),
        source_fingerprint=str(row["source_fingerprint"]),
    )


def _row_to_unit(row: sqlite3.Row) -> ReviewUnit:
    return ReviewUnit(
        revision_id=str(row["revision_id"]),
        unit_key=str(row["unit_key"]),
        kind=str(row["kind"]),  # type: ignore[arg-type]
        path=str(row["path"]),
        content_fingerprint=str(row["content_fingerprint"]),
        fingerprint_method=str(row["fingerprint_method"]),  # type: ignore[arg-type]
        symbol=str(row["symbol"]),
        ordinal=int(row["ordinal"]),
        start_line=int(row["start_line"]),
        end_line=int(row["end_line"]),
        attention_rank=int(row["attention_rank"]),
        attention_group=str(row["attention_group"]),  # type: ignore[arg-type]
        reasons=_json_tuple(row["reasons_json"]),
    )


def _row_to_change_proposal(row: sqlite3.Row) -> ReviewChangeProposal:
    return ReviewChangeProposal(
        id=str(row["id"]),
        review_id=str(row["review_id"]),
        base_revision_id=str(row["base_revision_id"]),
        path=str(row["path"]),
        start_line=int(row["start_line"]),
        end_line=int(row["end_line"]),
        original_text=str(row["original_text"]),
        replacement_text=str(row["replacement_text"]),
        patch_text=str(row["patch_text"]),
        base_file_sha256=str(row["base_file_sha256"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        target_unit_key=str(row["target_unit_key"]),
        annotation_id=str(row["annotation_id"]),
        intent=str(row["intent"]),
        conflict_reason=str(row["conflict_reason"]),
        created_by=str(row["created_by"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        applied_at=str(row["applied_at"]),
        applied_source_fingerprint=str(row["applied_source_fingerprint"]),
        result_revision_id=str(row["result_revision_id"]),
    )


def _row_to_mark(row: sqlite3.Row) -> ReviewMark:
    return ReviewMark(
        review_id=str(row["review_id"]),
        unit_key=str(row["unit_key"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        reviewed_revision_id=str(row["reviewed_revision_id"]),
        content_fingerprint=str(row["content_fingerprint"]),
        reviewer_id=str(row["reviewer_id"]),
        actor_type=str(row["actor_type"]),  # type: ignore[arg-type]
        note=str(row["note"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_mark_event(row: sqlite3.Row) -> ReviewMarkEvent:
    return ReviewMarkEvent(
        id=str(row["id"]),
        review_id=str(row["review_id"]),
        reviewer_id=str(row["reviewer_id"]),
        unit_key=str(row["unit_key"]),
        revision_id=str(row["revision_id"]),
        reviewed_revision_id=str(row["reviewed_revision_id"]),
        event_kind=str(row["event_kind"]),  # type: ignore[arg-type]
        from_state=str(row["from_state"]),  # type: ignore[arg-type]
        to_state=str(row["to_state"]),  # type: ignore[arg-type]
        content_fingerprint=str(row["content_fingerprint"]),
        previous_unit_key=str(row["previous_unit_key"]),
        actor_type=str(row["actor_type"]),  # type: ignore[arg-type]
        note=str(row["note"]),
        reason=str(row["reason"]),
        created_at=str(row["created_at"]),
    )


def _row_to_activity_event(row: sqlite3.Row) -> ReviewActivityEvent:
    return ReviewActivityEvent(
        id=str(row["id"]),
        review_id=str(row["review_id"]),
        revision_id=str(row["revision_id"]),
        kind=str(row["kind"]),  # type: ignore[arg-type]
        actor_id=str(row["actor_id"]),
        actor_type=str(row["actor_type"]),  # type: ignore[arg-type]
        subject_type=str(row["subject_type"]),
        subject_id=str(row["subject_id"]),
        summary=str(row["summary"]),
        detail_json=str(row["detail_json"]),
        created_at=str(row["created_at"]),
    )


def _row_to_outcome(row: sqlite3.Row) -> ReviewOutcome:
    outcome = str(row["outcome"])
    if outcome not in REVIEW_OUTCOME_KINDS:
        outcome = "comment"
    return ReviewOutcome(
        id=str(row["id"]),
        review_id=str(row["review_id"]),
        revision_id=str(row["revision_id"]),
        reviewer_id=str(row["reviewer_id"]),
        outcome=cast(ReviewOutcomeKind, outcome),
        summary=str(row["summary"]),
        created_at=str(row["created_at"]),
    )


def _row_to_annotation_version(row: sqlite3.Row) -> AnnotationVersion:
    return AnnotationVersion(
        id=str(row["id"]),
        annotation_id=str(row["annotation_id"]),
        review_id=str(row["review_id"]),
        revision_id=str(row["revision_id"]),
        version_number=int(row["version_number"]),
        body=str(row["body"]),
        kind=str(row["kind"]),  # type: ignore[arg-type]
        state=str(row["state"]),  # type: ignore[arg-type]
        author_response=str(row["author_response"]),  # type: ignore[arg-type]
        author_response_source_id=str(row["author_response_source_id"]),
        author_response_at=str(row["author_response_at"]),
        resolved_revision_id=str(row["resolved_revision_id"]),
        changed_by=str(row["changed_by"]),
        changed_by_actor=str(row["changed_by_actor"]),  # type: ignore[arg-type]
        change_kind=str(row["change_kind"]),  # type: ignore[arg-type]
        turn_owner_kind=str(row["turn_owner_kind"]),  # type: ignore[arg-type]
        turn_owner_id=str(row["turn_owner_id"]),
        created_at=str(row["created_at"]),
    )


def _row_to_discarded(row: sqlite3.Row) -> DiscardedMark:
    return DiscardedMark(
        review_id=str(row["review_id"]),
        unit_key=str(row["unit_key"]),
        discarded_revision_id=str(row["discarded_revision_id"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        content_fingerprint=str(row["content_fingerprint"]),
        reviewer_id=str(row["reviewer_id"]),
        reviewed_revision_id=str(row["reviewed_revision_id"]),
        kind=str(row["kind"]),  # type: ignore[arg-type]
        path=str(row["path"]),
        symbol=str(row["symbol"]),
        ordinal=int(row["ordinal"]),
        start_line=int(row["start_line"]),
        reason=str(row["reason"]),
        discarded_at=str(row["discarded_at"]),
    )


def _row_to_annotation(row: sqlite3.Row) -> Annotation:
    return Annotation(
        id=str(row["id"]),
        review_id=str(row["review_id"]),
        revision_id=str(row["revision_id"]),
        anchor=_anchor_from_json(row["anchor_json"]),
        body=str(row["body"]),
        kind=str(row["kind"]),  # type: ignore[arg-type]
        state=str(row["state"]),  # type: ignore[arg-type]
        parent_id=str(row["parent_id"]),
        created_by=str(row["created_by"]),
        created_by_actor=str(row["created_by_actor"]),  # type: ignore[arg-type]
        source=str(row["source"]),  # type: ignore[arg-type]
        source_id=str(row["source_id"]),
        title=str(row["title"]),
        evidence=_json_tuple(row["evidence_json"]),
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        author_response=str(row["author_response"]),  # type: ignore[arg-type]
        author_response_source_id=str(row["author_response_source_id"]),
        author_response_at=str(row["author_response_at"]),
        resolved_revision_id=str(row["resolved_revision_id"]),
        turn_owner_kind=str(row["turn_owner_kind"]),  # type: ignore[arg-type]
        turn_owner_id=str(row["turn_owner_id"]),
        anchor_method=str(row["anchor_method"]),  # type: ignore[arg-type]
        anchor_detail=str(row["anchor_detail"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_delivery(row: sqlite3.Row) -> DeliveryRecord:
    return DeliveryRecord(
        id=str(row["id"]),
        annotation_id=str(row["annotation_id"]),
        target_type=str(row["target_type"]),  # type: ignore[arg-type]
        target_ref=str(row["target_ref"]),
        state=str(row["state"]),
        remote_ref=str(row["remote_ref"]),
        last_error=str(row["last_error"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        operation_id=str(row["operation_id"]),
        revision_id=str(row["revision_id"]),
        feedback_hash=str(row["feedback_hash"]),
        annotation_version=int(row["annotation_version"]),
    )


def _row_to_evidence(row: sqlite3.Row) -> ReviewEvidence:
    return ReviewEvidence(
        id=str(row["id"]),
        review_id=str(row["review_id"]),
        revision_id=str(row["revision_id"]),
        kind=str(row["kind"]),  # type: ignore[arg-type]
        title=str(row["title"]),
        path=str(row["path"]),
        artifact_path=str(row["artifact_path"]),
        url=str(row["url"]),
        content_hash=str(row["content_hash"]),
        mime_type=str(row["mime_type"]),
        bytes=int(row["bytes"]),
        source=str(row["source"]),  # type: ignore[arg-type]
        source_ref=str(row["source_ref"]),
        verification_status=str(row["verification_status"]),
        detail=str(row["detail"]),
        created_at=str(row["created_at"]),
    )


__all__ = [
    "DB_NAME",
    "SCHEMA",
    "ReviewStore",
    "new_annotation_id",
    "new_delivery_id",
    "new_evidence_id",
    "new_revision_id",
    "new_session_id",
    "utc_now",
]
