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
  created_at          TEXT NOT NULL,
  UNIQUE(annotation_id, version_number)
);
CREATE INDEX IF NOT EXISTS idx_annotation_versions_annotation
  ON annotation_versions(annotation_id, version_number ASC);
CREATE INDEX IF NOT EXISTS idx_annotation_versions_review
  ON annotation_versions(review_id, created_at ASC, id ASC);
