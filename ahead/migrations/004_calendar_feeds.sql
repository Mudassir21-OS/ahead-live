-- Phase 2b: medication reminders in the patient's own calendar.

-- One private calendar link per patient (webcal://<site>/calendar/<token>.ics). Calendar apps fetch it again
-- every few hours, so it follows the doctor's changes to the plan. The token is kept as it is (not hashed) so
-- the patient can copy the same link again on another device, like a Google Calendar "secret address"; it
-- only gives read access to dose times, and a new link (Settings) stops the old one at once.
-- show_names: 0 shows events as "AHEAD medicine reminder"; 1 shows the medicine name and dose (the patient's
-- choice, because a calendar is often shared or visible on a lock screen).
CREATE TABLE calendar_feeds (
  user_id INTEGER PRIMARY KEY REFERENCES users(id),
  token TEXT UNIQUE NOT NULL,
  show_names INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  last_fetched_at TEXT
);

-- The notifications table was written for patients, but its patient_id is simply the person the notice is
-- for: from Phase 2b doctors receive notices there too (a linked patient's new result, a new link).
