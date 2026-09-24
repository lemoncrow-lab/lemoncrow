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
