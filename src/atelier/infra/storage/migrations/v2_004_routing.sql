CREATE TABLE IF NOT EXISTS route_decision (
  id                    TEXT PRIMARY KEY,
  run_id                TEXT NOT NULL,
  request_id            TEXT NOT NULL DEFAULT '',
  step_index            INTEGER NOT NULL,
  step_type             TEXT NOT NULL,
  risk_level            TEXT NOT NULL,
  tier                  TEXT NOT NULL,
  selected_model        TEXT NOT NULL DEFAULT '',
  confidence            REAL NOT NULL,
  reason                TEXT NOT NULL,
  protected_file_match  INTEGER NOT NULL DEFAULT 0,
  verifier_required     TEXT NOT NULL DEFAULT '[]',
  escalation_trigger    TEXT,
  evidence_refs         TEXT NOT NULL DEFAULT '[]',
  created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_route_decision_run_step ON route_decision(run_id, step_index);

CREATE TABLE IF NOT EXISTS verification_envelope (
  id                    TEXT PRIMARY KEY,
  route_decision_id     TEXT NOT NULL REFERENCES route_decision(id) ON DELETE CASCADE,
  run_id                TEXT NOT NULL,
  changed_files         TEXT NOT NULL DEFAULT '[]',
  validation_results    TEXT NOT NULL DEFAULT '[]',
  rubric_status         TEXT NOT NULL DEFAULT 'not_run',
  outcome               TEXT NOT NULL,
  compressed_evidence   TEXT NOT NULL DEFAULT '',
  human_accepted        INTEGER,
  created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_verification_envelope_route ON verification_envelope(route_decision_id);
CREATE INDEX IF NOT EXISTS ix_verification_envelope_run ON verification_envelope(run_id);
