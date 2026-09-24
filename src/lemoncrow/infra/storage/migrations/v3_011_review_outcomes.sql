CREATE TABLE IF NOT EXISTS review_outcomes (
  id           TEXT PRIMARY KEY,
  review_id    TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  revision_id  TEXT NOT NULL REFERENCES review_revisions(id) ON DELETE CASCADE,
  reviewer_id  TEXT NOT NULL,
  outcome      TEXT NOT NULL,
  summary      TEXT NOT NULL DEFAULT '',
  created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_review_outcomes_review
  ON review_outcomes(review_id, reviewer_id, created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_review_outcomes_revision
  ON review_outcomes(revision_id, reviewer_id, created_at DESC, id DESC);
