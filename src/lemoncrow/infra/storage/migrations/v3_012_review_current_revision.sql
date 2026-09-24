-- The active source state is not necessarily the highest revision number.
-- Reverting bytes can legitimately reactivate an older immutable revision.
ALTER TABLE review_sessions
  ADD COLUMN current_revision_id TEXT NOT NULL DEFAULT '';

UPDATE review_sessions
   SET current_revision_id = COALESCE((
       SELECT r.id
         FROM review_revisions AS r
        WHERE r.review_id = review_sessions.id
        ORDER BY r.revision_number DESC
        LIMIT 1
   ), '')
 WHERE current_revision_id = '';

CREATE INDEX IF NOT EXISTS idx_review_sessions_current_revision
  ON review_sessions(current_revision_id);
