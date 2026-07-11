CREATE TABLE IF NOT EXISTS lesson_candidate (
  id                     TEXT PRIMARY KEY,
  domain                 TEXT NOT NULL,
  cluster_fingerprint    TEXT NOT NULL DEFAULT '',
  kind                   TEXT NOT NULL,
  target_id              TEXT,
  proposed_block_json    TEXT,
  proposed_rubric_check  TEXT,
  evidence_trace_ids     TEXT NOT NULL,
  body                   TEXT NOT NULL DEFAULT '',
  evidence_json          TEXT NOT NULL DEFAULT '{}',
  embedding              BLOB,
  embedding_provenance   TEXT NOT NULL DEFAULT 'legacy_stub',
  confidence             REAL NOT NULL,
  status                 TEXT NOT NULL DEFAULT 'inbox',
  reviewer               TEXT,
  decision_at            TEXT,
  decision_reason        TEXT NOT NULL DEFAULT '',
  created_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lesson_candidate_domain_status_at
  ON lesson_candidate(domain, status, created_at DESC);

CREATE TABLE IF NOT EXISTS lesson_promotion (
  id                  TEXT PRIMARY KEY,
  lesson_id           TEXT NOT NULL REFERENCES lesson_candidate(id),
  published_block_id  TEXT,
  edited_block_id     TEXT,
  pr_url              TEXT NOT NULL DEFAULT '',
  created_at          TEXT NOT NULL
);
