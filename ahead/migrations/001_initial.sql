-- AHEAD baseline schema: everything the app had before versioned migrations (8 Nov 2026).
-- Written in SQLite syntax; ahead/db.py adapts ids and REAL columns for PostgreSQL.

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
  role TEXT NOT NULL, name TEXT NOT NULL, institution TEXT NOT NULL DEFAULT '',
  preferences_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS records (
  id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id),
  cohort TEXT NOT NULL, disease TEXT NOT NULL, patient_id TEXT NOT NULL,
  source_row INTEGER NOT NULL, values_json TEXT NOT NULL, dob TEXT,
  issues_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'Incomplete',
  note TEXT NOT NULL DEFAULT '', model TEXT, score REAL, threshold REAL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL, source_name TEXT
);
CREATE INDEX IF NOT EXISTS records_owner ON records(owner_id, disease, cohort);
CREATE TABLE IF NOT EXISTS screenings (
  id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL REFERENCES users(id),
  disease TEXT NOT NULL, model TEXT NOT NULL, score REAL NOT NULL,
  prediction INTEGER NOT NULL, threshold REAL NOT NULL, created_at TEXT NOT NULL,
  values_json TEXT NOT NULL DEFAULT '{}', source TEXT NOT NULL DEFAULT 'legacy', previous_id INTEGER
);
CREATE INDEX IF NOT EXISTS screenings_owner ON screenings(owner_id, created_at);

CREATE TABLE IF NOT EXISTS care_access (
  patient_id INTEGER NOT NULL REFERENCES users(id), doctor_id INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL, ai_allowed INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(patient_id,doctor_id));
CREATE TABLE IF NOT EXISTS lifestyle (
  id INTEGER PRIMARY KEY, patient_id INTEGER NOT NULL REFERENCES users(id),
  values_json TEXT NOT NULL, scores_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS lifestyle_patient ON lifestyle(patient_id,created_at);
CREATE TABLE IF NOT EXISTS lifestyle_goals (
  patient_id INTEGER PRIMARY KEY REFERENCES users(id), goals_json TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS challenge_days (
  patient_id INTEGER NOT NULL REFERENCES users(id), day TEXT NOT NULL,minutes INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual', updated_at TEXT NOT NULL,PRIMARY KEY(patient_id,day));
CREATE TABLE IF NOT EXISTS food_entries (
  id INTEGER PRIMARY KEY,patient_id INTEGER NOT NULL REFERENCES users(id),
  foods_json TEXT NOT NULL,nutrients_json TEXT NOT NULL,source TEXT NOT NULL,
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS medication_plans (
  id INTEGER PRIMARY KEY,patient_id INTEGER NOT NULL REFERENCES users(id),
  doctor_id INTEGER NOT NULL REFERENCES users(id), name TEXT NOT NULL,dosage TEXT NOT NULL,
  instructions TEXT NOT NULL,times_json TEXT NOT NULL,days_json TEXT NOT NULL,
  start_date TEXT NOT NULL,end_date TEXT NOT NULL,notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS medication_patient ON medication_plans(patient_id,status);
CREATE TABLE IF NOT EXISTS medication_audit (
  id INTEGER PRIMARY KEY,plan_id INTEGER NOT NULL REFERENCES medication_plans(id),
  doctor_id INTEGER NOT NULL REFERENCES users(id),action TEXT NOT NULL,details_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dose_logs (
  id INTEGER PRIMARY KEY,plan_id INTEGER NOT NULL REFERENCES medication_plans(id),
  patient_id INTEGER NOT NULL REFERENCES users(id),scheduled_date TEXT NOT NULL,scheduled_time TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('taken','skipped')),
  logged_at TEXT NOT NULL, UNIQUE(plan_id,scheduled_date,scheduled_time));
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY,patient_id INTEGER NOT NULL REFERENCES users(id),type TEXT NOT NULL,
  title TEXT NOT NULL,description TEXT NOT NULL,action TEXT NOT NULL DEFAULT '',
  dedupe_key TEXT,created_at TEXT NOT NULL,read_at TEXT,
  UNIQUE(patient_id,dedupe_key));
CREATE TABLE IF NOT EXISTS activity_events (
  id INTEGER PRIMARY KEY,patient_id INTEGER NOT NULL REFERENCES users(id),type TEXT NOT NULL,
  created_at TEXT NOT NULL);
DROP INDEX IF EXISTS activity_patient;
CREATE INDEX IF NOT EXISTS activity_patient_type ON activity_events(patient_id,created_at,type);
CREATE TABLE IF NOT EXISTS record_predictions (
  id INTEGER PRIMARY KEY, record_id INTEGER NOT NULL REFERENCES records(id),
  owner_id INTEGER NOT NULL REFERENCES users(id),disease TEXT NOT NULL,
  values_json TEXT NOT NULL,score REAL NOT NULL,threshold REAL NOT NULL,
  model TEXT NOT NULL,source TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS record_predictions_owner ON record_predictions(owner_id,record_id,created_at);
-- screening_id has no foreign key: deleting a patient's screening history must not be blocked by a plan.
CREATE TABLE IF NOT EXISTS prevention_plans (
  id INTEGER PRIMARY KEY,patient_id INTEGER NOT NULL REFERENCES users(id),
  doctor_id INTEGER NOT NULL REFERENCES users(id),disease TEXT NOT NULL,screening_id INTEGER,
  targets_json TEXT NOT NULL,note TEXT NOT NULL DEFAULT '',recheck_date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','active','paused','stopped')),
  started_at TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS prevention_patient ON prevention_plans(patient_id,doctor_id,status);
CREATE TABLE IF NOT EXISTS prevention_audit (
  id INTEGER PRIMARY KEY,plan_id INTEGER NOT NULL REFERENCES prevention_plans(id),
  doctor_id INTEGER NOT NULL REFERENCES users(id),action TEXT NOT NULL,details_json TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS prevention_audit_plan ON prevention_audit(plan_id,id);
