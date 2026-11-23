-- Per-viewer queue state for one Review (Home and queue, P5-1): when the viewer
-- snoozed it until, which Snapshot number it was snoozed on, and where they
-- paused. Never read for anyone but the viewer it names.
--
-- Writer: ReviewStore.set_viewer_state (PUT /reviews/{id}/queue-state): Snooze,
--   Undo and the bulk snooze write snoozed_until; the Reader screens write
--   resume_json as the reviewer moves between parts.
-- Closer: a row is deleted as soon as it holds neither a snooze nor a resume
--   position; every write also drops that viewer's expired snoozes; Review
--   deletion cascades. A snooze also ends when a newer Snapshot is captured
--   (the Inbox ignores a row whose snoozed_revision is behind the Review).
CREATE TABLE IF NOT EXISTS review_viewer_state (
  review_id        TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  viewer_id        TEXT NOT NULL,
  snoozed_until    TEXT NOT NULL DEFAULT '',
  snoozed_revision INTEGER NOT NULL DEFAULT 0,
  resume_json      TEXT NOT NULL DEFAULT '',
  updated_at       TEXT NOT NULL,
  PRIMARY KEY (review_id, viewer_id)
);
CREATE INDEX IF NOT EXISTS idx_review_viewer_state_viewer
  ON review_viewer_state(viewer_id, snoozed_until);
