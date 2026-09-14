ALTER TABLE annotations ADD COLUMN author_response TEXT NOT NULL DEFAULT 'none';
ALTER TABLE annotations ADD COLUMN author_response_source_id TEXT NOT NULL DEFAULT '';
ALTER TABLE annotations ADD COLUMN author_response_at TEXT NOT NULL DEFAULT '';
