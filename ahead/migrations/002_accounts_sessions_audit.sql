-- Accounts that can be switched off, sessions that survive a page reload, sign-in limits and an audit log.

-- is_active = 0 blocks sign-in; raising token_version signs the person out on every device at once.
ALTER TABLE users ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1;
ALTER TABLE users ADD COLUMN token_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN created_at TEXT;
ALTER TABLE users ADD COLUMN last_login_at TEXT;
ALTER TABLE users ADD COLUMN created_by INTEGER;

-- One row per signed-in browser. Only a SHA-256 of the cookie token is stored, never the token itself.
-- A new sign-in first gets a one-time handoff code (60 s); /auth/session swaps it for the HttpOnly cookie.
CREATE TABLE sessions (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
  token_hash TEXT UNIQUE, handoff_hash TEXT UNIQUE, handoff_expires_at TEXT,
  token_version INTEGER NOT NULL, created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, revoked_at TEXT, user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX sessions_user ON sessions(user_id);

-- One-time links: "setup" (an admin created the account; the person picks their own password) and
-- "reset" (forgotten password). Only a SHA-256 of the link token is stored.
CREATE TABLE account_tokens (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), purpose TEXT NOT NULL,
  token_hash TEXT UNIQUE NOT NULL, created_by INTEGER, created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL, used_at TEXT
);
CREATE INDEX account_tokens_user ON account_tokens(user_id, purpose);

-- Rate limits: failed sign-ins, new registrations (and later emails) counted over a rolling window.
CREATE TABLE rate_events (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX rate_events_lookup ON rate_events(kind, subject, created_at);

-- Who did what to which account. Never holds health values.
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY, actor_id INTEGER, action TEXT NOT NULL, target_user_id INTEGER,
  details_json TEXT NOT NULL DEFAULT '{}', ip TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE INDEX audit_log_time ON audit_log(created_at);
CREATE INDEX audit_log_target ON audit_log(target_user_id, created_at);
