-- P2-5: server-authoritative delayed send. No side effect may run while pending.
-- The unique active review constraint is enforced in ReviewStore's write transaction.
CREATE TABLE IF NOT EXISTS review_flow_sends (
  id TEXT PRIMARY KEY,
  review_id TEXT NOT NULL REFERENCES review_sessions(id) ON DELETE CASCADE,
  revision_id TEXT NOT NULL REFERENCES review_revisions(id),
  reviewer_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('pending','dispatching','sent','failed','undone','uncertain')),
  send_after TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_review_flow_sends_due ON review_flow_sends(state,send_after);
CREATE INDEX IF NOT EXISTS idx_review_flow_sends_review ON review_flow_sends(review_id,created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_review_flow_sends_active ON review_flow_sends(review_id)
  WHERE state IN ('pending','dispatching');
