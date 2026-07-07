CREATE TABLE IF NOT EXISTS context_budget (
  id                   TEXT PRIMARY KEY,
  session_id               TEXT NOT NULL,
  turn_index           INTEGER NOT NULL,
  model                TEXT NOT NULL,
  input_tokens         INTEGER NOT NULL,
  cache_read_tokens    INTEGER NOT NULL,
  cache_write_tokens   INTEGER NOT NULL,
  output_tokens        INTEGER NOT NULL,
  naive_input_tokens   INTEGER NOT NULL,
  lever_savings_json   TEXT NOT NULL,
  tool_calls           INTEGER NOT NULL,
  created_at           TEXT NOT NULL,
  UNIQUE (session_id, turn_index)
);
CREATE INDEX IF NOT EXISTS ix_context_budget_run ON context_budget(session_id);
