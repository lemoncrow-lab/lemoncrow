ALTER TABLE annotations ADD COLUMN source TEXT NOT NULL DEFAULT 'human';
ALTER TABLE annotations ADD COLUMN source_id TEXT NOT NULL DEFAULT '';
ALTER TABLE annotations ADD COLUMN title TEXT NOT NULL DEFAULT '';
ALTER TABLE annotations ADD COLUMN evidence_json TEXT NOT NULL DEFAULT '[]';
ALTER TABLE annotations ADD COLUMN confidence REAL;
CREATE INDEX IF NOT EXISTS idx_annotations_source ON annotations(review_id, source, state, created_at DESC);
