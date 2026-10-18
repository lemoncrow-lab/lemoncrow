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
is no advisory lock on any ``SqliteTableStore``. One instance may be shared
across threads: a pinned scope connection (``_connection``) and its batch
state are per thread, and every other operation opens its own connection.
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
import threading
import zipfile
import zlib
from bisect import bisect_left
from collections import OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from dataclasses import fields as dataclass_fields
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, ClassVar, Literal, Protocol, cast

from lemoncrow.core.foundation.paths import (
    DEFAULT_STORE_DIRNAME,
    confine_to_root,
    ensure_dir_gitignore,
    safe_segment,
)
from lemoncrow.core.foundation.sqlite_base import SqliteTableStore

if TYPE_CHECKING:
    from lemoncrow.pro.capabilities.review.run_evidence import RunArtifactStore
from lemoncrow.infra.storage.ids import make_uuid7
from lemoncrow.pro.capabilities.review.errors import AnnotationVersionConflict as AnnotationVersionConflict
from lemoncrow.pro.capabilities.review.errors import HostedAttachmentConflict
from lemoncrow.pro.capabilities.review.errors import ReviewArtifactReclaimed as ReviewArtifactReclaimed
from lemoncrow.pro.capabilities.review.session_models import (
    ANCHOR_METHODS,
    HOSTED_ACTION_KINDS,
    HOSTED_ACTION_STATES,
    HOSTED_ACTION_UNRESOLVED,
    REVIEW_ASK_HANDOFF_STATES,
    REVIEW_ASK_INTENTS,
    REVIEW_ASK_ROLES,
    REVIEW_CHANGE_PROPOSAL_STATES,
    REVIEW_DESTINATION_KINDS,
    REVIEW_OUTCOME_KINDS,
    SESSION_SCHEMA_VERSION,
    ActorType,
    AnchorMethod,
    AnchorMove,
    Annotation,
    AnnotationAnchor,
    AnnotationVersion,
    AnnotationVersionKind,
    AuthorGuidanceRow,
    DecisionAction,
    DecisionDisposition,
    DeliveryRecord,
    DiscardedMark,
    FindingDismissalReason,
    FindingDisposition,
    HostedAction,
    HostedActionKind,
    HostedActionState,
    HostedAttachment,
    ReviewActivityEvent,
    ReviewActivityKind,
    ReviewAskConversationRecord,
    ReviewAskHandoff,
    ReviewAskHandoffState,
    ReviewAskIntent,
    ReviewAskMessage,
    ReviewAskRole,
    ReviewAttribution,
    ReviewBinding,
    ReviewChangeProposal,
    ReviewCommit,
    ReviewDecisionEvent,
    ReviewDeletionTombstone,
    ReviewDestination,
    ReviewDestinationKind,
    ReviewEvidence,
    ReviewIntelligenceRun,
    ReviewIntelligenceRunStatus,
    ReviewMark,
    ReviewMarkEvent,
    ReviewMarkEventKind,
    ReviewOutcome,
    ReviewOutcomeKind,
    ReviewRelationship,
    ReviewRevision,
    ReviewSession,
    ReviewSynthesisRow,
    ReviewUnit,
    SynthesisStatus,
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
# Ranks an enrichment replaced on one Snapshot (stable slots; ``read_ranking_history``).
RANKING_HISTORY_FILENAME = "ranking-history.json.gz"
RANKING_HISTORY_VERSION = 1
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
_ASK_CONVERSATION_ID_PREFIX = "rac"
_ASK_MESSAGE_ID_PREFIX = "ram"
_RELATIONSHIP_ID_PREFIX = "rrl"
_BINDING_ID_PREFIX = "rbd"
_HOSTED_ACTION_ID_PREFIX = "rha"
_META_SCHEMA_VERSION_KEY = "schema_version"

_META_DDL = "CREATE TABLE IF NOT EXISTS review_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);"


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
  review_base_sha TEXT NOT NULL DEFAULT '',
  actor_type   TEXT NOT NULL DEFAULT 'unknown',
  status       TEXT NOT NULL DEFAULT 'open',
  reviewer_id  TEXT NOT NULL DEFAULT 'local',
  current_revision_id TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL,
  review_base_policy TEXT NOT NULL DEFAULT 'integrated'
);
CREATE INDEX IF NOT EXISTS idx_review_sessions_legacy_subject
  ON review_sessions(repo_root, subject_type, source_ref, range_mode);
CREATE INDEX IF NOT EXISTS idx_review_sessions_updated
  ON review_sessions(status, updated_at DESC);

CREATE TABLE IF NOT EXISTS review_bindings (
  id                  TEXT PRIMARY KEY,
  repository_identity TEXT NOT NULL,
  subject_kind        TEXT NOT NULL,
  subject_key         TEXT NOT NULL,
  review_id           TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  worktree_key        TEXT NOT NULL DEFAULT '',
  active              INTEGER NOT NULL DEFAULT 1,
  created_at          TEXT NOT NULL,
  updated_at          TEXT NOT NULL,
  UNIQUE(repository_identity, subject_kind, subject_key, review_id)
);
CREATE INDEX IF NOT EXISTS idx_review_bindings_subject
  ON review_bindings(repository_identity, subject_kind, subject_key, active, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_bindings_review
  ON review_bindings(review_id, active, updated_at DESC);

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
  source_view_id        TEXT NOT NULL DEFAULT '',
  source_view_revision  INTEGER NOT NULL DEFAULT 0,
  capture_kind          TEXT NOT NULL DEFAULT 'legacy',
  commit_sha            TEXT NOT NULL DEFAULT '',
  branch_ref            TEXT NOT NULL DEFAULT '',
  worktree_key          TEXT NOT NULL DEFAULT '',
  title                 TEXT NOT NULL DEFAULT '',
  UNIQUE(review_id, revision_number)
);
CREATE INDEX IF NOT EXISTS idx_review_revisions_review
  ON review_revisions(review_id, revision_number DESC);
CREATE INDEX IF NOT EXISTS idx_review_revisions_tree
  ON review_revisions(review_id, tree_fingerprint);

CREATE TABLE IF NOT EXISTS review_commits (
  review_id      TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  commit_number  INTEGER NOT NULL,
  commit_sha     TEXT NOT NULL,
  revision_id    TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  parent_sha     TEXT NOT NULL DEFAULT '',
  state          TEXT NOT NULL DEFAULT 'active',
  created_at     TEXT NOT NULL,
  PRIMARY KEY (review_id, commit_number),
  UNIQUE(review_id, commit_sha)
);
CREATE INDEX IF NOT EXISTS idx_review_commits_revision ON review_commits(revision_id);
CREATE INDEX IF NOT EXISTS idx_review_commits_state ON review_commits(review_id, state, commit_number);

CREATE TABLE IF NOT EXISTS review_relationships (
  id               TEXT PRIMARY KEY,
  review_id        TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  kind             TEXT NOT NULL,
  relation         TEXT NOT NULL,
  provider         TEXT NOT NULL DEFAULT '',
  external_id      TEXT NOT NULL DEFAULT '',
  target_review_id TEXT NOT NULL DEFAULT '',
  url              TEXT NOT NULL DEFAULT '',
  title            TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT '',
  source           TEXT NOT NULL DEFAULT 'explicit',
  metadata_json    TEXT NOT NULL DEFAULT '{}',
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  UNIQUE(review_id, kind, relation, provider, external_id, target_review_id)
);
CREATE INDEX IF NOT EXISTS idx_review_relationships_review
  ON review_relationships(review_id, kind, relation);

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

CREATE TABLE IF NOT EXISTS review_attributions (
  revision_id TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  scope       TEXT NOT NULL,
  scope_key   TEXT NOT NULL DEFAULT '',
  host        TEXT NOT NULL,
  session_id  TEXT NOT NULL,
  role        TEXT NOT NULL DEFAULT 'contributor',
  model       TEXT NOT NULL DEFAULT '',
  certainty   TEXT NOT NULL DEFAULT 'none',
  method      TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL,
  PRIMARY KEY (revision_id, scope, scope_key, host, session_id)
);
CREATE INDEX IF NOT EXISTS idx_review_attributions_revision
  ON review_attributions(revision_id, scope, scope_key);
CREATE INDEX IF NOT EXISTS idx_review_attributions_session
  ON review_attributions(host, session_id, created_at DESC);

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

