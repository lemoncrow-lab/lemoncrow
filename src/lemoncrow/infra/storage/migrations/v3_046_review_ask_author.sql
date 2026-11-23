-- P9-4 (SPEC 7.1 P7-1 F): who asked. A second reviewer who has not answered yet is shown none of the first reviewer's
-- Ask history, so each conversation and message records its author. Rows from before this are unauthored ('') and are
-- hidden from such a reader (fail closed). Writer: the Ask routes. Closer: the Review's own deletion (child rows).
ALTER TABLE review_ask_conversations ADD COLUMN created_by TEXT NOT NULL DEFAULT '';
ALTER TABLE review_ask_messages ADD COLUMN created_by TEXT NOT NULL DEFAULT '';
