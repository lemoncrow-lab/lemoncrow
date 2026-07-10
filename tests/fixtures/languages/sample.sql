CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    email TEXT NOT NULL,
    name TEXT
);

CREATE INDEX idx_users_email ON users(email);

CREATE VIEW active_users AS
SELECT * FROM users WHERE id > 0;
