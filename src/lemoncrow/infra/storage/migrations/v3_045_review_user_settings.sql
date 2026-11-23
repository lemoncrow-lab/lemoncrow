-- P5-4: per-user Review settings (SPEC 4.11). One row per reviewer holding only the keys the reviewer set;
-- everything else is the built-in default. Writer: PATCH /api/review-settings. Closer: a null value removes
-- one key, DELETE /api/review-settings removes the row.
CREATE TABLE IF NOT EXISTS review_user_settings (
  reviewer_id TEXT PRIMARY KEY,
  settings_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