CREATE TABLE IF NOT EXISTS review_ask_conversations (
  id          TEXT PRIMARY KEY,
  review_id   TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  title       TEXT NOT NULL DEFAULT '',
  origin_json TEXT NOT NULL DEFAULT '{}',
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_ask_conversations_review
  ON review_ask_conversations(review_id, updated_at DESC, id DESC);

CREATE TABLE IF NOT EXISTS review_ask_messages (
  id              TEXT PRIMARY KEY,
  review_id       TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  revision_id     TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  role            TEXT NOT NULL,
  intent          TEXT NOT NULL DEFAULT 'question',
  body            TEXT NOT NULL DEFAULT '',
  target_unit_key TEXT NOT NULL DEFAULT '',
  path            TEXT NOT NULL DEFAULT '',
  symbol          TEXT NOT NULL DEFAULT '',
  side            TEXT NOT NULL DEFAULT '',
  start_line      INTEGER NOT NULL DEFAULT 0,
  end_line        INTEGER NOT NULL DEFAULT 0,
  context_json    TEXT NOT NULL DEFAULT '{}',
  references_json TEXT NOT NULL DEFAULT '[]',
  action_json     TEXT NOT NULL DEFAULT '{}',
  created_at      TEXT NOT NULL,
  conversation_id TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_review_ask_messages_review
  ON review_ask_messages(review_id, created_at ASC, id ASC);
CREATE INDEX IF NOT EXISTS idx_review_ask_messages_revision
  ON review_ask_messages(review_id, revision_id, created_at ASC, id ASC);
-- idx_review_ask_messages_conversation is created by v3_038 (the column may not exist yet on older DBs).

CREATE TABLE IF NOT EXISTS review_ask_handoffs (
  message_id       TEXT PRIMARY KEY REFERENCES review_ask_messages(id) ON DELETE CASCADE,
  operation_id     TEXT NOT NULL UNIQUE,
  instruction_hash TEXT NOT NULL,
  state            TEXT NOT NULL DEFAULT 'prepared',
  target_ref       TEXT NOT NULL DEFAULT '',
  remote_ref       TEXT NOT NULL DEFAULT '',
  last_error       TEXT NOT NULL DEFAULT '',
  updated_at       TEXT NOT NULL
);

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
  agent_role           TEXT NOT NULL DEFAULT 'unknown',
  agent_host           TEXT NOT NULL DEFAULT '',
  agent_model          TEXT NOT NULL DEFAULT '',
  parent_session_id    TEXT NOT NULL DEFAULT '',
  source_message_id    TEXT NOT NULL DEFAULT '',
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
  created_at    TEXT NOT NULL,
  attestation_class TEXT NOT NULL DEFAULT '',
  stable_key TEXT NOT NULL DEFAULT '',
  provider TEXT NOT NULL DEFAULT '',
  integration_id TEXT NOT NULL DEFAULT '',
  provider_revision TEXT NOT NULL DEFAULT '',
  branch TEXT NOT NULL DEFAULT '',
  environment TEXT NOT NULL DEFAULT '',
  attestation_scope TEXT NOT NULL DEFAULT '',
  attested_by TEXT NOT NULL DEFAULT '',
  remote_identity TEXT NOT NULL DEFAULT '',
  observed_at TEXT NOT NULL DEFAULT '',
  waived_requirement_key TEXT NOT NULL DEFAULT '',
  execution_intent TEXT NOT NULL DEFAULT '',
  executor_class TEXT NOT NULL DEFAULT '',
  execution_verification TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_review_evidence_review ON review_evidence(review_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_review_evidence_revision ON review_evidence(revision_id, created_at DESC);

-- Author guidance (review/guidance.py): raw, attributed, versioned author
-- input imported with the Change. Immutable rows, and the session deletion flow
-- (ON DELETE CASCADE) owns retention.
CREATE TABLE IF NOT EXISTS review_author_guidance (
  review_id            TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  id                   TEXT NOT NULL,
  revision_id          TEXT NOT NULL DEFAULT '',
  producer_host        TEXT NOT NULL DEFAULT '',
  producer_session_id  TEXT NOT NULL DEFAULT '',
  producer_model       TEXT NOT NULL DEFAULT '',
  attribution          TEXT NOT NULL DEFAULT 'unattributed',
  repo_root            TEXT NOT NULL DEFAULT '',
  schema_version       INTEGER NOT NULL DEFAULT 1,
  source_identity_json TEXT NOT NULL DEFAULT '[]',
  payload_json         TEXT NOT NULL,
  created_at           TEXT NOT NULL,
  imported_at          TEXT NOT NULL,
  PRIMARY KEY (review_id, id)
);
CREATE INDEX IF NOT EXISTS idx_review_author_guidance_producer
  ON review_author_guidance(review_id, producer_host, producer_session_id, created_at);

-- HumanDecision events (review/decisions.py): append-only human dispositions.
-- The session deletion flow (ON DELETE CASCADE) owns retention.
CREATE TABLE IF NOT EXISTS review_decision_events (
  review_id                  TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  id                         TEXT NOT NULL,
  decision_key               TEXT NOT NULL,
  reviewer_id                TEXT NOT NULL DEFAULT 'local',
  revision_id                TEXT NOT NULL,
  action                     TEXT NOT NULL,
  disposition                TEXT NOT NULL,
  option_key                 TEXT NOT NULL DEFAULT '',
  definition_ref             TEXT NOT NULL DEFAULT '',
  semantic_fingerprint       TEXT NOT NULL DEFAULT '',
  implementation_fingerprint TEXT NOT NULL DEFAULT '',
  scope_json                 TEXT NOT NULL DEFAULT '{}',
  implementation_json        TEXT NOT NULL DEFAULT '{}',
  reason                     TEXT NOT NULL DEFAULT '',
  trigger_kind               TEXT NOT NULL DEFAULT '',
  trigger_check_key          TEXT NOT NULL DEFAULT '',
  trigger_note               TEXT NOT NULL DEFAULT '',
  feedback_annotation_id     TEXT NOT NULL DEFAULT '',
  prior_event_id             TEXT NOT NULL DEFAULT '',
  actor_type                 TEXT NOT NULL DEFAULT 'human',
  created_at                 TEXT NOT NULL,
  PRIMARY KEY (review_id, id)
);
CREATE INDEX IF NOT EXISTS idx_review_decision_events_key
  ON review_decision_events(review_id, reviewer_id, decision_key, created_at, id);

-- Generic Review Intelligence runs/cache. Identity is semantic and Snapshot-
-- bound; local materialization paths and prompts are intentionally not stored.
CREATE TABLE IF NOT EXISTS review_intelligence_runs (
  review_id         TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  id                TEXT NOT NULL,
  revision_id       TEXT NOT NULL,
  snapshot_identity TEXT NOT NULL,
  capability        TEXT NOT NULL,
  run_key           TEXT NOT NULL,
  scope_digest      TEXT NOT NULL,
  context_digest    TEXT NOT NULL,
  policy_digest     TEXT NOT NULL,
  schema_digest     TEXT NOT NULL,
  driver            TEXT NOT NULL,
  model             TEXT NOT NULL DEFAULT '',
  prompt_version    INTEGER NOT NULL DEFAULT 1,
  schema_version    INTEGER NOT NULL DEFAULT 1,
  status            TEXT NOT NULL,
  result_json       TEXT NOT NULL DEFAULT '{}',
  usage_json        TEXT NOT NULL DEFAULT '{}',
  error             TEXT NOT NULL DEFAULT '',
  model_calls       INTEGER NOT NULL DEFAULT 0,
  created_at        TEXT NOT NULL,
  started_at        TEXT NOT NULL DEFAULT '',
  completed_at      TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (review_id, id)
);
CREATE INDEX IF NOT EXISTS idx_review_intelligence_runs_key
  ON review_intelligence_runs(review_id, run_key, created_at);
CREATE INDEX IF NOT EXISTS idx_review_intelligence_runs_revision
  ON review_intelligence_runs(review_id, revision_id, capability, created_at);

-- Universal model-call spend for Review Intelligence, including legacy Brief
-- synthesis. Cache pruning never resets this counter.
CREATE TABLE IF NOT EXISTS review_intelligence_spend (
  review_id    TEXT NOT NULL PRIMARY KEY REFERENCES review_sessions(id) ON DELETE CASCADE,
  model_calls  INTEGER NOT NULL DEFAULT 0,
  updated_at   TEXT NOT NULL DEFAULT ''
);

-- Plan synthesis cache (review/synthesis.py): one row per exact input key.
-- prune_syntheses keeps a bounded history and the session deletion flow
-- (ON DELETE CASCADE) owns retention. Deleting a row never deletes a judgment.
CREATE TABLE IF NOT EXISTS review_plan_syntheses (
  review_id       TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  id              TEXT NOT NULL,
  revision_id     TEXT NOT NULL,
  synthesis_key   TEXT NOT NULL,
  schema_version  INTEGER NOT NULL DEFAULT 1,
  prompt_version  INTEGER NOT NULL DEFAULT 1,
  model           TEXT NOT NULL DEFAULT '',
  status          TEXT NOT NULL,
  payload_json    TEXT NOT NULL DEFAULT '{}',
  error           TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL,
  completed_at    TEXT NOT NULL DEFAULT '',
  inputs_digest   TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (review_id, id)
);
CREATE INDEX IF NOT EXISTS idx_review_plan_syntheses_key
  ON review_plan_syntheses(review_id, synthesis_key, created_at);
CREATE INDEX IF NOT EXISTS idx_review_plan_syntheses_inputs
  ON review_plan_syntheses(review_id, revision_id, inputs_digest, created_at);

-- Cumulative AI spend per Review (spec §10): survives synthesis-cache pruning;
-- the session deletion flow (ON DELETE CASCADE) owns retention.
CREATE TABLE IF NOT EXISTS review_synthesis_spend (
  review_id    TEXT NOT NULL PRIMARY KEY REFERENCES review_sessions(id) ON DELETE CASCADE,
  model_calls  INTEGER NOT NULL DEFAULT 0,
  updated_at   TEXT NOT NULL DEFAULT ''
);

-- Hook-captured discussion does not require a code Snapshot. Change deletion owns retention.
CREATE TABLE IF NOT EXISTS review_discussion_comments (
  id TEXT PRIMARY KEY,
  review_id TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_discussion_review ON review_discussion_comments(review_id, created_at);
CREATE TABLE IF NOT EXISTS review_comment_imports (
  review_id TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  host TEXT NOT NULL,
  session_id TEXT NOT NULL,
  cursor_json TEXT NOT NULL,
  PRIMARY KEY(review_id, host, session_id)
);

CREATE VIRTUAL TABLE IF NOT EXISTS annotations_fts USING fts5(
  body, path, content=''
);
-- Change-level Development (see review/development.py). Append-only events
-- with a per-Change sequence. A Snapshot freezes its Development by recording
-- the event watermark in the same transaction that creates the revision.
CREATE TABLE IF NOT EXISTS development_sessions (
  change_id         TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  host              TEXT NOT NULL,
  session_id        TEXT NOT NULL,
  parent_session_id TEXT NOT NULL DEFAULT '',
  model             TEXT NOT NULL DEFAULT '',
  purpose           TEXT NOT NULL DEFAULT '',
  role              TEXT NOT NULL DEFAULT 'observer',
  binding           TEXT NOT NULL DEFAULT 'observed',
  task              TEXT NOT NULL DEFAULT '',
  status            TEXT NOT NULL DEFAULT '',
  started_at        TEXT NOT NULL DEFAULT '',
  ended_at          TEXT NOT NULL DEFAULT '',
  cursor_json       TEXT NOT NULL DEFAULT '{}',
  created_at        TEXT NOT NULL,
  updated_at        TEXT NOT NULL,
  PRIMARY KEY (change_id, host, session_id)
);
CREATE INDEX IF NOT EXISTS idx_development_sessions_session
  ON development_sessions(host, session_id);

CREATE TABLE IF NOT EXISTS development_events (
  change_id        TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  sequence         INTEGER NOT NULL,
  event_id         TEXT NOT NULL,
  host             TEXT NOT NULL DEFAULT '',
  session_id       TEXT NOT NULL DEFAULT '',
  kind             TEXT NOT NULL,
  at               TEXT NOT NULL DEFAULT '',
  path             TEXT NOT NULL DEFAULT '',
  symbol           TEXT NOT NULL DEFAULT '',
  target_unit_key  TEXT NOT NULL DEFAULT '',
  summary          TEXT NOT NULL DEFAULT '',
  detail           TEXT NOT NULL DEFAULT '',
  status           TEXT NOT NULL DEFAULT '',
  role             TEXT NOT NULL DEFAULT '',
  source           TEXT NOT NULL DEFAULT '',
  source_event_id  TEXT NOT NULL,
  causation_id     TEXT NOT NULL DEFAULT '',
  parent_event_id  TEXT NOT NULL DEFAULT '',
  annotation_id    TEXT NOT NULL DEFAULT '',
  task_id          TEXT NOT NULL DEFAULT '',
  created_at       TEXT NOT NULL,
  PRIMARY KEY (change_id, sequence),
  UNIQUE (change_id, source_event_id)
);
CREATE INDEX IF NOT EXISTS idx_development_events_session
  ON development_events(change_id, host, session_id, sequence);
CREATE INDEX IF NOT EXISTS idx_development_events_path
  ON development_events(change_id, path, sequence);
CREATE INDEX IF NOT EXISTS idx_development_events_kind
  ON development_events(change_id, kind, sequence);
CREATE INDEX IF NOT EXISTS idx_development_events_causation
  ON development_events(change_id, causation_id);

CREATE TABLE IF NOT EXISTS development_projections (
  change_id  TEXT PRIMARY KEY REFERENCES review_sessions(id) ON DELETE CASCADE,
  cursor     INTEGER NOT NULL DEFAULT 0,
  state_json TEXT NOT NULL DEFAULT '{}',
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS development_snapshots (
  revision_id     TEXT PRIMARY KEY REFERENCES review_revisions(id) ON DELETE CASCADE,
  change_id       TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  watermark       INTEGER NOT NULL DEFAULT 0,
  projection_json TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_development_snapshots_change
  ON development_snapshots(change_id, watermark);
"""

_SESSION_UPDATABLE = (
    "subject_type",
    "title",
    "repo_root",
    "source_ref",
    "range_mode",
    "review_base_sha",
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
    "finding_disposition",
    "finding_disposition_reason",
    "finding_disposition_revision_id",
    "finding_disposition_reviewer_id",
    "finding_disposition_evidence_json",
)


def deletion_tombstone_digest(tombstone: ReviewDeletionTombstone) -> str:
    """SHA-256 over every tombstone field but its own digest (canonical JSON)."""

    canonical = json.dumps(tombstone.digest_fields(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _row_to_deletion_tombstone(row: Any) -> ReviewDeletionTombstone:
    try:
        raw_counts = json.loads(str(row["counts_json"] or "{}"))
    except ValueError:
        raw_counts = {}
    counts = raw_counts if isinstance(raw_counts, dict) else {}
    confirmed_by: Literal["id", "title"] = "title" if str(row["confirmed_by"]) == "title" else "id"
    return ReviewDeletionTombstone(
        review_id=str(row["review_id"]),
        repo_id=str(row["repo_id"]),
        actor_id=str(row["actor_id"]),
        request_id=str(row["request_id"]),
        confirmation_sha256=str(row["confirmation_sha256"]),
        confirmed_by=confirmed_by,
        id=str(row["id"]),
        sequence=int(row["sequence"]),
        counts=tuple(sorted((str(table), int(count)) for table, count in counts.items())),
        deleted_at=str(row["deleted_at"]),
        previous_sha256=str(row["previous_sha256"]),
        record_sha256=str(row["record_sha256"]),
    )


def _row_to_hosted_attachment(row: Any) -> HostedAttachment:
    return HostedAttachment(
        review_id=str(row["review_id"]),
        origin=str(row["origin"]),
        hosted_repo_id=str(row["hosted_repo_id"]),
        hosted_review_id=str(row["hosted_review_id"]),
        principal_id=str(row["principal_id"]),
        projection_json=str(row["projection_json"]),
        attached_at=str(row["attached_at"]),
        synced_at=str(row["synced_at"]),
    )


def _row_to_hosted_action(row: Any) -> HostedAction:
    return HostedAction(
        id=str(row["id"]),
        review_id=str(row["review_id"]),
        kind=cast(HostedActionKind, str(row["kind"])),
        snapshot_id=str(row["snapshot_id"]),
        payload_json=str(row["payload_json"]),
        preconditions_json=str(row["preconditions_json"]),
        sequence=int(row["sequence"]),
        supersede_key=str(row["supersede_key"]),
        state=cast(HostedActionState, str(row["state"])),
        reason=str(row["reason"]),
        result_json=str(row["result_json"]),
        created_at=str(row["created_at"]),
        resolved_at=str(row["resolved_at"]),
    )


def new_hosted_action_id() -> str:
    return f"{_HOSTED_ACTION_ID_PREFIX}-{make_uuid7()}"


def utc_now() -> str:
    """Aware-UTC ISO-8601, the only timestamp format these tables accept.

    Naive local time is unsyncable: two machines write the same string for
    different instants and no later reader can tell them apart.
    """
    return datetime.now(UTC).isoformat()


def _superseded_ranks(stored: Mapping[str, ReviewUnit], projected: Mapping[str, ReviewUnit]) -> dict[str, int]:
    """The ranks an enrichment replaces, by unit key; a unit it adds held its file's place."""

    file_ranks = {unit.path: unit.attention_rank for unit in stored.values() if unit.kind == "file"}
    out: dict[str, int] = {}
    for key, unit in projected.items():
        before = stored.get(key)
        rank = before.attention_rank if before is not None else file_ranks.get(unit.path)
        if rank is not None and rank != unit.attention_rank:
            out[key] = rank
    return out


def _after_timestamp(value: str) -> str:
    """The smallest ISO timestamp strictly after *value* (one microsecond later)."""

    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return utc_now()
    return (parsed + timedelta(microseconds=1)).isoformat()


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


class AmbiguousReviewReference(LookupError):
    """A shortened public Review reference matches more than one durable id."""


def _short_reference_token(identifier: str, *, revision: bool, width: int) -> str:
    """Stable opaque short token derived from the full durable id.

    Storage ids are UUIDv7, whose leading hex is time-ordered and therefore a
    poor short identifier when many Reviews are created close together. Hash the
    canonical durable id first so the public 8-character token is uniformly
    distributed, then extend only on collision.
    """

    canonical = _canonical_storage_id(identifier, revision=revision)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[: max(1, width)]


class ShortReferenceIndex:
    """One uniqueness domain of short references, hashed once for many lookups.

    Naming every Review of a list re-read and re-hashed the whole domain per
    row; this hashes each candidate once and counts a token's holders by
    binary search.
    """

    __slots__ = ("_digests", "_revision")

    def __init__(self, candidate_ids: Iterable[str], *, revision: bool) -> None:
        self._revision = revision
        canonical = (_canonical_storage_id(item, revision=revision) for item in candidate_ids)
        self._digests = sorted(hashlib.sha256(item.encode("utf-8")).hexdigest() for item in canonical if item)

    def shortest(self, identifier: str, *, min_chars: int = 8) -> str:
        """Shortest collision-free token for *identifier* (normally 8 hex chars)."""

        canonical = _canonical_storage_id(identifier, revision=self._revision)
        if not canonical:
            return identifier
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        for width in range(max(8, min_chars), 65):
            token = digest[:width]
            # Every lowercase-hex digest starting with token sorts in [token, token + "g").
            if bisect_left(self._digests, token + "g") - bisect_left(self._digests, token) == 1:
                return token
        return digest


def _short_unique_reference(
    identifier: str,
    candidate_ids: Sequence[str],
    *,
    revision: bool,
    min_chars: int = 8,
) -> str:
    return ShortReferenceIndex(candidate_ids, revision=revision).shortest(identifier, min_chars=min_chars)


def short_review_reference(review_id: str, candidate_ids: Sequence[str], *, min_chars: int = 8) -> str:
    """Shortest collision-free public Review token (normally 8 hex chars)."""

    return _short_unique_reference(review_id, candidate_ids, revision=False, min_chars=min_chars)


def short_revision_reference(revision_id: str, candidate_ids: Sequence[str], *, min_chars: int = 8) -> str:
    """Shortest collision-free public Snapshot token (normally 8 hex chars)."""

    return _short_unique_reference(revision_id, candidate_ids, revision=True, min_chars=min_chars)


def public_review_reference(review_id: str) -> str:
    """Canonical full Review reference; UI routes normally use a short token."""

    return f"{_SESSION_REF_PREFIX}{review_id}"


def public_revision_reference(revision_id: str) -> str:
    """Canonical public Review-revision reference for one internal revision id."""

    return f"{_REVISION_REF_PREFIX}{revision_id}"


def new_session_id() -> str:
    return make_uuid7().replace("-", "")


def new_revision_id() -> str:
    return make_uuid7().replace("-", "")


def new_binding_id() -> str:
    return f"{_BINDING_ID_PREFIX}-{make_uuid7()}"


def new_relationship_id() -> str:
    return f"{_RELATIONSHIP_ID_PREFIX}-{make_uuid7()}"


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


def new_ask_conversation_id() -> str:
    return f"{_ASK_CONVERSATION_ID_PREFIX}-{make_uuid7()}"


def new_ask_message_id() -> str:
    return f"{_ASK_MESSAGE_ID_PREFIX}-{make_uuid7()}"


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


# The Snapshot columns every unit rewrite changes in the same transaction.
_UNITS_STAMP_COLUMNS: tuple[str, ...] = (
    "packet_sha256",
    "packet_path",
    "packet_schema_version",
    "degraded_json",
    "provenance_host",
    "provenance_model",
    "provenance_session_id",
    "provenance_certainty",
)


@dataclass(frozen=True)
class ReviewListInputs:
    """What one Review list row derives from; read together for many Reviews."""

    revision: ReviewRevision | None
    attributions: tuple[ReviewAttribution, ...]
    marks: tuple[ReviewMark, ...]
    annotations: tuple[Annotation, ...]


@dataclass(frozen=True)
class DerivedResult:
    """One stored derivation of a Snapshot; ``stamp`` identifies its exact inputs."""

    review_id: str
    revision_id: str
    kind: str
    scope_key: str
    stamp: str
    payload: Mapping[str, Any]


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
            turn_owner_kind, turn_owner_id,
            finding_disposition, finding_disposition_reason,
            finding_disposition_revision_id, finding_disposition_reviewer_id,
            finding_disposition_evidence_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            record.finding_disposition,
            record.finding_disposition_reason,
            record.finding_disposition_revision_id,
            record.finding_disposition_reviewer_id,
            _dump_tuple(record.finding_disposition_evidence),
            record.created_at,
        ),
    )
    return record


class ReviewStore(SqliteTableStore):
    """Durable review sessions, revisions, units, marks and annotations."""

    SCHEMA = SCHEMA
    # The Reader issues dozens of small reads per page across many requests;
    # reuse connections rather than reconnect (and re-parse ~30 tables) per read.
    POOLED_CONNECTIONS: ClassVar[int] = 2
    DRIVER_ERRORS: ClassVar[tuple[type[Exception], ...]] = (sqlite3.Error,)
    """Database-driver failures a hosted subclass may raise from ``_transaction``."""

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
        "v3_014_review_attributions.sql",
        "v3_015_review_ask_scope.sql",
        "v3_016_change_identity.sql",
        "v3_017_review_ask_context.sql",
        "v3_018_review_source_view.sql",
        "v3_019_review_development.sql",
        "v3_020_review_development_since_cache.sql",
        "v3_021_review_commit_model.sql",
        "v3_022_review_binding_active_unique.sql",
        "v3_023_review_author_guidance.sql",
        "v3_024_review_decision_events.sql",
        "v3_025_review_plan_syntheses.sql",
        "v3_026_review_synthesis_spend.sql",
        "v3_027_review_derived.sql",
        "v3_028_review_destinations.sql",
        "v3_029_review_base_policy.sql",
        "v3_030_review_agent_comments.sql",
        "v3_031_review_revision_title.sql",
        "v3_032_review_intelligence.sql",
        "v3_033_review_attestations.sql",
        "v3_034_review_rules.sql",
        "v3_035_review_finding_dispositions.sql",
        "v3_036_review_evidence_executor_class.sql",
        "v3_037_review_deletion_tombstones.sql",
        "v3_038_review_ask_conversations.sql",
        "v3_039_review_hosted_attachment.sql",
        "v3_040_project_knowledge.sql",
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
        "review_bindings",
        "review_revisions",
        "review_commits",
        "review_relationships",
        "review_units",
        "review_attributions",
        "review_marks",
        "review_mark_events",
        "review_activity_events",
        "review_ask_conversations",
        "review_ask_messages",
        "review_ask_handoffs",
        "review_outcomes",
        "review_frontiers",
        "review_discarded_marks",
        "annotations",
        "review_rule_suggestion_states",
        "review_change_proposals",
        "annotation_versions",
        "annotation_anchor_events",
        "deliveries",
        "review_evidence",
        "review_author_guidance",
        "review_decision_events",
        "review_plan_syntheses",
        "review_synthesis_spend",
        "review_intelligence_runs",
        "review_intelligence_spend",
        "review_derived",
        "review_destinations",
        "review_hosted_attachments",
        "review_hosted_actions",
        "project_knowledge_meta",
        "project_knowledge_entries",
        "project_knowledge_events",
        "annotations_fts",
        "review_discussion_comments",
        "review_comment_imports",
        "development_sessions",
        "development_events",
        "development_projections",
        "development_snapshots",
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
        # Explicit native attribute: production wheels mypyc-compile ReviewStore,
        # whose instances have no __dict__ for dynamic per-store caches.
        self._session_comment_ingestors: dict[str, Any] = {}
        self._ready = False
        # Units per Snapshot, reused while the Snapshot row is unchanged; see list_units.
        self._units_memo: OrderedDict[str, tuple[tuple[Any, ...], tuple[ReviewUnit, ...]]] = OrderedDict()
        self._units_memo_lock = threading.Lock()

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
            ("review_relationships", "review_id"),
            ("review_relationships", "target_review_id"),
            ("review_marks", "review_id"),
            ("review_mark_events", "review_id"),
            ("review_activity_events", "review_id"),
            ("review_ask_conversations", "review_id"),
            ("review_ask_messages", "review_id"),
            ("review_frontiers", "review_id"),
            ("review_discarded_marks", "review_id"),
            ("annotations", "review_id"),
            ("review_discussion_comments", "review_id"),
            ("review_comment_imports", "review_id"),
            ("review_change_proposals", "review_id"),
            ("annotation_versions", "review_id"),
            ("review_evidence", "review_id"),
            ("review_author_guidance", "review_id"),
            ("review_decision_events", "review_id"),
            ("review_plan_syntheses", "review_id"),
            ("review_synthesis_spend", "review_id"),
            ("review_derived", "review_id"),
            ("review_destinations", "review_id"),
            ("review_hosted_attachments", "review_id"),
            ("review_hosted_actions", "review_id"),
            ("review_outcomes", "review_id"),
            ("development_sessions", "change_id"),
            ("development_events", "change_id"),
            ("development_projections", "change_id"),
            ("development_snapshots", "change_id"),
        )

    def _identity_revision_reference_columns(self) -> tuple[tuple[str, str], ...]:
        """Revision-id columns owned by this store composition."""

        return (
            ("review_sessions", "current_revision_id"),
            ("review_units", "revision_id"),
            ("review_attributions", "revision_id"),
            ("review_marks", "reviewed_revision_id"),
            ("review_mark_events", "revision_id"),
            ("review_mark_events", "reviewed_revision_id"),
            ("review_activity_events", "revision_id"),
            ("review_ask_messages", "revision_id"),
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
            ("review_author_guidance", "revision_id"),
            ("review_decision_events", "revision_id"),
            ("review_plan_syntheses", "revision_id"),
            ("review_derived", "revision_id"),
            ("review_outcomes", "revision_id"),
            ("development_snapshots", "revision_id"),
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
                    "UPDATE review_activity_events SET subject_id = ? WHERE subject_type = 'review' AND subject_id = ?",
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
                    "UPDATE review_revisions SET packet_path = replace(packet_path, ?, ?) WHERE packet_path LIKE ?",
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

    @contextlib.contextmanager
    def project_knowledge_scope(self, *, write: bool = False) -> Iterator[Any]:
        """One repository-scoped transaction for rebuildable Project Knowledge.

        Hosted PostgresReviewStore inherits this method and supplies its RLS-scoped
        ``_transaction``; local ReviewStore supplies the existing SQLite
        transaction. Project Knowledge therefore reuses the Review storage plane
        without becoming Review authority.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            if write:
                self._lock_for_write(conn, key="project-knowledge")
            yield conn

    # ----- sessions ------------------------------------------------------- #

    def create_session(self, session: ReviewSession) -> ReviewSession:
        """Insert a session, filling id and timestamps when blank.

        ``source_ref`` is retained as legacy/source-control context, but it is
        no longer unique. Explicit ReviewBinding rows own active branch/PR
        routing, which allows a branch to accumulate several finished Reviews.
        """
        self._ensure_ready()
        with self._transaction() as conn:
            return self._insert_session(conn, session)

    def create_bound_session(self, session: ReviewSession, binding: ReviewBinding) -> tuple[ReviewSession, bool]:
        """Create *session* as the subject's active Review, or join the one a racer bound.

        Lookup, insert and bind share one write-locked transaction, so two
        captures of the same branch converge on one Review instead of each
        creating one and the later bind orphaning the earlier. Returns
        ``(session, created)``; ``created`` is ``False`` when an open or
        discarded Review already owned the subject.
        """

        self._ensure_ready()
        repository_identity = binding.repository_identity.strip()
        subject_key = binding.subject_key.strip()
        if not repository_identity or not subject_key:
            raise ValueError("review binding requires repository_identity and subject_key")
        with self._transaction() as conn:
            self._lock_for_write(conn, key=_binding_lock_key(repository_identity, binding.subject_kind, subject_key))
            row = conn.execute(
                """
                SELECT s.* FROM review_bindings AS b
                  JOIN review_sessions AS s ON s.id = b.review_id
                 WHERE b.repository_identity = ? AND b.subject_kind = ? AND b.subject_key = ? AND b.active = 1
                   AND s.status IN ('open', 'archived')
                 ORDER BY b.updated_at DESC, b.id DESC LIMIT 1
                """,
                (repository_identity, binding.subject_kind, subject_key),
            ).fetchone()
            if row is not None:
                return _row_to_session(row), False
            record = self._insert_session(conn, session)
            self._bind_subject_locked(
                conn,
                ReviewBinding(
                    id=binding.id or new_binding_id(),
                    repository_identity=repository_identity,
                    subject_kind=binding.subject_kind,
                    subject_key=subject_key,
                    review_id=record.id,
                    worktree_key=binding.worktree_key.strip(),
                ),
            )
        return record, True

    def find_or_create_session(
        self,
        session: ReviewSession,
        *,
        reuse: Sequence[str] = ("open", "finished", "archived"),
        never_bound: bool = False,
    ) -> tuple[ReviewSession, bool]:
        """The Review with *session*'s reopen identity, or *session* inserted.

        ``(repo_root, subject_type, source_ref, range_mode)`` is no longer a
        unique index (bindings route branches), so lookup and insert share one
        write-locked transaction: concurrent captures of one unbound subject
        converge on one Review. An existing Review is returned only when its
        status is in ``reuse``. ``never_bound`` ignores Reviews a ReviewBinding
        ever owned, as :meth:`find_session` does. Returns ``(session, created)``.
        """

        self._ensure_ready()
        identity = (session.repo_root, session.subject_type, session.source_ref, session.range_mode)
        unbound = (
            " AND NOT EXISTS (SELECT 1 FROM review_bindings AS b WHERE b.review_id = review_sessions.id)"
            if never_bound
            else ""
        )
        with self._transaction() as conn:
            self._lock_for_write(conn, key="review-subject:" + "\x1f".join(identity))
            row = conn.execute(
                f"""
                SELECT * FROM review_sessions
                 WHERE repo_root = ? AND subject_type = ? AND source_ref = ? AND range_mode = ?{unbound}
                 ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, updated_at DESC, id DESC
                 LIMIT 1
                """,
                identity,
            ).fetchone()
            if row is not None and str(row["status"]) in reuse:
                return _row_to_session(row), False
            return self._insert_session(conn, session), True

    def _insert_session(self, conn: sqlite3.Connection, session: ReviewSession) -> ReviewSession:
        now = utc_now()
        record = ReviewSession(
            id=session.id or new_session_id(),
            subject_type=session.subject_type,
            repo_root=session.repo_root,
            range_mode=session.range_mode,
            title=session.title,
            source_ref=session.source_ref,
            review_base_sha=session.review_base_sha,
            actor_type=session.actor_type,
            status=session.status,
            reviewer_id=session.reviewer_id,
            current_revision_id=session.current_revision_id,
            created_at=session.created_at or now,
            updated_at=session.updated_at or now,
            review_base_policy=session.review_base_policy,
        )
        conn.execute(
            """
            INSERT INTO review_sessions (
                id, subject_type, title, repo_root, source_ref, range_mode, review_base_sha,
                actor_type, status, reviewer_id, current_revision_id, created_at, updated_at, review_base_policy
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.id,
                record.subject_type,
                record.title,
                record.repo_root,
                record.source_ref,
                record.range_mode,
                record.review_base_sha,
                record.actor_type,
                record.status,
                record.reviewer_id,
                record.current_revision_id,
                record.created_at,
                record.updated_at,
                record.review_base_policy,
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
        """Resolve a full id, ``r/<id>``, or collision-safe short token."""

        exact = self.get_session(reference)
        if exact is not None:
            return exact
        token = reference[len(_SESSION_REF_PREFIX) :] if reference.startswith(_SESSION_REF_PREFIX) else reference
        if not token:
            return None
        if exact := self.get_session(token):
            return exact
        # One identity (G5-SYNC): an attached Review shows the hosted reference,
        # so the exact hosted id must route back to the local Review it names.
        if self.hosted_attachment_supported() and (attached := self.find_hosted_attachment(token)) is not None:
            return self.get_session(attached.review_id)
        if len(token) < 8 or any(char not in "0123456789abcdef" for char in token.lower()):
            return None
        rows = self.list_sessions(status="", limit=None)
        matches = [
            row for row in rows if _short_reference_token(row.id, revision=False, width=len(token)) == token.lower()
        ]
        if len(matches) > 1:
            raise AmbiguousReviewReference(f"ambiguous Review reference {reference!r}")
        return matches[0] if matches else None

    def session_references(self) -> ShortReferenceIndex:
        """Every Review's short-reference domain: read once to name many Reviews."""

        return ShortReferenceIndex((row.id for row in self.list_sessions(status="", limit=None)), revision=False)

    def short_session_reference(self, review_id: str, *, min_chars: int = 8) -> str:
        return self.session_references().shortest(review_id, min_chars=min_chars)

    def session_reference(self, review_id: str) -> str:
        """Return the short canonical public ref for one durable Review id."""

        return f"{_SESSION_REF_PREFIX}{self.short_session_reference(review_id)}"

    def bind_subject(self, binding: ReviewBinding) -> ReviewBinding:
        """Make *binding.review_id* the one active Review for a logical source subject.

        Historical bindings are retained with ``active=0`` so reusing a branch
        after finishing a Review never erases which earlier Reviews belonged to it.
        """

        self._ensure_ready()
        repository_identity = binding.repository_identity.strip()
        subject_key = binding.subject_key.strip()
        if not repository_identity or not subject_key:
            raise ValueError("review binding requires repository_identity and subject_key")
        record = ReviewBinding(
            id=binding.id or new_binding_id(),
            repository_identity=repository_identity,
            subject_kind=binding.subject_kind,
            subject_key=subject_key,
            review_id=binding.review_id,
            worktree_key=binding.worktree_key.strip(),
            created_at=binding.created_at,
        )
        with self._transaction() as conn:
            self._lock_for_write(conn, key=_binding_lock_key(repository_identity, binding.subject_kind, subject_key))
            if conn.execute("SELECT 1 FROM review_sessions WHERE id = ?", (record.review_id,)).fetchone() is None:
                raise ValueError(f"no such review session {record.review_id!r}")
            return self._bind_subject_locked(conn, record)

    def _bind_subject_locked(self, conn: sqlite3.Connection, record: ReviewBinding) -> ReviewBinding:
        now = utc_now()
        conn.execute(
            """
            UPDATE review_bindings SET active = 0, updated_at = ?
             WHERE repository_identity = ? AND subject_kind = ? AND subject_key = ?
               AND active = 1 AND review_id <> ?
            """,
            (now, record.repository_identity, record.subject_kind, record.subject_key, record.review_id),
        )
        existing = conn.execute(
            """
            SELECT id FROM review_bindings
             WHERE repository_identity = ? AND subject_kind = ? AND subject_key = ? AND review_id = ?
            """,
            (record.repository_identity, record.subject_kind, record.subject_key, record.review_id),
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO review_bindings (
                    id, repository_identity, subject_kind, subject_key, review_id,
                    worktree_key, active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    record.id,
                    record.repository_identity,
                    record.subject_kind,
                    record.subject_key,
                    record.review_id,
                    record.worktree_key,
                    record.created_at or now,
                    now,
                ),
            )
        else:
            conn.execute(
                "UPDATE review_bindings SET worktree_key = ?, active = 1, updated_at = ? WHERE id = ?",
                (record.worktree_key, now, str(existing["id"])),
            )
        row = conn.execute(
            """
            SELECT * FROM review_bindings
             WHERE repository_identity = ? AND subject_kind = ? AND subject_key = ? AND review_id = ?
            """,
            (record.repository_identity, record.subject_kind, record.subject_key, record.review_id),
        ).fetchone()
        if row is None:  # pragma: no cover - written and selected in one transaction
            raise RuntimeError("review binding disappeared after bind")
        return _row_to_binding(row)

    def active_binding(
        self,
        *,
        repository_identity: str,
        subject_kind: str,
        subject_key: str,
    ) -> ReviewBinding | None:
        """Return the active Review binding for one branch/PR/range subject."""

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM review_bindings
                 WHERE repository_identity = ? AND subject_kind = ? AND subject_key = ? AND active = 1
                 ORDER BY updated_at DESC, id DESC LIMIT 1
                """,
                (repository_identity.strip(), subject_kind, subject_key.strip()),
            ).fetchone()
        return None if row is None else _row_to_binding(row)

    def latest_binding(
        self,
        *,
        repository_identity: str,
        subject_kind: str,
        subject_key: str,
    ) -> ReviewBinding | None:
        """The subject's most recent binding, active or released (for reopening)."""

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM review_bindings
                 WHERE repository_identity = ? AND subject_kind = ? AND subject_key = ?
                 ORDER BY active DESC, updated_at DESC, id DESC LIMIT 1
                """,
                (repository_identity.strip(), subject_kind, subject_key.strip()),
            ).fetchone()
        return None if row is None else _row_to_binding(row)

    def list_bindings(self, review_id: str, *, active_only: bool = False) -> tuple[ReviewBinding, ...]:
        self._ensure_ready()
        sql = "SELECT * FROM review_bindings WHERE review_id = ?"
        if active_only:
            sql += " AND active = 1"
        sql += " ORDER BY updated_at DESC, id DESC"
        with self._transaction() as conn:
            rows = conn.execute(sql, (review_id,)).fetchall()
        return tuple(_row_to_binding(row) for row in rows)

    def unbind_review(self, review_id: str) -> int:
        """Deactivate every active subject binding owned by *review_id*."""

        self._ensure_ready()
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE review_bindings SET active = 0, updated_at = ? WHERE review_id = ? AND active = 1",
                (utc_now(), review_id),
            ).rowcount
        return int(changed or 0)

    def restore_binding(self, review_id: str) -> ReviewBinding | None:
        """Re-activate *review_id*'s most recent binding while its subject is free.

        A Review reopened after finishing takes its branch back, unless another
        Review has become that branch's active one since; ``None`` then.
        """

        latest = self.list_bindings(review_id)
        if not latest:
            return None
        binding = latest[0]
        if binding.active:
            return binding
        key = (binding.repository_identity, binding.subject_kind, binding.subject_key)
        self._ensure_ready()
        with self._transaction() as conn:
            self._lock_for_write(conn, key=_binding_lock_key(*key))
            holder = conn.execute(
                """
                SELECT 1 FROM review_bindings
                 WHERE repository_identity = ? AND subject_kind = ? AND subject_key = ? AND active = 1
                """,
                key,
            ).fetchone()
            if holder is not None:
                return None
            conn.execute(
                "UPDATE review_bindings SET active = 1, updated_at = ? WHERE id = ?",
                (utc_now(), binding.id),
            )
        return replace(binding, active=True)

    def upsert_relationship(self, relationship: ReviewRelationship) -> ReviewRelationship:
        """Persist context without taking ownership of the external workflow."""

        self._ensure_ready()
        now = utc_now()
        record = ReviewRelationship(
            id=relationship.id or new_relationship_id(),
            review_id=relationship.review_id,
            kind=relationship.kind.strip(),
            relation=relationship.relation.strip(),
            provider=relationship.provider.strip(),
            external_id=relationship.external_id.strip()
            or (relationship.url.strip() if not relationship.target_review_id.strip() else ""),
            target_review_id=relationship.target_review_id.strip(),
            url=relationship.url.strip(),
            title=relationship.title.strip(),
            status=relationship.status.strip(),
            source=relationship.source.strip() or "explicit",
            metadata_json=relationship.metadata_json or "{}",
            created_at=relationship.created_at or now,
            updated_at=now,
        )
        if not record.kind or not record.relation:
            raise ValueError("relationship kind and relation are required")
        if record.target_review_id == record.review_id:
            raise ValueError("a Change cannot relate to itself")
        if record.target_review_id and self.get_session(record.target_review_id) is None:
            raise ValueError(f"no related Change {record.target_review_id!r}")
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO review_relationships (
                    id, review_id, kind, relation, provider, external_id,
                    target_review_id, url, title, status, source, metadata_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_id, kind, relation, provider, external_id, target_review_id)
                DO UPDATE SET url = excluded.url, title = excluded.title,
                              status = excluded.status, source = excluded.source,
                              metadata_json = excluded.metadata_json, updated_at = excluded.updated_at
                """,
                (
                    record.id,
                    record.review_id,
                    record.kind,
                    record.relation,
                    record.provider,
                    record.external_id,
                    record.target_review_id,
                    record.url,
                    record.title,
                    record.status,
                    record.source,
                    record.metadata_json,
                    record.created_at,
                    record.updated_at,
                ),
            )
            row = conn.execute(
                """
                SELECT * FROM review_relationships
                 WHERE review_id = ? AND kind = ? AND relation = ? AND provider = ?
                   AND external_id = ? AND target_review_id = ?
                """,
                (
                    record.review_id,
                    record.kind,
                    record.relation,
                    record.provider,
                    record.external_id,
                    record.target_review_id,
                ),
            ).fetchone()
        if row is None:
            raise RuntimeError("relationship disappeared after upsert")
        return _row_to_relationship(row)

    def delete_relationship(
        self,
        review_id: str,
        *,
        kind: str,
        relation: str,
        provider: str,
        external_id: str,
        target_review_id: str = "",
    ) -> bool:
        """Remove one relationship by its natural key; ``False`` when no such row exists."""

        self._ensure_ready()
        with self._transaction() as conn:
            deleted = conn.execute(
                """
                DELETE FROM review_relationships
                 WHERE review_id = ? AND kind = ? AND relation = ? AND provider = ?
                   AND external_id = ? AND target_review_id = ?
                """,
                (
                    review_id,
                    kind.strip(),
                    relation.strip(),
                    provider.strip(),
                    external_id.strip(),
                    target_review_id.strip(),
                ),
            ).rowcount
        return bool(deleted)

    def list_relationships(self, review_id: str, *, kind: str = "") -> tuple[ReviewRelationship, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM review_relationships WHERE review_id = ? AND kind = ? ORDER BY created_at, id",
                    (review_id, kind),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM review_relationships WHERE review_id = ? ORDER BY kind, created_at, id",
                    (review_id,),
                ).fetchall()
        return tuple(_row_to_relationship(row) for row in rows)

    def get_destination(self, review_id: str) -> ReviewDestination:
        """The Review's feedback destination; ``auto`` when nothing is pinned."""

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM review_destinations WHERE review_id = ?", (review_id,)).fetchone()
        if row is None:
            return ReviewDestination(review_id=review_id)
        return ReviewDestination(
            review_id=str(row["review_id"]),
            kind=cast(ReviewDestinationKind, str(row["kind"])),
            host=str(row["host"]),
            session_id=str(row["session_id"]),
            external_ref=str(row["external_ref"]),
            set_by=str(row["set_by"]),
            updated_at=str(row["updated_at"]),
        )

    def set_destination(self, destination: ReviewDestination) -> ReviewDestination:
        """Pin *destination*; ``auto`` unpins by deleting the row."""

        self._ensure_ready()
        if destination.kind not in REVIEW_DESTINATION_KINDS:
            raise ValueError(f"unknown review destination {destination.kind!r}")
        with self._transaction() as conn:
            if conn.execute("SELECT 1 FROM review_sessions WHERE id = ?", (destination.review_id,)).fetchone() is None:
                raise ValueError(f"no review {destination.review_id!r}")
            if destination.kind == "auto":
                conn.execute("DELETE FROM review_destinations WHERE review_id = ?", (destination.review_id,))
            else:
                conn.execute(
                    """
                    INSERT INTO review_destinations (
                        review_id, kind, host, session_id, external_ref, set_by, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(review_id) DO UPDATE SET
                        kind = excluded.kind, host = excluded.host, session_id = excluded.session_id,
                        external_ref = excluded.external_ref, set_by = excluded.set_by,
                        updated_at = excluded.updated_at
                    """,
                    (
                        destination.review_id,
                        destination.kind,
                        destination.host.strip().lower(),
                        destination.session_id.strip(),
                        destination.external_ref.strip(),
                        destination.set_by.strip(),
                        utc_now(),
                    ),
                )
        return self.get_destination(destination.review_id)

    # ----- local <-> hosted attachment (G5-SYNC) ------------------------- #

    def hosted_attachment_supported(self) -> bool:
        """Whether this store can attach its Reviews to a hosted Review.

        Only a local store can: a hosted store *is* the authority and keeps no
        projection or replay queue of another server.
        """

        return True

    def get_hosted_attachment(self, review_id: str) -> HostedAttachment | None:
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute("SELECT * FROM review_hosted_attachments WHERE review_id = ?", (review_id,)).fetchone()
        return None if row is None else _row_to_hosted_attachment(row)

    def find_hosted_attachment(self, hosted_review_id: str) -> HostedAttachment | None:
        """The attachment naming exactly *hosted_review_id*, or ``None``.

        :meth:`attach_hosted` keeps one attachment per hosted id; a store that
        still holds two (older data, two origins minting the same id) makes the
        reference ambiguous, and that fails closed.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM review_hosted_attachments WHERE hosted_review_id = ? ORDER BY review_id LIMIT 2",
                (hosted_review_id,),
            ).fetchall()
        if len(rows) > 1:
            raise AmbiguousReviewReference(f"ambiguous hosted Review reference {hosted_review_id!r}")
        return _row_to_hosted_attachment(rows[0]) if rows else None

    def hosted_attachments(self) -> dict[str, HostedAttachment]:
        """Every attachment in this store, by local Review id (one read for a list page)."""

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute("SELECT * FROM review_hosted_attachments").fetchall()
        return {str(row["review_id"]): _row_to_hosted_attachment(row) for row in rows}

    def attach_hosted(self, attachment: HostedAttachment) -> tuple[HostedAttachment, bool]:
        """Explicitly attach a local Review; ``(stored, created)``.

        Re-attaching the same hosted identity only refreshes the projection.
        A different hosted Review, origin or principal is refused: the reviewer
        must detach first, so no queued action silently changes target.
        """

        self._ensure_ready()
        identity = (
            attachment.origin.strip(),
            attachment.hosted_repo_id.strip(),
            attachment.hosted_review_id.strip(),
            attachment.principal_id.strip(),
        )
        if not all(identity):
            raise ValueError("a hosted attachment needs origin, hosted_repo_id, hosted_review_id and principal_id")
        now = utc_now()
        with self._transaction() as conn:
            self._lock_for_write(conn)
            if conn.execute("SELECT 1 FROM review_sessions WHERE id = ?", (attachment.review_id,)).fetchone() is None:
                raise ValueError(f"no review {attachment.review_id!r}")
            row = conn.execute(
                "SELECT * FROM review_hosted_attachments WHERE review_id = ?", (attachment.review_id,)
            ).fetchone()
            if row is not None:
                current = _row_to_hosted_attachment(row)
                if (current.origin, current.hosted_repo_id, current.hosted_review_id, current.principal_id) != identity:
                    raise HostedAttachmentConflict(
                        "already_attached",
                        f"this Review is already attached to hosted r/{current.hosted_review_id}; detach it first",
                    )
                conn.execute(
                    "UPDATE review_hosted_attachments SET projection_json = ?, synced_at = ? WHERE review_id = ?",
                    (attachment.projection_json, now, attachment.review_id),
                )
                created = False
            else:
                # The hosted id is an attached Review's one identity in this store
                # (inbox rows, routes): never two local Reviews showing the same id,
                # even from another origin or repository.
                taken = conn.execute(
                    "SELECT review_id FROM review_hosted_attachments WHERE hosted_review_id = ?", (identity[2],)
                ).fetchone()
                if taken is not None:
                    raise HostedAttachmentConflict(
                        "hosted_review_taken",
                        f"hosted r/{identity[2]} is already attached to another local Review; detach that one first",
                    )
                conn.execute(
                    """
                    INSERT INTO review_hosted_attachments (
                        review_id, origin, hosted_repo_id, hosted_review_id, principal_id,
                        projection_json, attached_at, synced_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (attachment.review_id, *identity, attachment.projection_json, now, now),
                )
                created = True
            stored = conn.execute(
                "SELECT * FROM review_hosted_attachments WHERE review_id = ?", (attachment.review_id,)
            ).fetchone()
        return _row_to_hosted_attachment(stored), created

    @staticmethod
    def _unresolved_hosted_actions(conn: Any, review_id: str) -> int:
        """Pending + conflicted actions queued for hosted: the reviewer's unsent work."""

        unresolved_in = ", ".join("?" for _ in HOSTED_ACTION_UNRESOLVED)
        return int(
            conn.execute(
                f"SELECT COUNT(*) AS n FROM review_hosted_actions WHERE review_id = ? AND state IN ({unresolved_in})",
                (review_id, *HOSTED_ACTION_UNRESOLVED),
            ).fetchone()["n"]
        )

    def detach_hosted(self, review_id: str, *, discard_unresolved: bool = False) -> tuple[int, int]:
        """Remove the attachment and its queue; ``(unresolved discarded, resolved removed)``.

        Pending or conflicted actions are the reviewer's unsent work, so they
        are dropped only when the caller says so explicitly. The local Review
        and every local mark, comment and outcome recorded before attaching
        are kept; whatever was applied on hosted stays on hosted.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            self._lock_for_write(conn)
            if (
                conn.execute("SELECT 1 FROM review_hosted_attachments WHERE review_id = ?", (review_id,)).fetchone()
                is None
            ):
                raise HostedAttachmentConflict("not_attached", "this Review is not attached to a hosted Review")
            unresolved = self._unresolved_hosted_actions(conn, review_id)
            if unresolved and not discard_unresolved:
                raise HostedAttachmentConflict(
                    "unresolved_actions",
                    f"{unresolved} queued action(s) were never applied on hosted; sync or discard them before detaching",
                )
            removed = conn.execute("DELETE FROM review_hosted_actions WHERE review_id = ?", (review_id,)).rowcount
            conn.execute("DELETE FROM review_hosted_attachments WHERE review_id = ?", (review_id,))
        return unresolved, int(removed) - unresolved

    def add_hosted_action(
        self,
        action: HostedAction,
        *,
        unresolved_limit: int,
        supersede: bool = False,
    ) -> tuple[HostedAction, tuple[str, ...]]:
        """Queue one action; ``(stored, superseded action ids)``.

        With *supersede*, unresolved actions under the same ``supersede_key``
        (a newer judgment on the same unit, a newer outcome) are discarded with
        reason ``superseded`` in the same transaction. Without it an unresolved
        action under the key refuses the new one. The queue never holds more
        than *unresolved_limit* pending or conflicted actions.
        """

        self._ensure_ready()
        if action.kind not in HOSTED_ACTION_KINDS:
            raise ValueError(f"unknown hosted action kind {action.kind!r}")
        if unresolved_limit < 1:
            raise ValueError("unresolved_limit must be positive")
        action_id = action.id or new_hosted_action_id()
        now = utc_now()
        unresolved_in = ", ".join("?" for _ in HOSTED_ACTION_UNRESOLVED)
        superseded: list[str] = []
        with self._transaction() as conn:
            self._lock_for_write(conn)
            if (
                conn.execute(
                    "SELECT 1 FROM review_hosted_attachments WHERE review_id = ?", (action.review_id,)
                ).fetchone()
                is None
            ):
                raise HostedAttachmentConflict("not_attached", "this Review is not attached to a hosted Review")
            if action.supersede_key:
                rivals = conn.execute(
                    f"SELECT id FROM review_hosted_actions WHERE review_id = ? AND supersede_key = ?"
                    f" AND state IN ({unresolved_in}) ORDER BY sequence",
                    (action.review_id, action.supersede_key, *HOSTED_ACTION_UNRESOLVED),
                ).fetchall()
                if rivals and not supersede:
                    raise HostedAttachmentConflict(
                        "duplicate_action", "an unresolved action for the same target is already queued"
                    )
                superseded = [str(row["id"]) for row in rivals]
                for rival in superseded:
                    conn.execute(
                        "UPDATE review_hosted_actions SET state = 'discarded', reason = 'superseded',"
                        " result_json = ?, resolved_at = ? WHERE id = ?",
                        (json.dumps({"superseded_by": action_id}, sort_keys=True), now, rival),
                    )
            unresolved = self._unresolved_hosted_actions(conn, action.review_id)
            if unresolved >= unresolved_limit:
                raise HostedAttachmentConflict(
                    "queue_full",
                    f"{unresolved} actions are already waiting for hosted; sync, discard or resolve them first",
                )
            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) AS n FROM review_hosted_actions WHERE review_id = ?",
                    (action.review_id,),
                ).fetchone()["n"]
            )
            conn.execute(
                """
                INSERT INTO review_hosted_actions (
                    id, review_id, sequence, kind, snapshot_id, supersede_key, payload_json,
                    preconditions_json, state, reason, result_json, created_at, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', '', '{}', ?, '')
                """,
                (
                    action_id,
                    action.review_id,
                    sequence + 1,
                    action.kind,
                    action.snapshot_id,
                    action.supersede_key,
                    action.payload_json,
                    action.preconditions_json,
                    now,
                ),
            )
            row = conn.execute("SELECT * FROM review_hosted_actions WHERE id = ?", (action_id,)).fetchone()
        return _row_to_hosted_action(row), tuple(superseded)

    def list_hosted_actions(self, review_id: str, *, states: Sequence[str] = ()) -> tuple[HostedAction, ...]:
        """Queued actions in replay (sequence) order, optionally only *states*."""

        self._ensure_ready()
        with self._transaction() as conn:
            if states:
                marks_in = ", ".join("?" for _ in states)
                rows = conn.execute(
                    f"SELECT * FROM review_hosted_actions WHERE review_id = ? AND state IN ({marks_in})"
                    " ORDER BY sequence",
                    (review_id, *states),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM review_hosted_actions WHERE review_id = ? ORDER BY sequence", (review_id,)
                ).fetchall()
        return tuple(_row_to_hosted_action(row) for row in rows)

    def get_hosted_action(self, review_id: str, action_id: str) -> HostedAction | None:
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM review_hosted_actions WHERE review_id = ? AND id = ?", (review_id, action_id)
            ).fetchone()
        return None if row is None else _row_to_hosted_action(row)

    def transition_hosted_action(
        self,
        review_id: str,
        action_id: str,
        *,
        expected_state: str,
        state: str,
        reason: str = "",
        result_json: str = "{}",
    ) -> HostedAction:
        """Move one action by compare-and-set on its state; a lost race raises.

        Nothing moves back to ``pending``: carrying work forward is a requeue,
        which queues a new action against the current projection.
        """

        self._ensure_ready()
        if state not in HOSTED_ACTION_STATES or expected_state not in HOSTED_ACTION_STATES or state == "pending":
            raise ValueError(f"a hosted action cannot move to {state!r}")
        resolved_at = utc_now()
        with self._transaction() as conn:
            self._lock_for_write(conn)
            changed = conn.execute(
                "UPDATE review_hosted_actions SET state = ?, reason = ?, result_json = ?, resolved_at = ?"
                " WHERE review_id = ? AND id = ? AND state = ?",
                (state, reason, result_json, resolved_at, review_id, action_id, expected_state),
            ).rowcount
            row = conn.execute(
                "SELECT * FROM review_hosted_actions WHERE review_id = ? AND id = ?", (review_id, action_id)
            ).fetchone()
        if row is None:
            raise HostedAttachmentConflict("action_unknown", f"no queued action {action_id!r} on this Review")
        if not changed:
            raise HostedAttachmentConflict(
                "action_state_changed",
                f"the action is {row['state']}, not {expected_state}; reload the queue",
            )
        return _row_to_hosted_action(row)

    def prune_hosted_actions(self, review_id: str, *, keep_resolved: int, resolved_before: str) -> int:
        """Delete resolved (applied/discarded) actions past the count or age bound.

        Unresolved actions are never pruned: they are bounded at insert time
        and leave the queue only by replay, discard or detach.
        """

        if keep_resolved < 0:
            raise ValueError("keep_resolved must be non-negative")
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT id, resolved_at FROM review_hosted_actions"
                " WHERE review_id = ? AND state IN ('applied', 'discarded') ORDER BY sequence DESC",
                (review_id,),
            ).fetchall()
            doomed = [
                str(row["id"])
                for index, row in enumerate(rows)
                if index >= keep_resolved or str(row["resolved_at"]) < resolved_before
            ]
            for action_id in doomed:
                conn.execute("DELETE FROM review_hosted_actions WHERE id = ?", (action_id,))
        return len(doomed)

    def list_relationships_by_kind(self, kind: str) -> tuple[ReviewRelationship, ...]:
        """Every relationship of one kind across all Changes in this store."""
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM review_relationships WHERE kind = ? ORDER BY review_id, created_at, id",
                (kind,),
            ).fetchall()
        return tuple(_row_to_relationship(row) for row in rows)

    def update_relationship_if_unchanged(
        self,
        expected: ReviewRelationship,
        *,
        status: str,
        metadata_json: str,
    ) -> ReviewRelationship | None:
        """Compare-and-set one relationship; ``None`` when it changed since ``expected`` was read."""
        self._ensure_ready()
        with self._transaction() as conn:
            updated = conn.execute(
                """
                UPDATE review_relationships SET status = ?, metadata_json = ?, updated_at = ?
                 WHERE id = ? AND status = ? AND metadata_json = ? AND updated_at = ?
                """,
                (
                    status,
                    metadata_json,
                    utc_now(),
                    expected.id,
                    expected.status,
                    expected.metadata_json,
                    expected.updated_at,
                ),
            ).rowcount
            row = (
                conn.execute("SELECT * FROM review_relationships WHERE id = ?", (expected.id,)).fetchone()
                if updated == 1
                else None
            )
        return _row_to_relationship(row) if row is not None else None

    def find_session(
        self,
        *,
        repo_root: str,
        subject_type: str,
        source_ref: str,
        range_mode: str,
        never_bound: bool = False,
    ) -> ReviewSession | None:
        """Look a session up by its reopen identity.

        ``never_bound`` restricts the lookup to sessions no ReviewBinding has
        ever owned: pre-binding Reviews that are safe to adopt. A Review that
        was bound once belongs to its subject even after release, so another
        branch must never inherit it through this shared key.
        """
        self._ensure_ready()
        unbound = (
            " AND NOT EXISTS (SELECT 1 FROM review_bindings AS b WHERE b.review_id = review_sessions.id)"
            if never_bound
            else ""
        )
        with self._transaction() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM review_sessions
                 WHERE repo_root = ? AND subject_type = ? AND source_ref = ? AND range_mode = ?{unbound}
                 ORDER BY CASE status WHEN 'open' THEN 0 ELSE 1 END, updated_at DESC, id DESC
                 LIMIT 1
                """,
                (repo_root, subject_type, source_ref, range_mode),
            ).fetchone()
        return None if row is None else _row_to_session(row)

    def list_sessions(
        self, *, status: str = "open", limit: int | None = 50, after: tuple[str, str] | None = None
    ) -> tuple[ReviewSession, ...]:
        """Most-recently-updated sessions; ``status=""`` lists every status.

        ``limit=None`` is reserved for higher-level cursor pagination that must
        merge several repository stores without imposing a hidden per-store
        truncation first. ``after`` is a keyset cursor, the ``(updated_at, id)``
        of the last session of the previous page: pass it to read every page.
        """
        self._ensure_ready()
        sql = "SELECT * FROM review_sessions"
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if after is not None:
            clauses.append("(updated_at < ? OR (updated_at = ? AND id < ?))")
            params.extend((after[0], after[0], after[1]))
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
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

    def require_current_revision(self, review_id: str, expected_revision_id: str) -> None:
        """Fail unless *expected_revision_id* is still the Review head in this transaction."""

        if not expected_revision_id:
            raise ValueError("expected revision id must not be empty")
        self._ensure_ready()
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"current-revision:{review_id}")
            row = conn.execute(
                "SELECT current_revision_id FROM review_sessions WHERE id = ?",
                (review_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"no such review session {review_id!r}")
            if str(row["current_revision_id"] or "") != expected_revision_id:
                raise ValueError(
                    "the Review advanced before this human action committed; reload the current revision and try again"
                )

    def update_session_status_if_current_revision(
        self,
        review_id: str,
        expected_revision_id: str,
        status: str,
    ) -> tuple[ReviewSession, bool]:
        """Atomically change Review status only while one exact revision is current.

        Global lifecycle state such as ``finished`` must never be committed from
        a verdict prepared against an older revision. The comparison and update
        therefore share SQLite's write lock.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"session-status:{review_id}")
            row = conn.execute("SELECT * FROM review_sessions WHERE id = ?", (review_id,)).fetchone()
            if row is None:
                raise ValueError(f"no such review session {review_id!r}")
            current_revision_id = str(row["current_revision_id"] or "")
            if current_revision_id != expected_revision_id:
                raise ValueError(
                    "the Review advanced before this status change; reload the current revision and try again"
                )
            changed = str(row["status"] or "") != status
            if changed:
                conn.execute(
                    "UPDATE review_sessions SET status = ?, updated_at = ? WHERE id = ?",
                    (status, utc_now(), review_id),
                )
                row = conn.execute("SELECT * FROM review_sessions WHERE id = ?", (review_id,)).fetchone()
        if row is None:  # pragma: no cover - selected under the same transaction
            raise RuntimeError("review session disappeared during status update")
        return _row_to_session(row), changed

    @contextlib.contextmanager
    def atomic_review_transition(self) -> Iterator[sqlite3.Connection]:
        """Hold one SQLite write transaction across a Review head transition.

        Packet construction and artifact I/O happen before this scope. Once a
        candidate revision exists, baseline reads, mark reconciliation, anchor
        reconciliation and the current-revision pointer must commit together so
        two refresh processes cannot interleave half of two source generations.
        """

        self._ensure_ready()
        if self._connection is not None:
            conn = self._connection
            owns_transaction = not conn.in_transaction
            if owns_transaction:
                conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                if owns_transaction and conn.in_transaction:
                    conn.commit()
            except Exception:
                if owns_transaction and conn.in_transaction:
                    conn.rollback()
                raise
            return

        conn = self._connect()
        old_conn: sqlite3.Connection | None = self._connection
        old_deadline = self._batch_deadline
        self._connection = conn
        self._batch_deadline = None
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            if conn.in_transaction:
                conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            self._connection = old_conn
            self._batch_deadline = old_deadline
            conn.close()

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
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            # Contention past busy_timeout surfaces: proceeding would run the
            # dependent read unserialized. Only an already-open transaction is benign.
            if "within a transaction" not in str(exc):
                raise

    def _discard_revision_artifacts(self, review_id: str, revision_id: str) -> None:
        """Delete the artifact directory of a revision that never got a row.

        ``record_revision`` writes ``packet.json.gz`` and ``blobs.json.gz``
        under a freshly minted revision id *before* it calls
        :meth:`add_revision`. When that id loses the exact-Snapshot race the row is never
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

    def _lock_for_claim(self, conn: sqlite3.Connection) -> None:
        """Hold this database's writer lock across an artifact claim and its insert.

        SQLite: the hosted claim commits in a separate artifact index, so the row's
        own insert is what the orphan GC must not decide around. The GC takes this
        writer lock before the index lock (the order a claiming writer uses), so it
        never re-checks authority while a claimed row is still uncommitted.
        PostgreSQL overrides this: there the claim's row locks hold until commit.
        """

        if callable(getattr(self._artifact_backend, "claim", None)):
            self._lock_for_write(conn, key="artifact-claim")

    def _claim_artifacts(self, keys: Iterable[str], *, row: str) -> None:
        """Refresh every artifact mapping a new Review row references, or refuse the row.

        Hosted orphan-artifact GC removes a mapping that is older than its grace
        and unreferenced by any Review row, and its delete re-checks the mapping's
        age. Called inside the row's own transaction, just before its insert: a
        writer that stalled past the grace either makes every mapping young again
        (GC's guarded delete misses them) or finds one already reclaimed and
        commits nothing. A row never lands referencing a reclaimed artifact.
        PostgreSQL: the claim is part of the insert's transaction and its row locks
        hold until commit. SQLite: the claim commits in the separate artifact index;
        the writer holds its ``review.db`` writer lock (:meth:`_lock_for_claim`)
        across claim and insert, and the GC deletes only under that same lock.
        Local stores have no such GC and no ``claim``.
        """

        claim = getattr(self._artifact_backend, "claim", None)
        expected = frozenset(key for key in keys if key)
        if not callable(claim) or not expected:
            return
        missing = sorted(expected - frozenset(claim(tuple(sorted(expected)))))
        if missing:
            raise ReviewArtifactReclaimed(
                f"{row} artifacts {missing!r} were reclaimed before the {row} row committed; write them again"
            )

    def _claim_revision_artifacts(self, revision: ReviewRevision, written: Sequence[str] = ()) -> None:
        """Claim the packet plus every other artifact the writer wrote for this revision.

        *written* is the writer's own list (blobs, media, submodules), handed over
        with the commit by ``record_revision``: exact, unbounded and shared by no
        other call, so nothing is evicted and nothing outlives the commit.
        """

        if not revision.packet_path:
            return
        self._claim_artifacts((revision.packet_path, *written), row="Snapshot")

    def add_revision(
        self,
        revision: ReviewRevision,
        units: Sequence[ReviewUnit],
        *,
        artifact_keys: Sequence[str] = (),
    ) -> ReviewRevision:
        """Write a revision and all of its units in **one** transaction.

        *artifact_keys* are the non-packet artifacts the caller wrote for this
        revision before calling; the insert claims them with the packet (see
        :meth:`_claim_artifacts`).

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

        The exact Snapshot lookup is repeated *after* taking the write lock. A
        rival may have created the same semantic tree from the same source
        generation while this caller was serializing artifacts before entering
        this method; the lock + recheck turns that race into the same warm-hit
        result as :meth:`find_revision_by_snapshot_identity` without relying on
        backend-specific constraint-error transaction semantics. A semantically
        identical tree from a different source generation is intentionally a new
        Snapshot so rebase/base movement remains visible in history.
        """
        self._ensure_ready()
        revision_id = revision.id or new_revision_id()
        created_at = revision.created_at or utc_now()
        winner: ReviewRevision | None = None
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"revision:{revision.review_id}")
            existing = conn.execute(
                "SELECT * FROM review_revisions "
                "WHERE review_id = ? AND tree_fingerprint = ? AND source_fingerprint = ?",
                (revision.review_id, revision.tree_fingerprint, revision.source_fingerprint),
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
                    source_view_id=revision.source_view_id,
                    source_view_revision=revision.source_view_revision,
                    capture_kind=revision.capture_kind,
                    commit_sha=revision.commit_sha,
                    branch_ref=revision.branch_ref,
                    worktree_key=revision.worktree_key,
                    title=revision.title,
                )
            if winner is None:
                self._claim_revision_artifacts(record, artifact_keys)
                conn.execute(
                    """
                    INSERT INTO review_revisions (
                        id, review_id, revision_number, range_mode, base_sha, head_sha,
                        merge_base_sha, dirty, tree_fingerprint, packet_schema_version,
                        packet_path, packet_sha256, packet_bytes, degraded_json,
                        provenance_host, provenance_model, provenance_session_id,
                        provenance_certainty, created_at, source_fingerprint,
                        source_view_id, source_view_revision, capture_kind, commit_sha,
                        branch_ref, worktree_key, title
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        record.source_view_id,
                        record.source_view_revision,
                        record.capture_kind,
                        record.commit_sha,
                        record.branch_ref,
                        record.worktree_key,
                        record.title,
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
                        summary=f"Snapshot {max(0, record.revision_number - 1)} recorded",
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
                # Freeze Development with the Snapshot: the watermark is read
                # under this revision's write lock, so no event can land
                # between "Snapshot exists" and "what it includes". Portable
                # upsert: the hosted PostgreSQL store runs the same statement.
                conn.execute(
                    """
                    INSERT INTO development_snapshots (
                        revision_id, change_id, watermark, projection_json, created_at
                    )
                    SELECT ?, ?, COALESCE(MAX(sequence), 0), '', ?
                      FROM development_events WHERE change_id = ?
                    ON CONFLICT(revision_id) DO NOTHING
                    """,
                    (record.id, record.review_id, record.created_at, record.review_id),
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

    def set_revision_title(self, review_id: str, revision_id: str, title: str) -> ReviewRevision | None:
        """Set derived Snapshot presentation text without changing Snapshot identity."""

        self._ensure_ready()
        clean = " ".join(title.split()).strip()[:160]
        with self._transaction() as conn:
            conn.execute(
                "UPDATE review_revisions SET title=? WHERE id=? AND review_id=?",
                (clean, revision_id, review_id),
            )
            row = conn.execute(
                "SELECT * FROM review_revisions WHERE id=? AND review_id=?",
                (revision_id, review_id),
            ).fetchone()
        return None if row is None else _row_to_revision(row)

    def resolve_revision(self, reference: str) -> ReviewRevision | None:
        """Resolve a full id, ``rr/<id>``, or collision-safe short token."""

        exact = self.get_revision(reference)
        if exact is not None:
            return exact
        token = reference[len(_REVISION_REF_PREFIX) :] if reference.startswith(_REVISION_REF_PREFIX) else reference
        if not token:
            return None
        if exact := self.get_revision(token):
            return exact
        if len(token) < 8 or any(char not in "0123456789abcdef" for char in token.lower()):
            return None
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute("SELECT * FROM review_revisions").fetchall()
        candidates = [_row_to_revision(row) for row in rows]
        matches = [
            row
            for row in candidates
            if _short_reference_token(row.id, revision=True, width=len(token)) == token.lower()
        ]
        if len(matches) > 1:
            raise AmbiguousReviewReference(f"ambiguous Review revision reference {reference!r}")
        return matches[0] if matches else None

    def revision_references(self) -> ShortReferenceIndex:
        """Every Snapshot's short-reference domain: read once to name many Snapshots."""

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute("SELECT id FROM review_revisions").fetchall()
        return ShortReferenceIndex((str(row["id"]) for row in rows), revision=True)

    def short_revision_reference(self, revision_id: str, *, min_chars: int = 8) -> str:
        return self.revision_references().shortest(revision_id, min_chars=min_chars)

    # ----- derived results ------------------------------------------------- #

    def derived_results(
        self, kind: str, revision_ids: Iterable[str]
    ) -> dict[tuple[str, str], tuple[str, dict[str, Any]]]:
        """Stored ``kind`` results for these Snapshots, in one read per 500 ids.

        Maps ``(revision_id, scope_key)`` to ``(stamp, payload)``. A caller
        serves a row only when its stamp equals the stamp of the inputs it
        holds now; anything else is recomputed and stored again.
        """

        self._ensure_ready()
        ids = list(dict.fromkeys(revision_ids))
        out: dict[tuple[str, str], tuple[str, dict[str, Any]]] = {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            with self._transaction() as conn:
                rows = conn.execute(
                    "SELECT revision_id, scope_key, stamp, payload_json FROM review_derived"
                    f" WHERE kind = ? AND revision_id IN ({', '.join('?' for _ in chunk)})",
                    (kind, *chunk),
                ).fetchall()
            for row in rows:
                try:
                    payload = json.loads(str(row["payload_json"]))
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    out[(str(row["revision_id"]), str(row["scope_key"]))] = (str(row["stamp"]), payload)
        return out

    def put_derived_results(self, results: Sequence[DerivedResult]) -> None:
        """Store derived results in one write; a row's newer stamp replaces its older one."""

        if not results:
            return
        self._ensure_ready()
        now = utc_now()
        with self._transaction() as conn:
            conn.executemany(
                """
                INSERT INTO review_derived (
                    revision_id, kind, scope_key, review_id, stamp, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(revision_id, kind, scope_key) DO UPDATE SET
                    review_id = excluded.review_id,
                    stamp = excluded.stamp,
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                [
                    (
                        item.revision_id,
                        item.kind,
                        item.scope_key,
                        item.review_id,
                        item.stamp,
                        json.dumps(item.payload, sort_keys=True),
                        now,
                    )
                    for item in results
                ],
            )

    def derived_results_by_kind(
        self, kind: str, *, stamp_prefix: str = "", limit: int = 500
    ) -> tuple[DerivedResult, ...]:
        """Stored ``kind`` rows across every Snapshot whose stamp starts with ``stamp_prefix`` (oldest first).

        For retention closers that must find rows by state rather than by
        Snapshot. ``limit`` is clamped to 1..5000.
        """

        self._ensure_ready()
        sql = "SELECT revision_id, scope_key, review_id, stamp, payload_json FROM review_derived WHERE kind = ?"
        params: list[Any] = [kind]
        if stamp_prefix:
            sql += " AND substr(stamp, 1, ?) = ?"
            params.extend([len(stamp_prefix), stamp_prefix])
        sql += " ORDER BY updated_at ASC, revision_id ASC, scope_key ASC LIMIT ?"
        params.append(max(1, min(int(limit), 5000)))
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        out: list[DerivedResult] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except ValueError:
                continue
            if isinstance(payload, dict):
                out.append(
                    DerivedResult(
                        review_id=str(row["review_id"]),
                        revision_id=str(row["revision_id"]),
                        kind=kind,
                        scope_key=str(row["scope_key"]),
                        stamp=str(row["stamp"]),
                        payload=payload,
                    )
                )
        return tuple(out)

    def insert_derived_result(self, result: DerivedResult) -> tuple[bool, str, dict[str, Any]]:
        """Insert one row only if its key is absent: ``(created, stored stamp, stored payload)``.

        The ``(revision_id, kind, scope_key)`` key admits exactly one writer, so
        concurrent processes racing the same key agree on which row won.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO review_derived (
                    revision_id, kind, scope_key, review_id, stamp, payload_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(revision_id, kind, scope_key) DO NOTHING
                """,
                (
                    result.revision_id,
                    result.kind,
                    result.scope_key,
                    result.review_id,
                    result.stamp,
                    json.dumps(result.payload, sort_keys=True),
                    utc_now(),
                ),
            )
            created = cursor.rowcount == 1
            row = conn.execute(
                "SELECT stamp, payload_json FROM review_derived WHERE revision_id = ? AND kind = ? AND scope_key = ?",
                (result.revision_id, result.kind, result.scope_key),
            ).fetchone()
        if row is None:
            raise LookupError(f"derived row {result.kind}/{result.scope_key} vanished after insert")
        payload = json.loads(str(row["payload_json"]))
        return created, str(row["stamp"]), payload if isinstance(payload, dict) else {}

    def compare_and_set_derived_result(self, result: DerivedResult, *, expected_stamp: str) -> bool:
        """Replace one existing row only while its stamp is still *expected_stamp*.

        One conditional ``UPDATE``: a writer holding a stale read loses instead
        of overwriting a concurrent writer, in this or any other process.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE review_derived
                   SET stamp = ?, payload_json = ?, updated_at = ?
                 WHERE revision_id = ? AND kind = ? AND scope_key = ? AND stamp = ?
                """,
                (
                    result.stamp,
                    json.dumps(result.payload, sort_keys=True),
                    utc_now(),
                    result.revision_id,
                    result.kind,
                    result.scope_key,
                    expected_stamp,
                ),
            )
            return cursor.rowcount == 1

    def accumulate_derived_result(
        self,
        result: DerivedResult,
        *,
        increments: Mapping[str, int | float],
        max_attempts: int = 256,
    ) -> dict[str, Any]:
        """Add *increments* to one row's numeric payload fields, atomically in any process or replica.

        ``result.payload`` holds the row's identity fields, stored as given;
        each ``increments`` key is added to its stored value (absent or a
        foreign stamp: 0). The first writer inserts (``ON CONFLICT DO
        NOTHING``); every later one is one conditional ``UPDATE`` matching only
        the exact stored payload text it read. A writer that lost a race
        changes nothing and retries from the new value, so a concurrent
        increment is never overwritten (lost) and never applied twice. This is
        the store-side authority for additive ledgers (compute spent, compute
        saved); it needs no per-process lock. Returns the stored payload.

        lc-debt: optimistic retry, bounded by *max_attempts*; a key hammered by
        more concurrent writers than that raises instead of dropping an
        increment. Upgrade: a dialect-specific additive JSON ``UPSERT``.
        """

        if not increments:
            raise ValueError("accumulate_derived_result needs at least one increment")
        for name, value in increments.items():
            if name in result.payload or isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"increment {name!r} must be a number outside the identity fields")
        self._ensure_ready()
        key = (result.revision_id, result.kind, result.scope_key)
        for _attempt in range(max(1, int(max_attempts))):
            with self._transaction() as conn:
                row = conn.execute(
                    "SELECT stamp, payload_json FROM review_derived WHERE revision_id = ? AND kind = ? AND scope_key = ?",
                    key,
                ).fetchone()
                if row is None:
                    payload = {**result.payload, **increments}
                    cursor = conn.execute(
                        """
                        INSERT INTO review_derived (
                            revision_id, kind, scope_key, review_id, stamp, payload_json, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(revision_id, kind, scope_key) DO NOTHING
                        """,
                        (*key, result.review_id, result.stamp, json.dumps(payload, sort_keys=True), utc_now()),
                    )
                    if cursor.rowcount == 1:
                        return payload
                else:
                    expected_stamp, expected_text = str(row["stamp"]), str(row["payload_json"])
                    try:
                        decoded = json.loads(expected_text)
                    except ValueError:
                        decoded = None
                    previous = decoded if isinstance(decoded, dict) and expected_stamp == result.stamp else {}
                    payload = dict(result.payload)
                    for name, value in increments.items():
                        stored = previous.get(name)
                        base = stored if isinstance(stored, (int, float)) and not isinstance(stored, bool) else 0
                        payload[name] = base + value
                    cursor = conn.execute(
                        """
                        UPDATE review_derived
                           SET review_id = ?, stamp = ?, payload_json = ?, updated_at = ?
                         WHERE revision_id = ? AND kind = ? AND scope_key = ? AND stamp = ? AND payload_json = ?
                        """,
                        (
                            result.review_id,
                            result.stamp,
                            json.dumps(payload, sort_keys=True),
                            utc_now(),
                            *key,
                            expected_stamp,
                            expected_text,
                        ),
                    )
                    if cursor.rowcount == 1:
                        return payload
        raise RuntimeError(
            f"derived row {result.kind}/{result.scope_key} kept changing under {max_attempts} concurrent writers"
        )

    def derived_rows_by_scope(self, kind: str, scope_key: str) -> list[DerivedResult]:
        """Every ``kind`` row with this scope key, on any Snapshot of any Review in this store.

        For results shared across Reviews (a base commit's shared check results,
        its test-impact map). Hosted stores are confined to the tenant
        repository by row-level security, so a lookup never crosses tenants.
        lc-debt: filtered through the ``(kind, revision_id)`` index, so the cost is
        the number of ``kind`` rows; callers keep that bounded with
        :meth:`evict_derived_results`. Upgrade: a ``(kind, scope_key)`` index.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT revision_id, review_id, stamp, payload_json FROM review_derived"
                " WHERE kind = ? AND scope_key = ? ORDER BY updated_at DESC, revision_id",
                (kind, scope_key),
            ).fetchall()
        out: list[DerivedResult] = []
        for row in rows:
            try:
                payload = json.loads(str(row["payload_json"]))
            except ValueError:
                continue
            if isinstance(payload, dict):
                out.append(
                    DerivedResult(
                        review_id=str(row["review_id"]),
                        revision_id=str(row["revision_id"]),
                        kind=kind,
                        scope_key=scope_key,
                        stamp=str(row["stamp"]),
                        payload=payload,
                    )
                )
        return out

    def touch_derived_result(self, revision_id: str, kind: str, scope_key: str) -> None:
        """Mark one row as just used (least-recently-used retention reads ``updated_at``)."""

        self._ensure_ready()
        with self._transaction() as conn:
            conn.execute(
                "UPDATE review_derived SET updated_at = ? WHERE revision_id = ? AND kind = ? AND scope_key = ?",
                (utc_now(), revision_id, kind, scope_key),
            )

    def evict_derived_results(self, kind: str, *, keep: int) -> int:
        """Delete all but the *keep* most recently used ``kind`` rows; returns how many went.

        Least-recently-used retention for shared rows, on top of the Snapshot
        deletion cascade. Hosted stores evict within the tenant repository only.
        """

        if keep < 0:
            raise ValueError("keep must be non-negative")
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT revision_id, scope_key FROM review_derived WHERE kind = ?"
                " ORDER BY updated_at DESC, revision_id, scope_key",
                (kind,),
            ).fetchall()
            stale = [(str(row["revision_id"]), str(row["scope_key"])) for row in rows[keep:]]
            for revision_id, scope_key in stale:
                conn.execute(
                    "DELETE FROM review_derived WHERE revision_id = ? AND kind = ? AND scope_key = ?",
                    (revision_id, kind, scope_key),
                )
        return len(stale)

    def revision_reference(self, revision_id: str) -> str:
        """Return the short canonical public ref for one durable Snapshot id."""

        return f"{_REVISION_REF_PREFIX}{self.short_revision_reference(revision_id)}"

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

    def set_revision_source_view(
        self,
        revision_id: str,
        *,
        view_id: str,
        view_revision: int,
    ) -> ReviewRevision | None:
        """Bind current-index context to the native View that supplied a Snapshot."""

        self._ensure_ready()
        with self._transaction() as conn:
            conn.execute(
                "UPDATE review_revisions SET source_view_id = ?, source_view_revision = ? WHERE id = ?",
                (view_id, max(0, int(view_revision)), revision_id),
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

        common_identity_changed = any(
            key not in projected or identity(stored[key]) != identity(projected[key]) for key in stored
        )
        added_keys = projected.keys() - stored.keys()
        # Compatibility repair for Snapshots created by the pre-symbol-fast-path:
        # those rows persisted file/hunk identity only, then a later normal Review
        # discovered stable symbol units and was rejected.  Adding only symbol
        # units is monotonic and preserves every identity the human could already
        # have reviewed; any removal, moved target, changed fingerprint, or other
        # new unit kind remains a hard refusal.
        symbol_only_upgrade = bool(
            added_keys
            and not common_identity_changed
            and not (stored.keys() - projected.keys())
            and all(projected[key].kind == "symbol" for key in added_keys)
        )
        if common_identity_changed or stored.keys() - projected.keys() or (added_keys and not symbol_only_upgrade):
            raise ValueError("review enrichment changed target identity; refusing to rewrite a visible revision")

        # Stable slots: the ranks this enrichment replaces are kept beside it, so a
        # reviewer who had already started on this Snapshot keeps the order they
        # were working through (``review_priority.pinned_ranks``).
        superseded = _superseded_ranks(stored, projected)
        if superseded:
            self._append_ranking_generation(current.review_id, current.id, superseded)
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
                if unit.unit_key in added_keys:
                    conn.execute(
                        """
                        INSERT INTO review_units (
                            revision_id, unit_key, kind, path, symbol, ordinal,
                            start_line, end_line, content_fingerprint, fingerprint_method,
                            attention_rank, attention_group, reasons_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            revision_id,
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
                else:
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
            self._lock_for_write(conn, key=f"current-revision:{review_id}")
            row = conn.execute(
                "SELECT current_revision_id, status FROM review_sessions WHERE id = ?",
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
            unreconciled = conn.execute(
                """
                SELECT a.id
                  FROM annotations AS a
                 WHERE a.review_id = ?
                   AND a.state IN ('open', 'orphaned')
                   AND NOT EXISTS (
                       SELECT 1
                         FROM annotation_anchor_events AS e
                        WHERE e.annotation_id = a.id
                          AND e.revision_id = ?
                          AND NOT EXISTS (
                              SELECT 1 FROM annotation_anchor_events AS later
                               WHERE later.annotation_id = e.annotation_id
                                 AND (later.created_at > e.created_at
                                      OR (later.created_at = e.created_at AND later.id > e.id))
                          )
                   )
                 LIMIT 1
                """,
                (review_id, revision_id),
            ).fetchone()
            if unreconciled is not None:
                raise ValueError("cannot publish Review revision while an open annotation is not reconciled to it")
            next_status = "open" if str(row["status"] or "") == "finished" else str(row["status"] or "")
            conn.execute(
                "UPDATE review_sessions SET current_revision_id = ?, status = ?, updated_at = ? WHERE id = ?",
                (revision_id, next_status, utc_now(), review_id),
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
        """Return the newest Snapshot with this semantic tree identity."""

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM review_revisions WHERE review_id = ? AND tree_fingerprint = ? "
                "ORDER BY revision_number DESC LIMIT 1",
                (review_id, tree_fingerprint),
            ).fetchone()
        return None if row is None else _row_to_revision(row)

    def find_revision_by_snapshot_identity(
        self,
        review_id: str,
        tree_fingerprint: str,
        source_fingerprint: str,
    ) -> ReviewRevision | None:
        """Exact Snapshot idempotency: semantic delta plus source generation."""

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM review_revisions "
                "WHERE review_id = ? AND tree_fingerprint = ? AND source_fingerprint = ? "
                "ORDER BY revision_number DESC LIMIT 1",
                (review_id, tree_fingerprint, source_fingerprint),
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

    def record_commit(self, checkpoint: ReviewCommit) -> ReviewCommit:
        """Record one immutable Git checkpoint, assigning a dense Review-local number."""

        self._ensure_ready()
        commit_sha = checkpoint.commit_sha.strip()
        if not commit_sha or not checkpoint.revision_id:
            raise ValueError("review commit requires commit_sha and revision_id")
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"review-commit:{checkpoint.review_id}")
            revision = conn.execute(
                "SELECT review_id FROM review_revisions WHERE id = ?",
                (checkpoint.revision_id,),
            ).fetchone()
            if revision is None or str(revision["review_id"]) != checkpoint.review_id:
                raise ValueError("review commit revision must belong to the same Review")
            existing = conn.execute(
                "SELECT * FROM review_commits WHERE review_id = ? AND commit_sha = ?",
                (checkpoint.review_id, commit_sha),
            ).fetchone()
            if existing is not None:
                record = _row_to_commit(existing)
                if record.revision_id != checkpoint.revision_id:
                    raise ValueError("Git commit is already attached to a different Review revision")
                return record
            number = int(checkpoint.commit_number)
            if number <= 0:
                row = conn.execute(
                    "SELECT COALESCE(MAX(commit_number), 0) AS n FROM review_commits WHERE review_id = ?",
                    (checkpoint.review_id,),
                ).fetchone()
                number = int(row["n"]) + 1 if row is not None else 1
            record = ReviewCommit(
                review_id=checkpoint.review_id,
                commit_number=number,
                commit_sha=commit_sha,
                revision_id=checkpoint.revision_id,
                parent_sha=checkpoint.parent_sha.strip(),
                state=checkpoint.state,
                created_at=checkpoint.created_at or utc_now(),
            )
            conn.execute(
                """
                INSERT INTO review_commits (
                    review_id, commit_number, commit_sha, revision_id, parent_sha, state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.review_id,
                    record.commit_number,
                    record.commit_sha,
                    record.revision_id,
                    record.parent_sha,
                    record.state,
                    record.created_at,
                ),
            )
        return record

    def list_commits(self, review_id: str, *, include_superseded: bool = True) -> tuple[ReviewCommit, ...]:
        self._ensure_ready()
        sql = "SELECT * FROM review_commits WHERE review_id = ?"
        if not include_superseded:
            sql += " AND state = 'active'"
        sql += " ORDER BY commit_number ASC"
        with self._transaction() as conn:
            rows = conn.execute(sql, (review_id,)).fetchall()
        return tuple(_row_to_commit(row) for row in rows)

    def find_commit(self, review_id: str, commit_sha: str) -> ReviewCommit | None:
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM review_commits WHERE review_id = ? AND commit_sha = ?",
                (review_id, commit_sha.strip()),
            ).fetchone()
        return None if row is None else _row_to_commit(row)

    def set_commit_state(self, review_id: str, commit_sha: str, state: str) -> ReviewCommit | None:
        if state not in {"active", "superseded"}:
            raise ValueError("review commit state must be active or superseded")
        self._ensure_ready()
        with self._transaction() as conn:
            changed = conn.execute(
                "UPDATE review_commits SET state = ? WHERE review_id = ? AND commit_sha = ?",
                (state, review_id, commit_sha.strip()),
            ).rowcount
            row = (
                conn.execute(
                    "SELECT * FROM review_commits WHERE review_id = ? AND commit_sha = ?",
                    (review_id, commit_sha.strip()),
                ).fetchone()
                if changed
                else None
            )
        return None if row is None else _row_to_commit(row)

    def list_revisions(self, review_id: str) -> tuple[ReviewRevision, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM review_revisions WHERE review_id = ? ORDER BY revision_number ASC",
                (review_id,),
            ).fetchall()
        return tuple(_row_to_revision(row) for row in rows)

    def publish_snapshot(self, review_id: str, revision_id: str) -> None:
        """Expose an explicit Review capture in History, once per revision."""
        self._ensure_ready()
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"snapshot-publication:{review_id}:{revision_id}")
            row = conn.execute(
                "SELECT id FROM review_activity_events WHERE review_id = ? AND revision_id = ? "
                "AND kind = 'snapshot.published' LIMIT 1",
                (review_id, revision_id),
            ).fetchone()
            if row is None:
                _insert_activity_event(
                    conn,
                    ReviewActivityEvent(
                        id="",
                        review_id=review_id,
                        revision_id=revision_id,
                        kind="snapshot.published",
                        actor_id="lemoncrow",
                        actor_type="unknown",
                        subject_type="snapshot",
                        subject_id=revision_id,
                        summary="Snapshot saved",
                    ),
                )

    def published_snapshot_ids(self, review_id: str) -> set[str]:
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT revision_id FROM review_activity_events " "WHERE review_id = ? AND kind = 'snapshot.published'",
                (review_id,),
            ).fetchall()
        return {str(row["revision_id"]) for row in rows}

    # Units held for reuse across calls, counted in units (~6k per large Snapshot).
    UNITS_MEMO_MAX_UNITS: ClassVar[int] = 60_000

    def list_units(self, revision_id: str) -> tuple[ReviewUnit, ...]:
        """A Snapshot's units, most important first.

        Units are written with their Snapshot and rewritten only by
        ``update_revision_projection``, which changes the Snapshot row's packet
        and provenance columns in the same transaction. So a one-row read of
        those columns decides whether the units read last time are still
        exact: every Reader request re-read (and re-decoded) thousands of unit
        rows of the same Snapshot, several times per request.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            head = conn.execute(
                f"SELECT {', '.join(_UNITS_STAMP_COLUMNS)} FROM review_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
            stamp = None if head is None else tuple(head[name] for name in _UNITS_STAMP_COLUMNS)
            if stamp is not None:
                with self._units_memo_lock:
                    cached = self._units_memo.get(revision_id)
                    if cached is not None and cached[0] == stamp:
                        self._units_memo.move_to_end(revision_id)
                        return cached[1]
            rows = conn.execute(
                """
                SELECT * FROM review_units WHERE revision_id = ?
                 ORDER BY attention_rank ASC, path ASC, ordinal ASC, unit_key ASC
                """,
                (revision_id,),
            ).fetchall()
        units = tuple(_row_to_unit(row) for row in rows)
        if stamp is not None:
            with self._units_memo_lock:
                self._units_memo[revision_id] = (stamp, units)
                self._units_memo.move_to_end(revision_id)
                held = sum(len(item[1]) for item in self._units_memo.values())
                while held > self.UNITS_MEMO_MAX_UNITS and len(self._units_memo) > 1:
                    _key, (_stamp, dropped) = self._units_memo.popitem(last=False)
                    held -= len(dropped)
        return units

    def add_revision_attributions(
        self,
        revision_id: str,
        attributions: Sequence[ReviewAttribution],
    ) -> tuple[ReviewAttribution, ...]:
        """Append exact authorship evidence without rewriting prior identity."""

        self._ensure_ready()
        now = utc_now()
        with self._transaction() as conn:
            for item in attributions:
                if item.revision_id != revision_id:
                    raise ValueError("attribution revision does not match target revision")
                if item.scope not in {"revision", "file"}:
                    raise ValueError(f"unsupported attribution scope {item.scope!r}")
                if item.role not in {"primary", "contributor"}:
                    raise ValueError(f"unsupported attribution role {item.role!r}")
                if not item.host or not item.session_id:
                    continue
                conn.execute(
                    """
                    INSERT INTO review_attributions (
                        revision_id, scope, scope_key, host, session_id, role,
                        model, certainty, method, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(revision_id, scope, scope_key, host, session_id) DO NOTHING
                    """,
                    (
                        revision_id,
                        item.scope,
                        item.scope_key,
                        item.host.strip().lower(),
                        item.session_id.strip(),
                        item.role,
                        item.model,
                        item.certainty,
                        item.method,
                        item.created_at or now,
                    ),
                )
            rows = conn.execute(
                """
                SELECT * FROM review_attributions WHERE revision_id = ?
                 ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END,
                          scope ASC, scope_key ASC, host ASC, session_id ASC
                """,
                (revision_id,),
            ).fetchall()
        return tuple(_row_to_attribution(row) for row in rows)

    def review_list_inputs(self, sessions: Sequence[ReviewSession]) -> dict[str, ReviewListInputs]:
        """Per Review: current Snapshot, its attributions, the reviewer's marks, the annotations.

        A list page needs these for every row. Reading them per row cost a
        handful of queries per Review; here each table is read once for all of
        them (per 500 Reviews), in the same order the single-Review reads use.
        """

        self._ensure_ready()
        by_id = {session.id: session for session in sessions}
        revisions: dict[str, ReviewRevision] = {}
        attributions: dict[str, list[ReviewAttribution]] = {}
        marks: dict[str, list[ReviewMark]] = {}
        annotations: dict[str, list[Annotation]] = {}
        ids = list(by_id)
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks_in = ", ".join("?" for _ in chunk)
            with self._transaction() as conn:
                for row in conn.execute(
                    f"""
                    SELECT r.* FROM review_sessions AS s
                      JOIN review_revisions AS r ON r.id = s.current_revision_id AND r.review_id = s.id
                     WHERE s.id IN ({marks_in})
                    """,
                    chunk,
                ).fetchall():
                    revision = _row_to_revision(row)
                    revisions[revision.review_id] = revision
                legacy = [review_id for review_id in chunk if review_id not in revisions]
                if legacy:
                    # Legacy fallback of ``latest_revision``: the highest number.
                    for row in conn.execute(
                        f"""
                        SELECT * FROM review_revisions
                         WHERE review_id IN ({", ".join("?" for _ in legacy)})
                         ORDER BY review_id, revision_number DESC
                        """,
                        legacy,
                    ).fetchall():
                        revision = _row_to_revision(row)
                        revisions.setdefault(revision.review_id, revision)
                current = [revisions[review_id].id for review_id in chunk if review_id in revisions]
                if current:
                    for row in conn.execute(
                        f"""
                        SELECT * FROM review_attributions WHERE revision_id IN ({", ".join("?" for _ in current)})
                         ORDER BY revision_id, CASE role WHEN 'primary' THEN 0 ELSE 1 END,
                                  scope ASC, scope_key ASC, host ASC, session_id ASC
                        """,
                        current,
                    ).fetchall():
                        attribution = _row_to_attribution(row)
                        attributions.setdefault(attribution.revision_id, []).append(attribution)
                for row in conn.execute(
                    f"SELECT * FROM review_marks WHERE review_id IN ({marks_in}) ORDER BY review_id, updated_at DESC",
                    chunk,
                ).fetchall():
                    mark = _row_to_mark(row)
                    if mark.reviewer_id == by_id[mark.review_id].reviewer_id:
                        marks.setdefault(mark.review_id, []).append(mark)
                for row in conn.execute(
                    f"SELECT * FROM annotations WHERE review_id IN ({marks_in}) ORDER BY review_id, created_at ASC",
                    chunk,
                ).fetchall():
                    annotation = _row_to_annotation(row)
                    annotations.setdefault(annotation.review_id, []).append(annotation)
        out: dict[str, ReviewListInputs] = {}
        for review_id in ids:
            current_revision = revisions.get(review_id)
            out[review_id] = ReviewListInputs(
                revision=current_revision,
                attributions=(tuple(attributions.get(current_revision.id, ())) if current_revision is not None else ()),
                marks=tuple(marks.get(review_id, ())),
                annotations=tuple(annotations.get(review_id, ())),
            )
        return out

    def active_bindings(self) -> dict[tuple[str, str, str], ReviewBinding]:
        """Every subject's active binding, as ``active_binding`` would pick it, in one read."""

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM review_bindings WHERE active = 1 ORDER BY updated_at DESC, id DESC"
            ).fetchall()
        out: dict[tuple[str, str, str], ReviewBinding] = {}
        for row in rows:
            binding = _row_to_binding(row)
            out.setdefault((binding.repository_identity, binding.subject_kind, binding.subject_key), binding)
        return out

    def list_revision_attributions(self, revision_id: str) -> tuple[ReviewAttribution, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT * FROM review_attributions WHERE revision_id = ?
                 ORDER BY CASE role WHEN 'primary' THEN 0 ELSE 1 END,
                          scope ASC, scope_key ASC, host ASC, session_id ASC
                """,
                (revision_id,),
            ).fetchall()
        return tuple(_row_to_attribution(row) for row in rows)

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

    def human_behavior_judgments(self, review_id: str) -> tuple[tuple[str, str], ...]:
        """``(judged revision id, note)`` of every human behavior judgment in force, any reviewer.

        Behavior judgments live in ``review_marks`` under the ``beh:`` key
        namespace. Run-artifact retention keeps the evidence those judgments
        were made against.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT reviewed_revision_id, note FROM review_marks "
                "WHERE review_id = ? AND substr(unit_key, 1, 4) = 'beh:' AND actor_type = 'human' "
                "AND state IN ('reviewed', 'needs_changes', 'changed_since_review')",
                (review_id,),
            ).fetchall()
        return tuple((str(row["reviewed_revision_id"]), str(row["note"] or "")) for row in rows)

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

    def first_judgment_at(self, review_id: str, revision_id: str, *, reviewer_id: str) -> str:
        """When *reviewer_id* first judged an item on this Snapshot (a human judgment), or ``""``."""

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT MIN(created_at) AS first_at FROM review_mark_events "
                "WHERE revision_id = ? AND review_id = ? AND reviewer_id = ? "
                "AND event_kind = 'judgment' AND actor_type = 'human'",
                (revision_id, review_id, reviewer_id),
            ).fetchone()
        return "" if row is None or row["first_at"] is None else str(row["first_at"])

    def list_review_mark_events(self, review_id: str) -> tuple[ReviewMarkEvent, ...]:
        """Every reviewer's append-only judgment transitions on one Change (signed review record)."""

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM review_mark_events WHERE review_id = ? ORDER BY created_at ASC, id ASC",
                (review_id,),
            ).fetchall()
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

    def list_activity_events(
        self,
        review_id: str,
        *,
        limit: int = 500,
        kinds: Sequence[str] = (),
        newest_first: bool = False,
    ) -> tuple[ReviewActivityEvent, ...]:
        """Oldest first; ``kinds`` narrows to those event kinds (applied before ``limit``), ``newest_first`` reads the latest *limit*."""

        self._ensure_ready()
        sql = "SELECT * FROM review_activity_events WHERE review_id = ?"
        params: list[Any] = [review_id]
        if kinds:
            sql += f" AND kind IN ({', '.join('?' for _ in kinds)})"
            params.extend(kinds)
        order = "DESC" if newest_first else "ASC"
        sql += f" ORDER BY created_at {order}, id {order} LIMIT ?"
        params.append(max(1, min(limit, 5000)))
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(_row_to_activity_event(row) for row in rows)

    def activity_events_for_reviews(
        self, review_ids: Sequence[str], *, kinds: Sequence[str]
    ) -> dict[str, tuple[ReviewActivityEvent, ...]]:
        """Events of ``kinds`` for many Reviews, oldest first, in one read per 500 Reviews (Inbox rows)."""

        self._ensure_ready()
        out: dict[str, list[ReviewActivityEvent]] = {}
        ids = list(dict.fromkeys(review_ids))
        if not kinds:
            return {}
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            with self._transaction() as conn:
                rows = conn.execute(
                    "SELECT * FROM review_activity_events"
                    f" WHERE review_id IN ({', '.join('?' for _ in chunk)})"
                    f" AND kind IN ({', '.join('?' for _ in kinds)})"
                    " ORDER BY review_id, created_at ASC, id ASC",
                    (*chunk, *kinds),
                ).fetchall()
            for row in rows:
                event = _row_to_activity_event(row)
                out.setdefault(event.review_id, []).append(event)
        return {review_id: tuple(events) for review_id, events in out.items()}

    #: Tables whose newest timestamp is a durable Review fact the readiness record must postdate.
    FACT_WATERMARK_SOURCES: ClassVar[tuple[tuple[str, str], ...]] = (
        ("review_marks", "updated_at"),
        ("annotations", "updated_at"),
        ("review_outcomes", "created_at"),
        ("review_evidence", "created_at"),
        ("review_relationships", "updated_at"),
    )

    def fact_watermarks(self, review_ids: Sequence[str]) -> dict[str, str]:
        """Newest durable-fact timestamp per Review, from aggregates only (no row content).

        A readiness record evaluated before this instant may no longer hold.
        One aggregate per source table per 500 Reviews.
        """

        from lemoncrow.pro.capabilities.review.freshness import parse_timestamp

        self._ensure_ready()
        newest: dict[str, tuple[datetime, str]] = {}
        ids = list(dict.fromkeys(review_ids))
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            with self._transaction() as conn:
                for table, column in self.FACT_WATERMARK_SOURCES:
                    rows = conn.execute(
                        f"SELECT review_id, MAX({column}) AS at FROM {table}"
                        f" WHERE review_id IN ({marks}) GROUP BY review_id",
                        chunk,
                    ).fetchall()
                    for row in rows:
                        text = str(row["at"] or "")
                        parsed = parse_timestamp(text)
                        if parsed is None:
                            continue
                        review_id = str(row["review_id"])
                        current = newest.get(review_id)
                        if current is None or parsed > current[0]:
                            newest[review_id] = (parsed, text)
        return {review_id: text for review_id, (_parsed, text) in newest.items()}

    def inbox_side_facts(self, review_ids: Sequence[str]) -> dict[str, dict[str, str]]:
        """Per Review: pinned feedback destination kind and Merge-when-ready landing status.

        Two reads per 500 Reviews (the local Inbox); a Review with neither is absent.
        """

        self._ensure_ready()
        out: dict[str, dict[str, str]] = {}
        ids = list(dict.fromkeys(review_ids))
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks = ", ".join("?" for _ in chunk)
            with self._transaction() as conn:
                for row in conn.execute(
                    f"SELECT review_id, kind FROM review_destinations WHERE review_id IN ({marks})", chunk
                ).fetchall():
                    out.setdefault(str(row["review_id"]), {})["destination"] = str(row["kind"] or "")
                for row in conn.execute(
                    "SELECT review_id, status FROM review_relationships"
                    f" WHERE review_id IN ({marks}) AND kind = 'landing' AND relation = 'merge_when_ready'"
                    " ORDER BY review_id, created_at, id",
                    chunk,
                ).fetchall():
                    out.setdefault(str(row["review_id"]), {})["landing"] = str(row["status"] or "")
        return out

    # ----- contextual Ask conversation ------------------------------------ #

    def create_ask_conversation(
        self,
        review_id: str,
        *,
        title: str = "",
        origin_json: str = "{}",
    ) -> ReviewAskConversationRecord:
        """Create one durable conversation. Existing unthreaded Ask rows remain legacy history."""

        self._ensure_ready()
        try:
            origin = json.loads(origin_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Ask conversation origin must be valid JSON: {exc}") from exc
        if not isinstance(origin, dict):
            raise ValueError("Ask conversation origin must encode an object")
        now = utc_now()
        stored = ReviewAskConversationRecord(
            id=new_ask_conversation_id(),
            review_id=review_id,
            title=" ".join(title.split())[:160],
            origin_json=json.dumps(origin, sort_keys=True, separators=(",", ":")),
            created_at=now,
            updated_at=now,
        )
        with self._transaction() as conn:
            exists = conn.execute("SELECT 1 FROM review_sessions WHERE id = ?", (review_id,)).fetchone()
            if exists is None:
                raise ValueError("Ask conversation Review does not exist")
            conn.execute(
                "INSERT INTO review_ask_conversations "
                "(id, review_id, title, origin_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (stored.id, stored.review_id, stored.title, stored.origin_json, stored.created_at, stored.updated_at),
            )
        return stored

    def get_ask_conversation(self, conversation_id: str) -> ReviewAskConversationRecord | None:
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM review_ask_conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
        return None if row is None else _row_to_ask_conversation(row)

    def list_ask_conversations(self, review_id: str, *, limit: int = 50) -> tuple[ReviewAskConversationRecord, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM review_ask_conversations WHERE review_id = ? "
                "ORDER BY updated_at DESC, id DESC LIMIT ?",
                (review_id, max(1, min(limit, 500))),
            ).fetchall()
        return tuple(_row_to_ask_conversation(row) for row in rows)

    def append_ask_message(self, message: ReviewAskMessage) -> ReviewAskMessage:
        """Append one immutable Review-scoped conversational turn."""

        self._ensure_ready()
        if message.role not in REVIEW_ASK_ROLES:
            raise ValueError(f"unknown Ask role {message.role!r}")
        if message.intent not in REVIEW_ASK_INTENTS:
            raise ValueError(f"unknown Ask intent {message.intent!r}")
        body = message.body.strip()
        if not body:
            raise ValueError("Ask message body must not be empty")
        if message.start_line < 0 or message.end_line < 0:
            raise ValueError("Ask source lines must be non-negative")
        if message.start_line and message.end_line and message.end_line < message.start_line:
            raise ValueError("Ask end_line must not precede start_line")
        try:
            context = json.loads(message.context_json or "{}")
            references = json.loads(message.references_json or "[]")
            action = json.loads(message.action_json or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError(f"Ask message metadata must be valid JSON: {exc}") from exc
        if not isinstance(context, dict):
            raise ValueError("Ask context_json must encode an object")
        if not isinstance(references, list):
            raise ValueError("Ask references_json must encode a list")
        if not isinstance(action, dict):
            raise ValueError("Ask action_json must encode an object")

        stored = replace(
            message,
            id=message.id or new_ask_message_id(),
            body=body[:65_536],
            context_json=json.dumps(context, sort_keys=True, separators=(",", ":")),
            references_json=json.dumps(references, sort_keys=True, separators=(",", ":")),
            action_json=json.dumps(action, sort_keys=True, separators=(",", ":")),
            created_at=message.created_at or utc_now(),
        )
        with self._transaction() as conn:
            revision = conn.execute(
                "SELECT review_id FROM review_revisions WHERE id = ?",
                (stored.revision_id,),
            ).fetchone()
            if revision is None or str(revision["review_id"]) != stored.review_id:
                raise ValueError("Ask message revision does not belong to this Review")
            if stored.conversation_id:
                conversation = conn.execute(
                    "SELECT review_id FROM review_ask_conversations WHERE id = ?",
                    (stored.conversation_id,),
                ).fetchone()
                if conversation is None or str(conversation["review_id"]) != stored.review_id:
                    raise ValueError("Ask message conversation does not belong to this Review")
            conn.execute(
                """
                INSERT INTO review_ask_messages (
                    id, review_id, revision_id, role, intent, body,
                    target_unit_key, path, symbol, side, start_line, end_line,
                    context_json, references_json, action_json, created_at, conversation_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.id,
                    stored.review_id,
                    stored.revision_id,
                    stored.role,
                    stored.intent,
                    stored.body,
                    stored.target_unit_key,
                    stored.path,
                    stored.symbol,
                    stored.side,
                    stored.start_line,
                    stored.end_line,
                    stored.context_json,
                    stored.references_json,
                    stored.action_json,
                    stored.created_at,
                    stored.conversation_id,
                ),
            )
            if stored.conversation_id:
                title = " ".join(stored.body.split())[:160] if stored.role == "human" else ""
                conn.execute(
                    "UPDATE review_ask_conversations SET updated_at = ?, "
                    "title = CASE WHEN title = '' AND ? != '' THEN ? ELSE title END WHERE id = ?",
                    (stored.created_at, title, title, stored.conversation_id),
                )
        return stored

    def get_ask_message(self, message_id: str) -> ReviewAskMessage | None:
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM review_ask_messages WHERE id = ?",
                (message_id,),
            ).fetchone()
        return None if row is None else _row_to_ask_message(row)

    def list_ask_messages(
        self,
        review_id: str,
        *,
        conversation_id: str | None = None,
        limit: int = 500,
    ) -> tuple[ReviewAskMessage, ...]:
        self._ensure_ready()
        sql = "SELECT * FROM review_ask_messages WHERE review_id = ?"
        params: list[Any] = [review_id]
        if conversation_id is not None:
            sql += " AND conversation_id = ?"
            params.append(conversation_id)
        sql += " ORDER BY created_at ASC, id ASC LIMIT ?"
        params.append(max(1, min(limit, 5000)))
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(_row_to_ask_message(row) for row in rows)

    def prepare_ask_handoff(
        self,
        message_id: str,
        *,
        operation_id: str,
        instruction_hash: str,
        target_ref: str,
    ) -> tuple[ReviewAskHandoff, bool]:
        """Persist a preview-bound handoff identity before external dispatch."""

        self._ensure_ready()
        operation = operation_id.strip()
        digest = instruction_hash.strip()
        if not operation or not digest:
            raise ValueError("Ask handoff requires operation_id and instruction_hash")
        now = utc_now()
        with self._transaction() as conn:
            existing = conn.execute(
                "SELECT * FROM review_ask_handoffs WHERE message_id = ? OR operation_id = ?",
                (message_id, operation),
            ).fetchone()
            if existing is not None:
                row = _row_to_ask_handoff(existing)
                if row.message_id != message_id or row.operation_id != operation or row.instruction_hash != digest:
                    raise ValueError("Ask handoff operation identity does not match the prepared instruction")
                return row, False
            message = conn.execute(
                "SELECT role, intent FROM review_ask_messages WHERE id = ?",
                (message_id,),
            ).fetchone()
            if message is None:
                raise ValueError("no such Ask message")
            if str(message["role"]) != "assistant" or str(message["intent"]) != "change_request":
                raise ValueError("only an assistant change-request preview can be handed to the author")
            conn.execute(
                """
                INSERT INTO review_ask_handoffs (
                    message_id, operation_id, instruction_hash, state,
                    target_ref, remote_ref, last_error, updated_at
                ) VALUES (?, ?, ?, 'prepared', ?, '', '', ?)
                """,
                (message_id, operation, digest, target_ref, now),
            )
            created = conn.execute(
                "SELECT * FROM review_ask_handoffs WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        assert created is not None
        return _row_to_ask_handoff(created), True

    def update_ask_handoff(
        self,
        message_id: str,
        *,
        state: ReviewAskHandoffState,
        remote_ref: str = "",
        last_error: str = "",
    ) -> ReviewAskHandoff | None:
        self._ensure_ready()
        if state not in REVIEW_ASK_HANDOFF_STATES:
            raise ValueError(f"unknown Ask handoff state {state!r}")
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE review_ask_handoffs
                   SET state = ?, remote_ref = ?, last_error = ?, updated_at = ?
                 WHERE message_id = ?
                """,
                (state, remote_ref, last_error, utc_now(), message_id),
            )
            row = conn.execute(
                "SELECT * FROM review_ask_handoffs WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return None if row is None else _row_to_ask_handoff(row)

    def claim_ask_handoff_dispatch(self, message_id: str) -> ReviewAskHandoff | None:
        """Atomically move one prepared/retryable handoff to ``dispatching``.

        Returns the claimed row, or ``None`` when another request already owns
        (or finished) delivery. The conditional UPDATE is the compare-and-set:
        two concurrent Reader workers can both pass preview validation, but only
        one of them may wake the authoring agent.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE review_ask_handoffs
                   SET state = 'dispatching', last_error = '', updated_at = ?
                 WHERE message_id = ? AND state IN ('prepared', 'blocked', 'failed')
                """,
                (utc_now(), message_id),
            )
            if int(cursor.rowcount or 0) != 1:
                return None
            row = conn.execute(
                "SELECT * FROM review_ask_handoffs WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return None if row is None else _row_to_ask_handoff(row)

    def get_ask_handoff(self, message_id: str) -> ReviewAskHandoff | None:
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT * FROM review_ask_handoffs WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        return None if row is None else _row_to_ask_handoff(row)

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
        expected_current_revision_id: str = "",
        activity_detail: Mapping[str, Any] | None = None,
    ) -> ReviewOutcome:
        """Append one reviewer verdict for one immutable revision.

        Repeating the reviewer's newest verdict unchanged on the same revision
        returns that record instead of appending a duplicate. ``activity_detail``
        joins the ``outcome.recorded`` event (e.g. the policy generation the
        verdict was given under).
        """

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
            if expected_current_revision_id:
                self._lock_for_write(conn, key=f"current-revision:{review_id}")
                current = conn.execute(
                    "SELECT current_revision_id FROM review_sessions WHERE id = ?",
                    (review_id,),
                ).fetchone()
                if current is None or str(current["current_revision_id"] or "") != expected_current_revision_id:
                    raise ValueError(
                        "the Review advanced before this outcome committed; reload the current revision and try again"
                    )
            revision = conn.execute(
                "SELECT review_id FROM review_revisions WHERE id = ?",
                (revision_id,),
            ).fetchone()
            if revision is None or str(revision["review_id"]) != review_id:
                raise ValueError("review outcome revision does not belong to this review")
            # A retried submission (a Finish retry after its first reply was
            # lost) must not append a duplicate verdict: the same reviewer's
            # newest verdict, identical on the same revision, is the answer.
            latest = conn.execute(
                """
                SELECT * FROM review_outcomes
                 WHERE review_id = ? AND reviewer_id = ?
                 ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (review_id, reviewer),
            ).fetchone()
            if latest is not None:
                existing = _row_to_outcome(latest)
                if existing.revision_id == revision_id and existing.outcome == outcome and existing.summary == note:
                    return existing
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
                        {**dict(activity_detail or {}), "outcome": outcome, "summary": note},
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

    def add_change_proposal(
        self,
        proposal: ReviewChangeProposal,
        *,
        expected_current_revision_id: str = "",
    ) -> ReviewChangeProposal:
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
            if expected_current_revision_id:
                self._lock_for_write(conn, key=f"current-revision:{record.review_id}")
                current = conn.execute(
                    "SELECT current_revision_id FROM review_sessions WHERE id = ?",
                    (record.review_id,),
                ).fetchone()
                if current is None or str(current["current_revision_id"] or "") != expected_current_revision_id:
                    raise ValueError(
                        "the Review advanced before this change proposal committed; reload the current revision and try again"
                    )
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
            agent_role=annotation.agent_role,
            agent_host=annotation.agent_host,
            agent_model=annotation.agent_model,
            parent_session_id=annotation.parent_session_id,
            source_message_id=annotation.source_message_id,
            title=annotation.title,
            evidence=tuple(annotation.evidence),
            rule_refs=tuple(annotation.rule_refs),
            confidence=annotation.confidence,
            finding_disposition=(
                annotation.finding_disposition or ("open" if annotation.source == "ai_review" else "")
            ),
            finding_disposition_reason=annotation.finding_disposition_reason,
            finding_disposition_revision_id=annotation.finding_disposition_revision_id,
            finding_disposition_reviewer_id=annotation.finding_disposition_reviewer_id,
            finding_disposition_evidence=tuple(annotation.finding_disposition_evidence),
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
                    source, source_id, title, evidence_json, rule_refs_json, confidence,
                    author_response, author_response_source_id, author_response_at,
                    resolved_revision_id, anchor_method, anchor_detail,
                    turn_owner_kind, turn_owner_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    json.dumps(list(record.rule_refs), ensure_ascii=False),
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
            conn.execute(
                "UPDATE annotations SET agent_role=?, agent_host=?, agent_model=?, "
                "parent_session_id=?, source_message_id=?, "
                "finding_disposition=?, finding_disposition_reason=?, "
                "finding_disposition_revision_id=?, finding_disposition_reviewer_id=?, "
                "finding_disposition_evidence_json=? WHERE id=?",
                (
                    record.agent_role,
                    record.agent_host,
                    record.agent_model,
                    record.parent_session_id,
                    record.source_message_id,
                    record.finding_disposition,
                    record.finding_disposition_reason,
                    record.finding_disposition_revision_id,
                    record.finding_disposition_reviewer_id,
                    _dump_tuple(record.finding_disposition_evidence),
                    record.id,
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
                    finding_disposition=record.finding_disposition,
                    finding_disposition_reason=record.finding_disposition_reason,
                    finding_disposition_revision_id=record.finding_disposition_revision_id,
                    finding_disposition_reviewer_id=record.finding_disposition_reviewer_id,
                    finding_disposition_evidence=record.finding_disposition_evidence,
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
                            finding_disposition=root.finding_disposition,
                            finding_disposition_reason=root.finding_disposition_reason,
                            finding_disposition_revision_id=root.finding_disposition_revision_id,
                            finding_disposition_reviewer_id=root.finding_disposition_reviewer_id,
                            finding_disposition_evidence=root.finding_disposition_evidence,
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
                    # Re-validate the whole semantic row so non-HTTP callers cannot
                    # create impossible finding dispositions or blur AI/human authority.
                    validate_annotation_semantics(_row_to_annotation(updated))
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
                        "finding_disposition",
                        "finding_disposition_reason",
                        "finding_disposition_revision_id",
                        "finding_disposition_reviewer_id",
                        "finding_disposition_evidence_json",
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
                        elif "finding_disposition" in fields:
                            resolved_kind = "finding_disposition"
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
                                finding_disposition=str(updated["finding_disposition"]),  # type: ignore[arg-type]
                                finding_disposition_reason=str(updated["finding_disposition_reason"]),  # type: ignore[arg-type]
                                finding_disposition_revision_id=str(updated["finding_disposition_revision_id"]),
                                finding_disposition_reviewer_id=str(updated["finding_disposition_reviewer_id"]),
                                finding_disposition_evidence=_json_tuple(updated["finding_disposition_evidence_json"]),
                            ),
                        )
                        activity_kind = cast(
                            ReviewActivityKind,
                            {
                                "edited": "annotation.updated",
                                "state_changed": "annotation.state_changed",
                                "author_response": "annotation.author_response",
                                "thread_reply": "annotation.thread_reply",
                                "finding_disposition": "annotation.finding_disposition",
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

    def add_discussion_comment(self, annotation: Annotation) -> None:
        """Retain the immutable agent message; Snapshot ownership is assigned separately."""
        self._ensure_ready()
        if annotation.comment_scope != "change" or annotation.created_by_actor != "agent":
            raise ValueError("Change discussion must be an agent comment")
        validate_annotation_semantics(annotation)
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO review_discussion_comments (id, review_id, payload_json, created_at, imported_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(id) DO NOTHING",
                (
                    annotation.id,
                    annotation.review_id,
                    json.dumps(asdict(annotation), ensure_ascii=False),
                    annotation.created_at,
                    utc_now(),
                ),
            )

    def attach_discussion_comment(self, review_id: str, comment_id: str, revision_id: str) -> None:
        """Assign a draft discussion to its first Snapshot without changing its message."""
        self._ensure_ready()
        with self._transaction() as conn:
            if (
                conn.execute(
                    "SELECT id FROM review_revisions WHERE id=? AND review_id=?", (revision_id, review_id)
                ).fetchone()
                is None
            ):
                raise ValueError("Snapshot does not belong to this Change")
            row = conn.execute(
                "SELECT payload_json FROM review_discussion_comments WHERE id=? AND review_id=?",
                (comment_id, review_id),
            ).fetchone()
            if row is None:
                return
            payload = json.loads(row["payload_json"])
            if payload.get("revision_id"):
                return
            payload["revision_id"] = revision_id
            payload["anchor_detail"] = "Snapshot discussion; no code anchor"
            conn.execute(
                "UPDATE review_discussion_comments SET payload_json=? WHERE id=? AND review_id=? AND payload_json=?",
                (json.dumps(payload, ensure_ascii=False), comment_id, review_id, row["payload_json"]),
            )

    def delete_discussion_comment(self, review_id: str, comment_id: str) -> None:
        """Remove a discussion row after the same message is projected onto code."""

        self._ensure_ready()
        with self._transaction() as conn:
            conn.execute(
                "DELETE FROM review_discussion_comments WHERE id=? AND review_id=?",
                (comment_id, review_id),
            )

    def list_discussion_comments(self, review_id: str, *, before: str = "") -> tuple[Annotation, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            sql = "SELECT payload_json FROM review_discussion_comments WHERE review_id=?"
            params: list[Any] = [review_id]
            if before:
                sql += " AND imported_at < ?"
                params.append(before)
            rows = conn.execute(sql + " ORDER BY created_at, id", params).fetchall()
        out: list[Annotation] = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["review_id"] = review_id
            payload["anchor"] = AnnotationAnchor(**payload["anchor"])
            payload["evidence"] = tuple(payload["evidence"])
            out.append(Annotation(**payload))
        return tuple(out)

    def list_discussion_comments_at_revision(self, review_id: str, revision_id: str) -> tuple[Annotation, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            target = conn.execute(
                "SELECT revision_number FROM review_revisions WHERE id=? AND review_id=?",
                (revision_id, review_id),
            ).fetchone()
            if target is None:
                return ()
            rows = conn.execute(
                "SELECT id FROM review_revisions WHERE review_id=? AND revision_number<=?",
                (review_id, target["revision_number"]),
            ).fetchall()
        included = {row["id"] for row in rows}
        return tuple(item for item in self.list_discussion_comments(review_id) if item.revision_id in included)

    def session_comment_cursor(self, review_id: str, host: str, session_id: str) -> dict[str, Any]:
        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT cursor_json FROM review_comment_imports WHERE review_id=? AND host=? AND session_id=?",
                (review_id, host, session_id),
            ).fetchone()
        return json.loads(row["cursor_json"]) if row else {}

    def set_session_comment_cursor(self, review_id: str, host: str, session_id: str, cursor: dict[str, Any]) -> None:
        self._ensure_ready()
        with self._transaction() as conn:
            conn.execute(
                "INSERT INTO review_comment_imports (review_id, host, session_id, cursor_json) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(review_id, host, session_id) DO UPDATE SET cursor_json=excluded.cursor_json",
                (review_id, host, session_id, json.dumps(cursor)),
            )

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

    def annotation_versions_for_review(self, review_id: str) -> dict[str, tuple[AnnotationVersion, ...]]:
        """Every comment's versions for one Review in one read, each in version order."""

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM annotation_versions WHERE review_id = ? ORDER BY annotation_id, version_number ASC",
                (review_id,),
            ).fetchall()
        grouped: dict[str, list[AnnotationVersion]] = {}
        for row in rows:
            version = _row_to_annotation_version(row)
            grouped.setdefault(version.annotation_id, []).append(version)
        return {annotation_id: tuple(items) for annotation_id, items in grouped.items()}

    def feedback_rows_for_reviews(
        self, review_ids: Sequence[str]
    ) -> tuple[dict[str, tuple[AnnotationVersion, ...]], dict[str, tuple[DeliveryRecord, ...]]]:
        """Every comment's versions and delivery attempts across *review_ids*, by annotation id.

        What a Reviews-list page needs to say where each Review's feedback
        stands: two reads per 500 Reviews instead of two per comment.
        """

        self._ensure_ready()
        versions: dict[str, list[AnnotationVersion]] = {}
        deliveries: dict[str, list[DeliveryRecord]] = {}
        ids = list(dict.fromkeys(review_ids))
        for start in range(0, len(ids), 500):
            chunk = ids[start : start + 500]
            marks_in = ", ".join("?" for _ in chunk)
            with self._transaction() as conn:
                for row in conn.execute(
                    f"""
                    SELECT * FROM annotation_versions WHERE review_id IN ({marks_in})
                     ORDER BY annotation_id, version_number ASC
                    """,
                    chunk,
                ).fetchall():
                    version = _row_to_annotation_version(row)
                    versions.setdefault(version.annotation_id, []).append(version)
                for row in conn.execute(
                    f"""
                    SELECT d.* FROM deliveries AS d
                      JOIN annotations AS a ON a.id = d.annotation_id
                     WHERE a.review_id IN ({marks_in})
                     ORDER BY d.annotation_id, d.created_at ASC
                    """,
                    chunk,
                ).fetchall():
                    delivery = _row_to_delivery(row)
                    deliveries.setdefault(delivery.annotation_id, []).append(delivery)
        return (
            {key: tuple(items) for key, items in versions.items()},
            {key: tuple(items) for key, items in deliveries.items()},
        )

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

    def annotations_needing_anchor_at(self, review_id: str, revision_id: str) -> tuple[Annotation, ...]:
        """Open annotations whose current geometry is not derived from *revision_id*."""

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                """
                SELECT a.*
                  FROM annotations AS a
                 WHERE a.review_id = ?
                   AND a.state IN ('open', 'orphaned')
                   AND NOT EXISTS (
                       SELECT 1
                         FROM annotation_anchor_events AS e
                        WHERE e.annotation_id = a.id
                          AND e.revision_id = ?
                          AND NOT EXISTS (
                              SELECT 1 FROM annotation_anchor_events AS later
                               WHERE later.annotation_id = e.annotation_id
                                 AND (later.created_at > e.created_at
                                      OR (later.created_at = e.created_at AND later.id > e.id))
                          )
                   )
                 ORDER BY a.created_at ASC, a.id ASC
                """,
                (review_id, revision_id),
            ).fetchall()
        return tuple(_row_to_annotation(row) for row in rows)

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
                if not same_identity:
                    raise ValueError("delivery operation identity does not match the existing operation")
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

    def recover_stale_delivery_attempts(
        self,
        revision_id: str,
        annotation_ids: Sequence[str],
        *,
        stale_after_s: float = 120.0,
    ) -> int:
        """Recover only stale *pre-dispatch* leases.

        A ``leased`` row proves the worker owned the operation but had not yet
        crossed the durable host-handoff boundary, so reclaiming it cannot
        duplicate an agent execution. ``dispatching`` is intentionally excluded:
        after that state is committed a crash may have happened either before or
        after the host accepted the payload, and no timeout can distinguish the
        two without a host receipt. Such rows remain ambiguous/non-resendable.
        """

        ids = tuple(dict.fromkeys(item for item in annotation_ids if item))
        if not revision_id or not ids:
            return 0
        self._ensure_ready()
        now_dt = datetime.now(UTC)
        cutoff = (now_dt - timedelta(seconds=max(1.0, stale_after_s))).isoformat()
        now = now_dt.isoformat()
        placeholders = ",".join("?" for _ in ids)
        with self._transaction() as conn:
            cursor = conn.execute(
                f"""
                UPDATE deliveries
                   SET state = 'failed',
                       last_error = 'delivery worker lease expired before host dispatch; safe to retry the same operation',
                       updated_at = ?
                 WHERE revision_id = ?
                   AND annotation_id IN ({placeholders})
                   -- Never auto-recover dispatching: it may already have crossed the host boundary.
                   AND state = 'leased'
                   AND remote_ref = ''
                   AND updated_at <= ?
                """,
                (now, revision_id, *ids, cutoff),
            )
        return int(cursor.rowcount or 0)

    def claim_delivery_target(
        self,
        operation_id: str,
        target_ref: str,
        *,
        lease_timeout_s: float = 30.0,
    ) -> tuple[tuple[DeliveryRecord, ...], bool]:
        """Lease one exact target before dispatch; stale pre-dispatch leases are safe to reclaim."""

        self._ensure_ready()
        now_dt = datetime.now(UTC)
        now = now_dt.isoformat()
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"delivery:{operation_id}:{target_ref}")
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE operation_id = ? AND target_ref = ? ORDER BY created_at ASC, id ASC",
                (operation_id, target_ref),
            ).fetchall()
            records = tuple(_row_to_delivery(row) for row in rows)
            if not records:
                return (), False

            def stale_lease(item: DeliveryRecord) -> bool:
                if item.state != "leased" or not item.updated_at:
                    return False
                try:
                    updated = datetime.fromisoformat(item.updated_at)
                    if updated.tzinfo is None:
                        updated = updated.replace(tzinfo=UTC)
                except ValueError:
                    return False
                return (now_dt - updated.astimezone(UTC)).total_seconds() >= max(0.0, lease_timeout_s)

            claimable = all(
                item.state == "prepared"
                or stale_lease(item)
                or (item.state in {"blocked", "failed"} and not item.remote_ref)
                for item in records
            )
            if not claimable:
                return records, False
            conn.execute(
                """
                UPDATE deliveries
                   SET state = 'leased', remote_ref = '', last_error = '', updated_at = ?
                 WHERE operation_id = ? AND target_ref = ?
                """,
                (now, operation_id, target_ref),
            )
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE operation_id = ? AND target_ref = ? ORDER BY created_at ASC, id ASC",
                (operation_id, target_ref),
            ).fetchall()
        return tuple(_row_to_delivery(row) for row in rows), True

    def begin_delivery_dispatch(
        self,
        operation_id: str,
        target_ref: str,
    ) -> tuple[tuple[DeliveryRecord, ...], bool]:
        """Cross the durable pre-dispatch boundary exactly once for one target group."""

        self._ensure_ready()
        now = utc_now()
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"delivery:{operation_id}:{target_ref}")
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE operation_id = ? AND target_ref = ? ORDER BY created_at ASC, id ASC",
                (operation_id, target_ref),
            ).fetchall()
            records = tuple(_row_to_delivery(row) for row in rows)
            if not records or not all(item.state == "leased" for item in records):
                return records, False
            conn.execute(
                """
                UPDATE deliveries
                   SET state = 'dispatching', updated_at = ?
                 WHERE operation_id = ? AND target_ref = ? AND state = 'leased'
                """,
                (now, operation_id, target_ref),
            )
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE operation_id = ? AND target_ref = ? ORDER BY created_at ASC, id ASC",
                (operation_id, target_ref),
            ).fetchall()
        return tuple(_row_to_delivery(row) for row in rows), True

    def update_delivery_target(
        self,
        operation_id: str,
        target_ref: str,
        *,
        state: str,
        remote_ref: str = "",
        last_error: str = "",
    ) -> tuple[DeliveryRecord, ...]:
        """Move one exact target group without changing another contributor's handoff."""

        self._ensure_ready()
        now = utc_now()
        with self._transaction() as conn:
            conn.execute(
                """
                UPDATE deliveries
                   SET state = ?, remote_ref = ?, last_error = ?, updated_at = ?
                 WHERE operation_id = ? AND target_ref = ?
                """,
                (state, remote_ref, last_error, now, operation_id, target_ref),
            )
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE operation_id = ? AND target_ref = ? ORDER BY created_at ASC, id ASC",
                (operation_id, target_ref),
            ).fetchall()
        return tuple(_row_to_delivery(row) for row in rows)

    def retarget_delivery_target(self, operation_id: str, target_ref: str, new_target_ref: str) -> int:
        """Name the exact session a launch created as the target of its in-flight rows.

        Only a group still ``dispatching`` moves: that is the launch this caller
        owns, so a settled or foreign group is never rewritten.
        """

        if not new_target_ref or new_target_ref == target_ref:
            return 0
        self._ensure_ready()
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"delivery:{operation_id}:{target_ref}")
            cursor = conn.execute(
                """
                UPDATE deliveries
                   SET target_ref = ?, updated_at = ?
                 WHERE operation_id = ? AND target_ref = ? AND state = 'dispatching'
                """,
                (new_target_ref, utc_now(), operation_id, target_ref),
            )
        return int(cursor.rowcount or 0)

    def acknowledge_delivery_remote_refs(self, remote_refs: Sequence[str]) -> int:
        """Promote exact host receipts to consumed without touching other targets."""

        refs = tuple(dict.fromkeys(item for item in remote_refs if item))
        if not refs:
            return 0
        self._ensure_ready()
        now = utc_now()
        placeholders = ",".join("?" for _ in refs)
        with self._transaction() as conn:
            cursor = conn.execute(
                f"""
                UPDATE deliveries
                   SET state = 'consumed', last_error = '', updated_at = ?
                 WHERE remote_ref IN ({placeholders})
                   AND state IN ('queued', 'working', 'sent')
                """,
                (now, *refs),
            )
        return int(cursor.rowcount or 0)

    def update_delivery_operation(
        self,
        operation_id: str,
        *,
        state: str,
        remote_ref: str = "",
        last_error: str = "",
        remote_refs: Mapping[str, str] | None = None,
    ) -> tuple[DeliveryRecord, ...]:
        """Move every row in an operation to the same externally-observed state.

        ``remote_refs`` (annotation id -> remote id) overrides ``remote_ref`` per
        row, for targets such as a GitHub review where each comment gets its own id.
        """
        self._ensure_ready()
        now = utc_now()
        with self._transaction() as conn:
            conn.execute(
                "UPDATE deliveries SET state = ?, remote_ref = ?, last_error = ?, updated_at = ? WHERE operation_id = ?",
                (state, remote_ref, last_error, now, operation_id),
            )
            for annotation_id, row_ref in (remote_refs or {}).items():
                conn.execute(
                    "UPDATE deliveries SET remote_ref = ? WHERE operation_id = ? AND annotation_id = ?",
                    (row_ref, operation_id, annotation_id),
                )
            rows = conn.execute(
                "SELECT * FROM deliveries WHERE operation_id = ? ORDER BY created_at ASC, id ASC",
                (operation_id,),
            ).fetchall()
        return tuple(_row_to_delivery(row) for row in rows)

    # ----- review evidence ------------------------------------------------ #

    def add_evidence(self, evidence: ReviewEvidence, *, expected_current_revision_id: str = "") -> ReviewEvidence:
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
            attestation_class=evidence.attestation_class,
            stable_key=evidence.stable_key,
            provider=evidence.provider,
            integration_id=evidence.integration_id,
            provider_revision=evidence.provider_revision,
            branch=evidence.branch,
            environment=evidence.environment,
            attestation_scope=evidence.attestation_scope,
            attested_by=evidence.attested_by,
            remote_identity=evidence.remote_identity,
            observed_at=evidence.observed_at,
            waived_requirement_key=evidence.waived_requirement_key,
            execution_intent=evidence.execution_intent,
            executor_class=evidence.executor_class,
            execution_verification=evidence.execution_verification,
        )
        with self._transaction() as conn:
            if expected_current_revision_id:
                self._lock_for_write(conn, key=f"current-revision:{record.review_id}")
                current = conn.execute(
                    "SELECT current_revision_id FROM review_sessions WHERE id = ?",
                    (record.review_id,),
                ).fetchone()
                if current is None or str(current["current_revision_id"] or "") != expected_current_revision_id:
                    raise ValueError(
                        "the Review advanced before this evidence committed; reload the current revision and try again"
                    )
            # The row claims its artifact in this transaction, so an orphan GC can
            # never reclaim bytes a just-committed evidence row references.
            self._lock_for_claim(conn)
            self._claim_artifacts((record.artifact_path,), row="evidence")
            conn.execute(
                """
                INSERT INTO review_evidence (
                    id, review_id, revision_id, kind, title, path,
                    artifact_path, url, content_hash, mime_type, bytes,
                    source, source_ref, verification_status, detail, created_at,
                    attestation_class, stable_key, provider, integration_id,
                    provider_revision, branch, environment, attestation_scope,
                    attested_by, remote_identity, observed_at, waived_requirement_key, execution_intent,
                    executor_class, execution_verification
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    record.attestation_class,
                    record.stable_key,
                    record.provider,
                    record.integration_id,
                    record.provider_revision,
                    record.branch,
                    record.environment,
                    record.attestation_scope,
                    record.attested_by,
                    record.remote_identity,
                    record.observed_at,
                    record.waived_requirement_key,
                    record.execution_intent,
                    record.executor_class,
                    record.execution_verification,
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

    def add_author_guidance(self, row: AuthorGuidanceRow) -> bool:
        """Store one imported guidance document; ``False`` if already stored.

        Rows are immutable: supersession and conflict are read-time
        interpretations (``guidance.build_guidance_view``), never rewrites.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO review_author_guidance (
                    review_id, id, revision_id, producer_host, producer_session_id,
                    producer_model, attribution, repo_root, schema_version, source_identity_json,
                    payload_json, created_at, imported_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (review_id, id) DO NOTHING
                """,
                (
                    row.review_id,
                    row.id,
                    row.revision_id,
                    row.producer_host,
                    row.producer_session_id,
                    row.producer_model,
                    row.attribution,
                    row.repo_root,
                    int(row.schema_version),
                    row.source_identity_json,
                    row.payload_json,
                    row.created_at,
                    row.imported_at or utc_now(),
                ),
            )
            return bool(cursor.rowcount)

    def list_author_guidance(self, review_id: str) -> tuple[AuthorGuidanceRow, ...]:
        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM review_author_guidance WHERE review_id = ? ORDER BY created_at, id",
                (review_id,),
            ).fetchall()
        return tuple(
            AuthorGuidanceRow(
                review_id=str(row["review_id"]),
                id=str(row["id"]),
                revision_id=str(row["revision_id"] or ""),
                producer_host=str(row["producer_host"] or ""),
                producer_session_id=str(row["producer_session_id"] or ""),
                producer_model=str(row["producer_model"] or ""),
                attribution=str(row["attribution"] or "unattributed"),
                repo_root=str(row["repo_root"] or ""),
                schema_version=int(row["schema_version"] or 1),
                source_identity_json=str(row["source_identity_json"] or "[]"),
                payload_json=str(row["payload_json"]),
                created_at=str(row["created_at"]),
                imported_at=str(row["imported_at"] or ""),
            )
            for row in rows
        )

    def append_decision_event(
        self,
        event: ReviewDecisionEvent,
        *,
        expected_prior_event_id: str,
    ) -> ReviewDecisionEvent:
        """Append one human decision event with compare-and-set semantics.

        The write commits only while ``event.revision_id`` is still the
        Review's current Snapshot and ``expected_prior_event_id`` is still the
        newest event for this reviewer's decision, so an action meant for
        Snapshot N can never land on N+1 and two tabs cannot both win.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"decision:{event.review_id}:{event.decision_key}")
            session = conn.execute(
                "SELECT current_revision_id FROM review_sessions WHERE id = ?",
                (event.review_id,),
            ).fetchone()
            if session is None:
                raise ValueError(f"no such review session {event.review_id!r}")
            if str(session["current_revision_id"] or "") != event.revision_id:
                raise ValueError("Snapshot changed since you loaded this decision; reload before deciding")
            latest = conn.execute(
                """
                SELECT id, created_at FROM review_decision_events
                WHERE review_id = ? AND reviewer_id = ? AND decision_key = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (event.review_id, event.reviewer_id, event.decision_key),
            ).fetchone()
            latest_id = "" if latest is None else str(latest["id"])
            if latest_id != expected_prior_event_id:
                raise ValueError("decision changed since you loaded it; reload before deciding")
            now = utc_now()
            if latest is not None and now <= str(latest["created_at"]):
                # Order is the history: never let a clock tie reorder two events.
                now = _after_timestamp(str(latest["created_at"]))
            record = replace(
                event,
                id=event.id or f"dec-{make_uuid7()}",
                prior_event_id=expected_prior_event_id,
                created_at=now,
            )
            conn.execute(
                """
                INSERT INTO review_decision_events (
                    review_id, id, decision_key, reviewer_id, revision_id, action, disposition,
                    option_key, definition_ref, semantic_fingerprint, implementation_fingerprint,
                    scope_json, implementation_json, reason, trigger_kind, trigger_check_key,
                    trigger_note, feedback_annotation_id, prior_event_id, actor_type, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.review_id,
                    record.id,
                    record.decision_key,
                    record.reviewer_id,
                    record.revision_id,
                    record.action,
                    record.disposition,
                    record.option_key,
                    record.definition_ref,
                    record.semantic_fingerprint,
                    record.implementation_fingerprint,
                    record.scope_json,
                    record.implementation_json,
                    record.reason,
                    record.trigger_kind,
                    record.trigger_check_key,
                    record.trigger_note,
                    record.feedback_annotation_id,
                    record.prior_event_id,
                    record.actor_type,
                    record.created_at,
                ),
            )
        return record

    def list_decision_events(
        self,
        review_id: str,
        *,
        reviewer_id: str | None = None,
        decision_key: str = "",
    ) -> tuple[ReviewDecisionEvent, ...]:
        """Decision events in the order they happened."""

        self._ensure_ready()
        sql = "SELECT * FROM review_decision_events WHERE review_id = ?"
        params: list[Any] = [review_id]
        if reviewer_id is not None:
            sql += " AND reviewer_id = ?"
            params.append(reviewer_id)
        if decision_key:
            sql += " AND decision_key = ?"
            params.append(decision_key)
        sql += " ORDER BY created_at ASC, id ASC"
        with self._transaction() as conn:
            rows = conn.execute(sql, params).fetchall()
        return tuple(
            ReviewDecisionEvent(
                id=str(row["id"]),
                review_id=str(row["review_id"]),
                decision_key=str(row["decision_key"]),
                reviewer_id=str(row["reviewer_id"]),
                revision_id=str(row["revision_id"]),
                action=cast(DecisionAction, str(row["action"])),
                disposition=cast(DecisionDisposition, str(row["disposition"])),
                option_key=str(row["option_key"] or ""),
                definition_ref=str(row["definition_ref"] or ""),
                semantic_fingerprint=str(row["semantic_fingerprint"] or ""),
                implementation_fingerprint=str(row["implementation_fingerprint"] or ""),
                scope_json=str(row["scope_json"] or "{}"),
                implementation_json=str(row["implementation_json"] or "{}"),
                reason=str(row["reason"] or ""),
                trigger_kind=str(row["trigger_kind"] or ""),
                trigger_check_key=str(row["trigger_check_key"] or ""),
                trigger_note=str(row["trigger_note"] or ""),
                feedback_annotation_id=str(row["feedback_annotation_id"] or ""),
                prior_event_id=str(row["prior_event_id"] or ""),
                actor_type=cast(ActorType, str(row["actor_type"] or "human")),
                created_at=str(row["created_at"]),
            )
            for row in rows
        )

    @staticmethod
    def _intelligence_run_row(row: Any) -> ReviewIntelligenceRun:
        return ReviewIntelligenceRun(
            review_id=str(row["review_id"]),
            id=str(row["id"]),
            revision_id=str(row["revision_id"]),
            snapshot_identity=str(row["snapshot_identity"]),
            capability=str(row["capability"]),
            run_key=str(row["run_key"]),
            scope_digest=str(row["scope_digest"]),
            context_digest=str(row["context_digest"]),
            policy_digest=str(row["policy_digest"]),
            schema_digest=str(row["schema_digest"]),
            driver=str(row["driver"]),
            model=str(row["model"] or ""),
            prompt_version=int(row["prompt_version"]),
            schema_version=int(row["schema_version"]),
            status=cast(ReviewIntelligenceRunStatus, str(row["status"])),
            result_json=str(row["result_json"] or "{}"),
            usage_json=str(row["usage_json"] or "{}"),
            error=str(row["error"] or ""),
            model_calls=int(row["model_calls"] or 0),
            created_at=str(row["created_at"]),
            started_at=str(row["started_at"] or ""),
            completed_at=str(row["completed_at"] or ""),
        )

    def claim_intelligence_run(
        self,
        row: ReviewIntelligenceRun,
        *,
        stale_after_s: float = 240.0,
        bypass_cache: bool = False,
    ) -> tuple[ReviewIntelligenceRun, bool]:
        """Claim one semantic intelligence key; live duplicates converge.

        ``bypass_cache`` (an explicit fresh re-run, P11 item 15b) skips only the
        reuse of a ready row: a live pending duplicate still converges. Without
        it, a newer settled non-ready row (a failed fresh re-run) never hides an
        older ready row from ordinary requests.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"intelligence:{row.review_id}:{row.run_key}")
            existing = conn.execute(
                """
                SELECT * FROM review_intelligence_runs
                WHERE review_id = ? AND run_key = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (row.review_id, row.run_key),
            ).fetchone()
            if existing is not None:
                current = self._intelligence_run_row(existing)
                abandoned = False
                if current.status == "pending":
                    try:
                        age = datetime.now(UTC) - datetime.fromisoformat(current.created_at)
                    except ValueError:
                        abandoned = True
                    else:
                        abandoned = age.total_seconds() >= stale_after_s
                if (current.status == "ready" and not bypass_cache) or (current.status == "pending" and not abandoned):
                    return current, False
                if current.status == "pending":
                    conn.execute(
                        """
                        UPDATE review_intelligence_runs
                        SET status = 'failed', error = ?, completed_at = ?
                        WHERE review_id = ? AND id = ? AND status = 'pending'
                        """,
                        ("abandoned intelligence run", utc_now(), current.review_id, current.id),
                    )
                if current.status != "ready" and not bypass_cache:
                    ready = conn.execute(
                        """
                        SELECT * FROM review_intelligence_runs
                        WHERE review_id = ? AND run_key = ? AND status = 'ready'
                        ORDER BY created_at DESC, id DESC LIMIT 1
                        """,
                        (row.review_id, row.run_key),
                    ).fetchone()
                    if ready is not None:
                        return self._intelligence_run_row(ready), False
            now = utc_now()
            record = replace(
                row,
                id=f"rir-{make_uuid7()}",
                status="pending",
                result_json="{}",
                usage_json="{}",
                error="",
                model_calls=0,
                created_at=now,
                started_at=now,
                completed_at="",
            )
            conn.execute(
                """
                INSERT INTO review_intelligence_runs (
                    review_id, id, revision_id, snapshot_identity, capability, run_key,
                    scope_digest, context_digest, policy_digest, schema_digest,
                    driver, model, prompt_version, schema_version, status,
                    result_json, usage_json, error, model_calls,
                    created_at, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.review_id,
                    record.id,
                    record.revision_id,
                    record.snapshot_identity,
                    record.capability,
                    record.run_key,
                    record.scope_digest,
                    record.context_digest,
                    record.policy_digest,
                    record.schema_digest,
                    record.driver,
                    record.model,
                    record.prompt_version,
                    record.schema_version,
                    record.status,
                    record.result_json,
                    record.usage_json,
                    record.error,
                    record.model_calls,
                    record.created_at,
                    record.started_at,
                    record.completed_at,
                ),
            )
        return record, True

    def complete_intelligence_run(
        self,
        review_id: str,
        row_id: str,
        *,
        status: ReviewIntelligenceRunStatus,
        result_json: str = "{}",
        usage_json: str = "{}",
        error: str = "",
        model_calls: int = 0,
    ) -> bool:
        """Compare-and-set one claimed intelligence run to a terminal state."""

        if status == "pending":
            raise ValueError("an intelligence run completes as ready, failed, or cancelled")
        self._ensure_ready()
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE review_intelligence_runs
                SET status = ?, result_json = ?, usage_json = ?, error = ?,
                    model_calls = ?, completed_at = ?
                WHERE review_id = ? AND id = ? AND status = 'pending'
                """,
                (
                    status,
                    result_json or "{}",
                    usage_json or "{}",
                    error,
                    max(0, int(model_calls)),
                    utc_now(),
                    review_id,
                    row_id,
                ),
            )
            return int(cursor.rowcount or 0) == 1

    def get_intelligence_run(self, review_id: str, run_key: str) -> ReviewIntelligenceRun | None:
        """Newest durable attempt for one exact semantic intelligence key."""

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM review_intelligence_runs
                WHERE review_id = ? AND run_key = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (review_id, run_key),
            ).fetchone()
        return None if row is None else self._intelligence_run_row(row)

    def list_intelligence_runs(
        self,
        review_id: str,
        *,
        revision_id: str = "",
        capability: str = "",
    ) -> tuple[ReviewIntelligenceRun, ...]:
        """Inspect durable intelligence history without triggering inference."""

        self._ensure_ready()
        clauses = ["review_id = ?"]
        values: list[Any] = [review_id]
        if revision_id:
            clauses.append("revision_id = ?")
            values.append(revision_id)
        if capability:
            clauses.append("capability = ?")
            values.append(capability)
        where = " AND ".join(clauses)
        with self._transaction() as conn:
            rows = conn.execute(
                f"SELECT * FROM review_intelligence_runs WHERE {where} ORDER BY created_at DESC, id DESC",
                tuple(values),
            ).fetchall()
        return tuple(self._intelligence_run_row(item) for item in rows)

    def intelligence_calls(self, review_id: str) -> int:
        """Cumulative model calls spent by every intelligence capability."""

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT model_calls FROM review_intelligence_spend WHERE review_id = ?",
                (review_id,),
            ).fetchone()
        return 0 if row is None else int(row["model_calls"])

    def reserve_intelligence_call(self, review_id: str, *, limit: int) -> bool:
        """Atomically reserve one provider call under the Review-wide ceiling."""

        self._ensure_ready()
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"intelligence-spend:{review_id}")
            row = conn.execute(
                "SELECT model_calls FROM review_intelligence_spend WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            spent = 0 if row is None else int(row["model_calls"])
            if spent >= max(0, limit):
                return False
            conn.execute(
                """
                INSERT INTO review_intelligence_spend (review_id, model_calls, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(review_id) DO UPDATE SET
                  model_calls = review_intelligence_spend.model_calls + 1,
                  updated_at = excluded.updated_at
                """,
                (review_id, utc_now()),
            )
        return True

    @staticmethod
    def _synthesis_row(row: Any) -> ReviewSynthesisRow:
        return ReviewSynthesisRow(
            review_id=str(row["review_id"]),
            id=str(row["id"]),
            revision_id=str(row["revision_id"]),
            synthesis_key=str(row["synthesis_key"]),
            schema_version=int(row["schema_version"]),
            prompt_version=int(row["prompt_version"]),
            model=str(row["model"] or ""),
            status=cast(SynthesisStatus, str(row["status"])),
            payload_json=str(row["payload_json"] or "{}"),
            error=str(row["error"] or ""),
            created_at=str(row["created_at"]),
            completed_at=str(row["completed_at"] or ""),
            inputs_digest=str(row["inputs_digest"] or ""),
        )

    def claim_synthesis(
        self,
        row: ReviewSynthesisRow,
        *,
        stale_after_s: float = 180.0,
    ) -> tuple[ReviewSynthesisRow, bool]:
        """Claim the model call for one exact synthesis key.

        Returns ``(row, True)`` with a new ``pending`` row when this caller
        must run the model, or ``(existing, False)`` when a ready or live
        pending row already answers the key, so concurrent requests make one
        call. A ``failed`` row, or a ``pending`` row older than
        *stale_after_s* (an abandoned claim), is replaced; deleting it means
        the abandoned claim can never publish.
        """

        self._ensure_ready()
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"synthesis:{row.review_id}:{row.synthesis_key}")
            existing = conn.execute(
                """
                SELECT * FROM review_plan_syntheses
                WHERE review_id = ? AND synthesis_key = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (row.review_id, row.synthesis_key),
            ).fetchone()
            if existing is not None:
                current = self._synthesis_row(existing)
                abandoned = False
                if current.status == "pending":
                    try:
                        age = datetime.now(UTC) - datetime.fromisoformat(current.created_at)
                    except ValueError:
                        abandoned = True
                    else:
                        abandoned = age.total_seconds() >= stale_after_s
                if current.status == "ready" or (current.status == "pending" and not abandoned):
                    if row.inputs_digest and current.inputs_digest != row.inputs_digest:
                        # Same exact inputs reached under a new cheap digest (an
                        # identical guidance re-send): readers look rows up by it.
                        conn.execute(
                            "UPDATE review_plan_syntheses SET inputs_digest = ? WHERE review_id = ? AND id = ?",
                            (row.inputs_digest, current.review_id, current.id),
                        )
                        current = replace(current, inputs_digest=row.inputs_digest)
                    return current, False
                conn.execute(
                    "DELETE FROM review_plan_syntheses WHERE review_id = ? AND synthesis_key = ?",
                    (row.review_id, row.synthesis_key),
                )
            record = replace(
                row,
                id=f"syn-{make_uuid7()}",
                status="pending",
                payload_json="{}",
                error="",
                created_at=utc_now(),
                completed_at="",
            )
            conn.execute(
                """
                INSERT INTO review_plan_syntheses (
                    review_id, id, revision_id, synthesis_key, schema_version, prompt_version,
                    model, status, payload_json, error, created_at, completed_at, inputs_digest
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.review_id,
                    record.id,
                    record.revision_id,
                    record.synthesis_key,
                    record.schema_version,
                    record.prompt_version,
                    record.model,
                    record.status,
                    record.payload_json,
                    record.error,
                    record.created_at,
                    record.completed_at,
                    record.inputs_digest,
                ),
            )
        return record, True

    def complete_synthesis(
        self,
        review_id: str,
        row_id: str,
        *,
        status: SynthesisStatus,
        payload_json: str = "",
        error: str = "",
    ) -> bool:
        """Publish a claimed synthesis; only a still-``pending`` row changes."""

        if status == "pending":
            raise ValueError("a synthesis completes as ready or failed")
        self._ensure_ready()
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                UPDATE review_plan_syntheses
                SET status = ?, payload_json = ?, error = ?, completed_at = ?
                WHERE review_id = ? AND id = ? AND status = 'pending'
                """,
                (status, payload_json or "{}", error, utc_now(), review_id, row_id),
            )
            return int(cursor.rowcount or 0) == 1

    def get_synthesis(self, review_id: str, synthesis_key: str) -> ReviewSynthesisRow | None:
        """The live row for one exact synthesis key, if any."""

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM review_plan_syntheses
                WHERE review_id = ? AND synthesis_key = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (review_id, synthesis_key),
            ).fetchone()
        return None if row is None else self._synthesis_row(row)

    def find_synthesis(self, review_id: str, *, revision_id: str, inputs_digest: str) -> ReviewSynthesisRow | None:
        """The newest synthesis for one Snapshot's exact inputs, if any."""

        self._ensure_ready()
        with self._transaction() as conn:
            row = conn.execute(
                """
                SELECT * FROM review_plan_syntheses
                WHERE review_id = ? AND revision_id = ? AND inputs_digest = ?
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (review_id, revision_id, inputs_digest),
            ).fetchone()
        return None if row is None else self._synthesis_row(row)

    def list_syntheses(self, review_id: str) -> tuple[ReviewSynthesisRow, ...]:
        """Every cached synthesis for one Review, newest first."""

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM review_plan_syntheses WHERE review_id = ? ORDER BY created_at DESC, id DESC",
                (review_id,),
            ).fetchall()
        return tuple(self._synthesis_row(row) for row in rows)

    def prune_syntheses(self, review_id: str, *, keep: int = 3) -> int:
        """Keep the newest *keep* settled rows plus every pending claim."""

        self._ensure_ready()
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"synthesis-prune:{review_id}")
            settled = conn.execute(
                """
                SELECT id FROM review_plan_syntheses
                WHERE review_id = ? AND status <> 'pending'
                ORDER BY created_at DESC, id DESC
                """,
                (review_id,),
            ).fetchall()
            doomed = [str(row["id"]) for row in settled[max(0, keep) :]]
            for row_id in doomed:
                conn.execute(
                    "DELETE FROM review_plan_syntheses WHERE review_id = ? AND id = ?",
                    (review_id, row_id),
                )
        return len(doomed)

    def synthesis_calls(self, review_id: str) -> int:
        """Compatibility view of the now-universal Review Intelligence spend."""

        return self.intelligence_calls(review_id)

    def reserve_synthesis_call(self, review_id: str, *, limit: int) -> bool:
        """Compatibility wrapper; synthesis shares the universal Review budget."""

        return self.reserve_intelligence_call(review_id, limit=limit)

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

    def discard_artifact(self, artifact_path: str) -> None:
        """Best-effort cleanup for an artifact whose metadata write lost a CAS race."""

        if artifact_path:
            self._delete_artifact_prefix(artifact_path)

    def run_artifacts(self, *, retention_seconds: int | None = None) -> RunArtifactStore | None:
        """Raw run-artifact store of this Review store, with its reference-aware closer.

        Local stores keep the filesystem layout beside the store; hosted stores
        put bytes and metadata through their org-scoped artifact backend.
        ``None`` when that backend cannot enumerate keys, so nothing is written
        whose retention could not be enforced.
        """

        from lemoncrow.pro.capabilities.review.run_evidence import (
            DEFAULT_RAW_RETENTION_SECONDS,
            RunArtifactStore,
            review_reference_resolver,
        )

        try:
            return RunArtifactStore.for_store(
                self.root,
                retention_seconds=retention_seconds or DEFAULT_RAW_RETENTION_SECONDS,
                backend=self._artifact_backend,
                resolver=review_reference_resolver(self),
            )
        except ValueError:
            return None

    def expire_run_artifacts(self) -> tuple[str, ...]:
        """Closer pass: expired, unreferenced raw run artifacts of every Review here."""

        artifacts = self.run_artifacts()
        return () if artifacts is None else artifacts.expire(resolver=artifacts.resolver)

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

    def ranking_history_relpath(self, review_id: str, revision_id: str) -> str:
        review_seg = safe_segment(review_id, field="review_id")
        revision_seg = safe_segment(revision_id, field="revision_id")
        return f"review/{ARTIFACT_DIRNAME}/{review_seg}/{revision_seg}/{RANKING_HISTORY_FILENAME}"

    def read_ranking_history(self, review_id: str, revision_id: str) -> tuple[tuple[str, dict[str, int]], ...]:
        """Every ranking an enrichment replaced on one Snapshot, oldest first.

        Each entry is ``(superseded_at, {unit_key: rank})``: the ranks that were
        current until that moment, for the units the enrichment moved. Empty when
        the Snapshot was never re-ranked or the record is unreadable; then nothing
        is pinned and every reviewer sees the current ranking.
        """

        self._ensure_ready()
        try:
            rel = self.ranking_history_relpath(review_id, revision_id)
        except ValueError:
            return ()
        raw = self._read_artifact_bytes(rel)
        if raw is None:
            return ()
        try:
            payload = json.loads(gzip.decompress(raw).decode("utf-8"))
        except (EOFError, OSError, UnicodeDecodeError, ValueError, zlib.error):
            return ()
        if not isinstance(payload, dict) or payload.get("version") != RANKING_HISTORY_VERSION:
            return ()
        out: list[tuple[str, dict[str, int]]] = []
        for row in payload.get("generations") or ():
            at = row.get("superseded_at") if isinstance(row, dict) else None
            ranks = row.get("ranks") if isinstance(row, dict) else None
            if not isinstance(at, str) or not at or not isinstance(ranks, dict):
                return ()
            if not all(
                isinstance(key, str) and isinstance(rank, int) and not isinstance(rank, bool)
                for key, rank in ranks.items()
            ):
                return ()
            out.append((at, dict(ranks)))
        return tuple(out)

    def _append_ranking_generation(self, review_id: str, revision_id: str, ranks: Mapping[str, int]) -> None:
        """Keep the ranks one enrichment is about to replace (the writer of ``read_ranking_history``).

        The record lives in the revision's artifact directory, so its closer is
        that directory's: deleted with the Review (``delete_archived_session``,
        ``prune``) or with a revision that never got a row, and kept by hosted
        orphan-artifact GC exactly while the revision row exists. One generation
        is appended per re-rank that moved a unit; a Snapshot is re-ranked by its
        one progressive enrichment.
        """

        generations = [
            {"superseded_at": at, "ranks": previous}
            for at, previous in self.read_ranking_history(review_id, revision_id)
        ]
        generations.append({"superseded_at": utc_now(), "ranks": dict(sorted(ranks.items()))})
        raw = json.dumps(
            {"version": RANKING_HISTORY_VERSION, "generations": generations}, separators=(",", ":")
        ).encode("utf-8")
        self._write_artifact_bytes(self.ranking_history_relpath(review_id, revision_id), gzip.compress(raw, mtime=0))

    def read_routine_proofs(
        self, review_id: str, revision_id: str, *, version: str
    ) -> dict[str, dict[str, str]] | None:
        """Persisted routine-edit proofs for one immutable Snapshot, or ``None``.

        ``version`` names the proof rules; a stored result from other rules is a miss.
        """

        self._ensure_ready()
        try:
            rel = self.routine_proofs_relpath(review_id, revision_id)
        except ValueError:
            return None
        raw = self._read_artifact_bytes(rel)
        if raw is None:
            return None
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        if not isinstance(payload, dict) or payload.get("version") != version:
            return None
        proofs = payload.get("proofs")
        if not isinstance(proofs, dict) or not all(
            isinstance(k, str) and isinstance(v, dict) and all(isinstance(x, str) for x in v.values())
            for k, v in proofs.items()
        ):
            return None
        return {k: dict(v) for k, v in proofs.items()}

    def write_routine_proofs(
        self, review_id: str, revision_id: str, proofs: Mapping[str, Mapping[str, str]], *, version: str
    ) -> None:
        self._ensure_ready()
        rel = self.routine_proofs_relpath(review_id, revision_id)
        raw = json.dumps({"version": version, "proofs": proofs}, separators=(",", ":")).encode("utf-8")
        self._write_artifact_bytes(rel, raw)

    def routine_proofs_relpath(self, review_id: str, revision_id: str) -> str:
        review_seg = safe_segment(review_id, field="review_id")
        revision_seg = safe_segment(revision_id, field="revision_id")
        return f"review/{ARTIFACT_DIRNAME}/{review_seg}/{revision_seg}/routine-proofs.json"

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
        referenced source blobs stay live after the transient View that
        captured the Review expires.
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

    #: Review children a deletion tombstone counts (present in every store composition).
    DELETION_TOMBSTONE_COUNTED: ClassVar[tuple[str, ...]] = (
        "review_revisions",
        "annotations",
        "annotation_versions",
        "review_activity_events",
        "review_relationships",
        "review_evidence",
        "review_marks",
    )

    def delete_archived_session(self, review_id: str, *, tombstone: ReviewDeletionTombstone | None = None) -> bool:
        """Delete one explicitly archived Review, its search entries and artifacts.

        With *tombstone*, the durable deletion record is written first, in the
        same transaction: if it cannot be written nothing is deleted. Unsent
        work queued for an attached hosted Review refuses the delete
        (``unresolved_actions``), exactly as it refuses a detach.
        """
        self._ensure_ready()
        with self._transaction() as conn:
            self._lock_for_write(conn, key=f"session-status:{review_id}")
            # Acquire the row's write lock too: PostgreSQL status writers may use
            # other advisory keys. A concurrent restore must win or wait here.
            locked = conn.execute(
                "UPDATE review_sessions SET status = status WHERE id = ? AND status = 'archived'",
                (review_id,),
            ).rowcount
            if not locked:
                row = conn.execute("SELECT status FROM review_sessions WHERE id = ?", (review_id,)).fetchone()
                if row is None:
                    return False
                raise ValueError("only archived reviews can be deleted")
            if self.hosted_attachment_supported():
                unresolved = self._unresolved_hosted_actions(conn, review_id)
                if unresolved:
                    raise HostedAttachmentConflict(
                        "unresolved_actions",
                        f"{unresolved} queued action(s) were never applied on hosted; sync or discard them, "
                        "or detach with discard_unresolved=true, before deleting",
                    )
            if tombstone is not None:
                self._write_deletion_tombstone(conn, review_id, tombstone)
            for annotation in conn.execute(
                "SELECT id, body, path FROM annotations WHERE review_id = ?", (review_id,)
            ).fetchall():
                self._annotation_search_delete(
                    conn, str(annotation["id"]), str(annotation["body"]), str(annotation["path"])
                )
            conn.execute("DELETE FROM review_sessions WHERE id = ? AND status = 'archived'", (review_id,))
        self._remove_artifacts(review_id)
        return True

    def _write_deletion_tombstone(
        self, conn: Any, review_id: str, tombstone: ReviewDeletionTombstone
    ) -> ReviewDeletionTombstone:
        """Append the chained tombstone for *review_id* inside the caller's deletion transaction."""

        if tombstone.review_id != review_id:
            raise ValueError("a deletion tombstone must name the Review being deleted")
        counts = [("review_sessions", 1)]
        for table in self.DELETION_TOMBSTONE_COUNTED:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE review_id = ?", (review_id,)).fetchone()
            counts.append((table, int(row["n"]) if row is not None else 0))
        # One chain per store: serialize appenders so each links to the last.
        self._lock_for_write(conn, key="review-deletion-tombstones")
        last = conn.execute(
            "SELECT sequence, record_sha256 FROM review_deletion_tombstones ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        stored = replace(
            tombstone,
            id=make_uuid7(),
            sequence=(int(last["sequence"]) + 1) if last is not None else 1,
            counts=tuple(counts),
            deleted_at=utc_now(),
            previous_sha256=str(last["record_sha256"]) if last is not None else "",
        )
        stored = replace(stored, record_sha256=deletion_tombstone_digest(stored))
        conn.execute(
            """INSERT INTO review_deletion_tombstones (
                sequence, id, review_id, repo_id, actor_id, request_id, confirmation_sha256,
                confirmed_by, counts_json, deleted_at, previous_sha256, record_sha256
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                stored.sequence,
                stored.id,
                stored.review_id,
                stored.repo_id,
                stored.actor_id,
                stored.request_id,
                stored.confirmation_sha256,
                stored.confirmed_by,
                json.dumps(dict(stored.counts), sort_keys=True),
                stored.deleted_at,
                stored.previous_sha256,
                stored.record_sha256,
            ),
        )
        return stored

    def deletion_tombstones(self, review_id: str = "") -> tuple[ReviewDeletionTombstone, ...]:
        """Deletion tombstones in chain order (all, or those naming *review_id*)."""

        self._ensure_ready()
        sql = "SELECT * FROM review_deletion_tombstones"
        params: tuple[str, ...] = ()
        if review_id:
            sql += " WHERE review_id = ?"
            params = (review_id,)
        with self._transaction() as conn:
            rows = conn.execute(sql + " ORDER BY sequence ASC", params).fetchall()
        return tuple(_row_to_deletion_tombstone(row) for row in rows)

    def verify_deletion_tombstones(self) -> str:
        """``""`` when the tombstone chain is intact, else why it is not (the first broken link)."""

        previous = ""
        expected = 1
        for item in self.deletion_tombstones():
            if item.sequence != expected:
                return f"tombstone {expected} is missing"
            if item.previous_sha256 != previous:
                return f"tombstone {item.sequence} does not link to its predecessor"
            if deletion_tombstone_digest(item) != item.record_sha256:
                return f"tombstone {item.sequence} was altered"
            previous = item.record_sha256
            expected += 1
        return ""

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
            self._lock_for_write(conn, key="project-knowledge")
            conn.execute(
                "DELETE FROM project_knowledge_entries WHERE state IN ('historical', 'superseded') AND updated_at < ?",
                (cutoff,),
            )
            conn.execute(
                "DELETE FROM project_knowledge_events WHERE observed_at < ? AND generation_id NOT IN (SELECT current_generation FROM project_knowledge_meta)",
                (cutoff,),
            )
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

    def list_all_annotations(self) -> tuple[Annotation, ...]:
        """All explicit annotation records in this repository-scoped store."""

        self._ensure_ready()
        with self._transaction() as conn:
            rows = conn.execute("SELECT * FROM annotations ORDER BY created_at ASC, id ASC").fetchall()
        return tuple(_row_to_annotation(row) for row in rows)

    def dismissed_rule_suggestion_ids(self, *, repository_scope: str = "") -> tuple[str, ...]:
        """Durable dismissal only; a suggestion row is never authoritative policy."""

        self._ensure_ready()
        with self._transaction() as conn:
            if repository_scope:
                rows = conn.execute(
                    "SELECT id FROM review_rule_suggestion_states WHERE repository_scope = ? AND state = 'dismissed' ORDER BY id",
                    (repository_scope,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id FROM review_rule_suggestion_states WHERE state = 'dismissed' ORDER BY id"
                ).fetchall()
        return tuple(str(row["id"]) for row in rows)

    def dismiss_rule_suggestion(self, suggestion_id: str, *, repository_scope: str, actor_id: str = "") -> None:
        """Dismiss an inert suggestion without changing Review policy."""

        self._ensure_ready()
        now = utc_now()
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO review_rule_suggestion_states (id, repository_scope, state, actor_id, updated_at)
                VALUES (?, ?, 'dismissed', ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  repository_scope=excluded.repository_scope,
                  state='dismissed',
                  actor_id=excluded.actor_id,
                  updated_at=excluded.updated_at
                """,
                (suggestion_id, repository_scope, actor_id, now),
            )

    def _remove_artifacts(self, review_id: str) -> None:
        try:
            segment = safe_segment(review_id, field="review_id")
        except ValueError:
            return
        self._delete_artifact_prefix(f"review/{ARTIFACT_DIRNAME}/{segment}/")
        # Raw run artifacts (sink payloads, logs) go with their Review, local or hosted.
        with contextlib.suppress(OSError, ValueError):
            artifacts = self.run_artifacts()
            if artifacts is not None:
                artifacts.delete_review(review_id)


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


