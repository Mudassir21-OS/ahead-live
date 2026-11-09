-- Phase 2a: sign-in tied to one browser tab, verified email addresses and an email outbox.

-- Each sign-in belongs to one browser tab. The tab keeps a random secret in sessionStorage (which the
-- browser forgets when the tab closes); only its SHA-256 is stored here. A reload sends the secret again;
-- a new tab or a reopened link has no secret, so it gets the sign-in page. Sessions from before this
-- change have no tab_hash and can't be resumed.
ALTER TABLE sessions ADD COLUMN tab_hash TEXT;

-- NULL until the person opens the link in their confirmation email (or sets a password from an emailed
-- link, which proves the same thing). Every account that exists before this change counts as confirmed.
ALTER TABLE users ADD COLUMN email_verified_at TEXT;
UPDATE users SET email_verified_at = COALESCE(created_at, '2026-11-09T00:00:00+00:00');

-- Emails wait here and a background sender delivers them (Brevo's HTTPS API), with retries, a daily
-- budget and per-recipient caps. Emails never hold health values. The body is blanked once the email is
-- sent or has failed for good, so one-time links don't stay in the database.
CREATE TABLE email_outbox (
  id INTEGER PRIMARY KEY, user_id INTEGER, to_email TEXT NOT NULL, kind TEXT NOT NULL,
  subject TEXT NOT NULL, body_text TEXT NOT NULL, body_html TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', priority INTEGER NOT NULL DEFAULT 1,
  attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at TEXT NOT NULL, last_error TEXT NOT NULL DEFAULT '',
  provider_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, sent_at TEXT
);
CREATE INDEX email_outbox_due ON email_outbox(status, next_attempt_at);
CREATE INDEX email_outbox_recipient ON email_outbox(to_email, created_at);
