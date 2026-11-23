-- Additive, fail-safe provenance for a human note submitted after reconsidering a
-- checked Structure fact. Existing rows default to ordinary; older readers
-- ignore these columns. Neither is editable after creation.
ALTER TABLE annotations ADD COLUMN reconsidered_after_structure INTEGER NOT NULL DEFAULT 0 CHECK(reconsidered_after_structure IN (0, 1));
ALTER TABLE annotations ADD COLUMN structure_revision_id TEXT NOT NULL DEFAULT '';
