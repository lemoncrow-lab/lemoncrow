CREATE EXTENSION IF NOT EXISTS vector;
ALTER TABLE archival_passage
  ALTER COLUMN embedding TYPE vector({dim}) USING NULL::vector({dim});
CREATE INDEX IF NOT EXISTS ix_archival_passage_embedding
  ON archival_passage USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
