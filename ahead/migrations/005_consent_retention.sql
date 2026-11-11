-- Phase 3b: patient consent records, account retention dates and a little app state.
-- (Health values are encrypted by the app itself, ahead/fieldcrypt.py: no column changes are needed for that.)

-- One row each time a patient agrees to how AHEAD uses their information; withdrawn_at is set if they withdraw.
-- version names the consent text they saw (ahead/consent.py); a new text asks everyone again.
CREATE TABLE consents (
  id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), version TEXT NOT NULL,
  given_at TEXT NOT NULL, withdrawn_at TEXT, ip TEXT NOT NULL DEFAULT ''
);
CREATE INDEX consents_user ON consents(user_id, id);

-- Retention: an account switched off for a year is erased (ahead/retention.py). deactivated_at starts that year;
-- erased_at marks an account whose personal data has been removed (doctor and admin rows stay, renamed, so the
-- plans they wrote keep a prescriber).
ALTER TABLE users ADD COLUMN deactivated_at TEXT;
ALTER TABLE users ADD COLUMN erased_at TEXT;
UPDATE users SET deactivated_at = (SELECT MAX(a.created_at) FROM audit_log a
                                   WHERE a.target_user_id = users.id AND a.action = 'account_deactivated')
 WHERE is_active = 0;
UPDATE users SET deactivated_at = '2026-11-12T00:00:00+00:00' WHERE is_active = 0 AND deactivated_at IS NULL;

CREATE INDEX notifications_created ON notifications(created_at);

-- Small named values, e.g. when the retention clean-up last ran.
CREATE TABLE app_state (name TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