def _binding_lock_key(repository_identity: str, subject_kind: str, subject_key: str) -> str:
    return f"review-binding:{repository_identity}:{subject_kind}:{subject_key}"


def _row_to_binding(row: sqlite3.Row) -> ReviewBinding:
    return ReviewBinding(
        id=str(row["id"]),
        repository_identity=str(row["repository_identity"]),
        subject_kind=str(row["subject_kind"]),  # type: ignore[arg-type]
        subject_key=str(row["subject_key"]),
        review_id=str(row["review_id"]),
        worktree_key=str(row["worktree_key"]),
        active=bool(row["active"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_commit(row: sqlite3.Row) -> ReviewCommit:
    return ReviewCommit(
        review_id=str(row["review_id"]),
        commit_number=int(row["commit_number"]),
        commit_sha=str(row["commit_sha"]),
        revision_id=str(row["revision_id"]),
        parent_sha=str(row["parent_sha"]),
        state=str(row["state"]),  # type: ignore[arg-type]
        created_at=str(row["created_at"]),
    )


def _row_to_relationship(row: sqlite3.Row) -> ReviewRelationship:
    return ReviewRelationship(
        id=str(row["id"]),
        review_id=str(row["review_id"]),
        kind=str(row["kind"]),
        relation=str(row["relation"]),
        provider=str(row["provider"]),
        external_id=str(row["external_id"]),
        target_review_id=str(row["target_review_id"]),
        url=str(row["url"]),
        title=str(row["title"]),
        status=str(row["status"]),
        source=str(row["source"]),
        metadata_json=str(row["metadata_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_session(row: sqlite3.Row) -> ReviewSession:
    return ReviewSession(
        id=str(row["id"]),
        subject_type=str(row["subject_type"]),  # type: ignore[arg-type]
        repo_root=str(row["repo_root"]),
        range_mode=str(row["range_mode"]),  # type: ignore[arg-type]
        title=str(row["title"]),
        source_ref=str(row["source_ref"]),
        review_base_sha=str(row["review_base_sha"]) if "review_base_sha" in row.keys() else "",
        review_base_policy=str(row["review_base_policy"]) if "review_base_policy" in row.keys() else "integrated",  # type: ignore[arg-type]
        actor_type=str(row["actor_type"]),  # type: ignore[arg-type]
        status=str(row["status"]),  # type: ignore[arg-type]
        reviewer_id=str(row["reviewer_id"]),
        current_revision_id=str(row["current_revision_id"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_revision(row: sqlite3.Row) -> ReviewRevision:
    keys = set(row.keys())
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
        source_view_id=str(row["source_view_id"]) if "source_view_id" in keys else "",
        source_view_revision=int(row["source_view_revision"]) if "source_view_revision" in keys else 0,
        capture_kind=str(row["capture_kind"]) if "capture_kind" in keys else "legacy",  # type: ignore[arg-type]
        commit_sha=str(row["commit_sha"]) if "commit_sha" in keys else "",
        branch_ref=str(row["branch_ref"]) if "branch_ref" in keys else "",
        worktree_key=str(row["worktree_key"]) if "worktree_key" in keys else "",
        title=str(row["title"]) if "title" in keys else "",
    )


def _row_to_attribution(row: sqlite3.Row) -> ReviewAttribution:
    return ReviewAttribution(
        revision_id=str(row["revision_id"]),
        scope=str(row["scope"]),  # type: ignore[arg-type]
        scope_key=str(row["scope_key"]),
        host=str(row["host"]),
        session_id=str(row["session_id"]),
        role=str(row["role"]),  # type: ignore[arg-type]
        model=str(row["model"]),
        certainty=str(row["certainty"]),  # type: ignore[arg-type]
        method=str(row["method"]),
        created_at=str(row["created_at"]),
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


def _row_to_ask_conversation(row: sqlite3.Row) -> ReviewAskConversationRecord:
    return ReviewAskConversationRecord(
        id=str(row["id"]),
        review_id=str(row["review_id"]),
        title=str(row["title"]),
        origin_json=str(row["origin_json"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_ask_message(row: sqlite3.Row) -> ReviewAskMessage:
    return ReviewAskMessage(
        id=str(row["id"]),
        review_id=str(row["review_id"]),
        revision_id=str(row["revision_id"]),
        role=cast(ReviewAskRole, str(row["role"])),
        intent=cast(ReviewAskIntent, str(row["intent"])),
        body=str(row["body"]),
        target_unit_key=str(row["target_unit_key"]),
        path=str(row["path"]),
        symbol=str(row["symbol"]),
        side=str(row["side"]),
        start_line=int(row["start_line"]),
        end_line=int(row["end_line"]),
        context_json=str(row["context_json"]),
        references_json=str(row["references_json"]),
        action_json=str(row["action_json"]),
        created_at=str(row["created_at"]),
        conversation_id=str(row["conversation_id"]) if "conversation_id" in row.keys() else "",
    )


def _row_to_ask_handoff(row: sqlite3.Row) -> ReviewAskHandoff:
    return ReviewAskHandoff(
        message_id=str(row["message_id"]),
        operation_id=str(row["operation_id"]),
        instruction_hash=str(row["instruction_hash"]),
        state=cast(ReviewAskHandoffState, str(row["state"])),
        target_ref=str(row["target_ref"]),
        remote_ref=str(row["remote_ref"]),
        last_error=str(row["last_error"]),
        updated_at=str(row["updated_at"]),
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
        finding_disposition=cast(
            FindingDisposition,
            str(row["finding_disposition"]) if "finding_disposition" in row.keys() else "",
        ),
        finding_disposition_reason=cast(
            FindingDismissalReason,
            str(row["finding_disposition_reason"]) if "finding_disposition_reason" in row.keys() else "",
        ),
        finding_disposition_revision_id=(
            str(row["finding_disposition_revision_id"]) if "finding_disposition_revision_id" in row.keys() else ""
        ),
        finding_disposition_reviewer_id=(
            str(row["finding_disposition_reviewer_id"]) if "finding_disposition_reviewer_id" in row.keys() else ""
        ),
        finding_disposition_evidence=(
            _json_tuple(row["finding_disposition_evidence_json"])
            if "finding_disposition_evidence_json" in row.keys()
            else ()
        ),
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
        agent_role=str(row["agent_role"]),
        agent_host=str(row["agent_host"]),
        agent_model=str(row["agent_model"]),
        parent_session_id=str(row["parent_session_id"]),
        source_message_id=str(row["source_message_id"]),
        title=str(row["title"]),
        evidence=_json_tuple(row["evidence_json"]),
        rule_refs=_json_tuple(row["rule_refs_json"]) if "rule_refs_json" in row.keys() else (),
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        author_response=str(row["author_response"]),  # type: ignore[arg-type]
        author_response_source_id=str(row["author_response_source_id"]),
        resolved_revision_id=str(row["resolved_revision_id"]),
        author_response_at=str(row["author_response_at"]),
        turn_owner_kind=str(row["turn_owner_kind"]),  # type: ignore[arg-type]
        turn_owner_id=str(row["turn_owner_id"]),
        finding_disposition=cast(
            FindingDisposition,
            str(row["finding_disposition"]) if "finding_disposition" in row.keys() else "",
        ),
        finding_disposition_reason=cast(
            FindingDismissalReason,
            str(row["finding_disposition_reason"]) if "finding_disposition_reason" in row.keys() else "",
        ),
        finding_disposition_revision_id=(
            str(row["finding_disposition_revision_id"]) if "finding_disposition_revision_id" in row.keys() else ""
        ),
        finding_disposition_reviewer_id=(
            str(row["finding_disposition_reviewer_id"]) if "finding_disposition_reviewer_id" in row.keys() else ""
        ),
        finding_disposition_evidence=(
            _json_tuple(row["finding_disposition_evidence_json"])
            if "finding_disposition_evidence_json" in row.keys()
            else ()
        ),
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
        attestation_class=str(row["attestation_class"]),
        stable_key=str(row["stable_key"]),
        provider=str(row["provider"]),
        integration_id=str(row["integration_id"]),
        provider_revision=str(row["provider_revision"]),
        branch=str(row["branch"]),
        environment=str(row["environment"]),
        attestation_scope=str(row["attestation_scope"]),
        attested_by=str(row["attested_by"]),
        remote_identity=str(row["remote_identity"]),
        observed_at=str(row["observed_at"]),
        waived_requirement_key=str(row["waived_requirement_key"]),
        execution_intent=str(row["execution_intent"]),
        executor_class=str(row["executor_class"]),
        execution_verification=str(row["execution_verification"]),
    )


__all__ = [
    "DB_NAME",
    "SCHEMA",
    "ReviewStore",
    "new_annotation_id",
    "new_ask_message_id",
    "new_delivery_id",
    "new_evidence_id",
    "new_revision_id",
    "new_session_id",
    "utc_now",
]
