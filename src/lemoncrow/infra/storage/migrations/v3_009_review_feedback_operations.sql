ALTER TABLE deliveries ADD COLUMN operation_id TEXT NOT NULL DEFAULT '';
ALTER TABLE deliveries ADD COLUMN revision_id TEXT NOT NULL DEFAULT '';
ALTER TABLE deliveries ADD COLUMN feedback_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE deliveries ADD COLUMN annotation_version INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_deliveries_operation ON deliveries(operation_id, updated_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_deliveries_operation_annotation
  ON deliveries(operation_id, annotation_id) WHERE operation_id <> '';
