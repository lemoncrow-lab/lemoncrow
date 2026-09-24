-- Shared Review thread ownership only. Multi-principal participants and reviewer
-- requests are hosted-service state and are created by the private collaboration store.
ALTER TABLE annotations ADD COLUMN turn_owner_kind TEXT NOT NULL DEFAULT 'none';
ALTER TABLE annotations ADD COLUMN turn_owner_id TEXT NOT NULL DEFAULT '';
ALTER TABLE annotation_versions ADD COLUMN turn_owner_kind TEXT NOT NULL DEFAULT 'none';
ALTER TABLE annotation_versions ADD COLUMN turn_owner_id TEXT NOT NULL DEFAULT '';
