"""Small local prototype store. Set AHEAD_DB_PATH to a durable, protected volume."""

import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timezone

from ahead import db as database
from ahead.config import ROLE_ADMIN, ROLE_DOCTOR, ROLE_PATIENT

connection = database.connection
log = logging.getLogger(__name__)
ROLES = (ROLE_PATIENT, ROLE_DOCTOR, ROLE_ADMIN)
MIN_PASSWORD = 12


def _db_path():
    return database.sqlite_path()


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 600_000)
    return f"{salt.hex()}:{digest.hex()}"


def verify_password(password, encoded):
    try:
        salt, digest = encoded.split(":", 1)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 600_000)
        return hmac.compare_digest(actual, bytes.fromhex(digest))
    except (ValueError, AttributeError):
        return False


# Spent on unknown emails too, so a failed sign-in takes as long whether or not the account exists.
_DUMMY_HASH = password_hash(secrets.token_hex(16))


def _columns(db, table):
    return {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}


def _legacy_sqlite_upgrade(db, run_001):
    """Bring a local SQLite file from before versioned migrations up to the 001 schema."""
    run_001()  # creates any table that is missing
    for table, column, definition in (
            ("users", "preferences_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("screenings", "values_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("screenings", "source", "TEXT NOT NULL DEFAULT 'legacy'"),
            ("screenings", "previous_id", "INTEGER"),
            ("records", "source_name", "TEXT"),
            ("care_access", "ai_allowed", "INTEGER NOT NULL DEFAULT 0")):
        if column not in _columns(db, table):
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


_bootstrapped = set()


def _sqlite_repairs():
    """One-time repairs of local SQLite records saved by earlier versions; user_version marks them done,
    so this is a single cheap check on later start-ups. (The PostgreSQL database never held such records.)"""
    with connection() as db:
        if db.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return
        # Values once blocked by the exact training range ("...; verify it.") are rechecked against the wider bounds.
        legacy = db.execute("SELECT id,disease,status,values_json,issues_json FROM records WHERE issues_json LIKE ?",
                            ("%is outside the training range (%; verify it.%",)).fetchall()
        if legacy:
            _recheck_old_ranges(db, legacy)
        # Uploads were once saved without a status, so valid unscored rows were left 'Incomplete'.
        db.execute("UPDATE records SET status='Ready' WHERE status='Incomplete' AND issues_json='[]' AND score IS NULL")
        db.execute("PRAGMA user_version=1")


def init_db():
    """Apply migrations, then create the accounts named in the environment (once per database per process)."""
    database.migrate(legacy_upgrade=_legacy_sqlite_upgrade)
    if not database.is_postgres():
        _sqlite_repairs()
    key = database.target()
    if key in _bootstrapped:
        return
    _bootstrapped.add(key)
    # Demo accounts are for a local classroom run only; they are refused on PostgreSQL (the live site).
    if os.environ.get("AHEAD_ENABLE_DEMO_ACCOUNTS") == "1":
        if database.is_postgres():
            log.warning("AHEAD_ENABLE_DEMO_ACCOUNTS is ignored on PostgreSQL: no demo accounts on the live site.")
        else:
            create_user("user@ahead.demo", "user123", ROLE_PATIENT, "Sarah Ahmed", demo=True)
            create_user("doctor@ahead.demo", "doctor123", ROLE_DOCTOR, "Dr. Sarah Ahmed", "AHEAD Medical Center", demo=True)
            create_user("admin@ahead.demo", "admin123", ROLE_ADMIN, "AHEAD Admin", demo=True)
    for prefix, role, name in (("AHEAD_ADMIN", ROLE_ADMIN, "AHEAD Admin"), ("AHEAD_DOCTOR", ROLE_DOCTOR, "Doctor")):
        email, password = os.environ.get(f"{prefix}_EMAIL"), os.environ.get(f"{prefix}_PASSWORD")
        if email and password:
            try:
                create_user(email, password, role, name)
            except ValueError:
                # A misconfigured secret must not take the whole site down (including sign-in).
                log.warning("%s_PASSWORD is shorter than %d characters; that account was not created.", prefix, MIN_PASSWORD)


def _recheck_old_ranges(db, rows):
    # Imported here: only a database saved before the wider bounds needs the model statistics at start-up.
    from ahead.clinical_data import validate_row
    from ahead.resources import feature_summary
    # Datasets load before the first UPDATE, so no write lock is held (blocking other sessions) while they are read.
    summaries, updates = {disease: feature_summary(disease) for disease in {row["disease"] for row in rows}}, []
    for row in rows:
        values, stats, issues = json.loads(row["values_json"]), summaries[row["disease"]], []
        for issue in json.loads(row["issues_json"]):
            feature = issue.split(":", 1)[0]
            old = " is outside the training range (" in issue and issue.endswith("; verify it.") and feature in stats
            issues += validate_row(values, row["disease"], [feature], stats)[1] if old else [issue]
        status = "Ready" if not issues and row["status"] == "Incomplete" else row["status"]
        updates.append((json.dumps(issues), status, row["id"]))
    db.executemany("UPDATE records SET issues_json=?,status=? WHERE id=?", updates)


def has_doctor_account():
    with connection() as db:
        return db.execute("SELECT 1 FROM users WHERE role=? LIMIT 1", (ROLE_DOCTOR,)).fetchone() is not None


def create_user(email, password, role, name, institution="", demo=False, created_by=None, verified=True):
    """Create an account. False if the email is taken; ValueError for a short password or an unknown role.

    verified=False for a patient who signed up while emails are on (confirmed by the emailed link) and for
    staff added by an admin (confirmed when they set their password from the emailed set-up link)."""
    if role not in ROLES or (len(password) < MIN_PASSWORD and not demo):
        raise ValueError(f"Use a password of at least {MIN_PASSWORD} characters.")
    email = email.strip().lower()
    with connection() as db:
        if db.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
            return False
    try:
        with connection() as db:
            now = datetime.now(timezone.utc).isoformat()
            db.execute("INSERT INTO users(email,password_hash,role,name,institution,created_at,created_by,email_verified_at) "
                       "VALUES(?,?,?,?,?,?,?,?)", (email, password_hash(password), role, name.strip(), institution.strip(),
                                                   now, created_by, now if verified else None))
    except database.IntegrityError:
        return False
    return True


def user_view(row):
    """The signed-in user as the pages see it (no password hash)."""
    result = {k: row[k] for k in ("id", "email", "role", "name", "institution")}
    result["preferences"] = json.loads(row["preferences_json"] or "{}")
    return result


def authenticate(email, password, role):
    """The account for these details, or None. role may be one role or a tuple of roles.
    Deactivated accounts never sign in."""
    roles = (role,) if isinstance(role, str) else tuple(role)
    with connection() as db:
        row = db.execute("SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),)).fetchone()
    if not row:
        verify_password(password or "", _DUMMY_HASH)
        return None
    if not verify_password(password or "", row["password_hash"]) or row["role"] not in roles or not row["is_active"]:
        return None
    return user_view(row)


def get_user(user_id):
    with connection() as db:
        row = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    return user_view(row) if row else None


def update_preferences(user_id, preferences):
    """Save preference groups ("toggles", "appearance", "email", ...). Groups not given are kept."""
    with connection() as db:
        row = db.execute("SELECT preferences_json FROM users WHERE id=?", (user_id,)).fetchone()
        merged = {**json.loads((row["preferences_json"] if row else None) or "{}"), **preferences}
        db.execute("UPDATE users SET preferences_json=? WHERE id=?", (json.dumps(merged), user_id))


def update_profile(user_id, name, email, institution):
    """Save the profile. Returns True when the email address changed."""
    email = email.strip().lower()
    with connection() as db:
        old = db.execute("SELECT email FROM users WHERE id=?", (user_id,)).fetchone()
        db.execute("UPDATE users SET name=?,email=?,institution=? WHERE id=?",
                   (name.strip(), email, institution.strip(), user_id))
    return bool(old) and old["email"] != email


def change_password(user_id, current, new):
    with connection() as db:
        row = db.execute("SELECT password_hash FROM users WHERE id=?", (user_id,)).fetchone()
        if not row or not verify_password(current, row["password_hash"]):
            return False
        # A new password signs every other browser out (their sessions carry the old token_version).
        db.execute("UPDATE users SET password_hash=?,token_version=token_version+1 WHERE id=?", (password_hash(new), user_id))
    return True


def insert_records(owner_id, cohort, disease, rows, source_name=None):
    now = datetime.now(timezone.utc).isoformat()
    with connection() as db:
        for index, row in enumerate(rows, 1):
            db.execute("""INSERT INTO records(owner_id,cohort,disease,patient_id,source_row,values_json,dob,issues_json,status,created_at,updated_at,source_name)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                       (owner_id, cohort, disease, row["patient_id"], index, json.dumps(row["values"]),
                        row.get("dob"), json.dumps(row["issues"]), "Incomplete" if row["issues"] else "Ready", now, now,
                        source_name))


def list_records(owner_id, disease=None):
    query = "SELECT * FROM records WHERE owner_id=?"
    args = [owner_id]
    if disease:
        query += " AND disease=?"
        args.append(disease)
    query += " ORDER BY updated_at DESC, id DESC"
    with connection() as db:
        return [dict(r) for r in db.execute(query, args).fetchall()]


def update_record(owner_id, record_id, **fields):
    allowed = {"values_json", "dob", "issues_json", "status", "note", "model", "score", "threshold"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    assignments = ",".join(f"{key}=?" for key in fields)
    with connection() as db:
        db.execute(f"UPDATE records SET {assignments} WHERE id=? AND owner_id=?",
                   [*fields.values(), record_id, owner_id])


def save_scoring_run(owner_id, disease, model, threshold, scored, invalid=(), source="batch"):
    """One transaction for a cohort run: scored (id, values, score) rows get a score and a history entry;
    invalid (id, values, issues) rows failed the recheck and go back to 'Incomplete'."""
    now = datetime.now(timezone.utc).isoformat()
    where = "WHERE id=? AND owner_id=? AND disease=?"
    with connection() as db:
        db.executemany(f"UPDATE records SET values_json=?,issues_json=?,status='Incomplete',score=NULL,model=NULL,threshold=NULL,updated_at=? {where}",
                       [(json.dumps(values), json.dumps(issues), now, record_id, owner_id, disease)
                        for record_id, values, issues in invalid])
        db.executemany(f"UPDATE records SET values_json=?,issues_json='[]',status=?,score=?,model=?,threshold=?,updated_at=? {where}",
                       [(json.dumps(values), "Flagged" if score >= threshold else "Ready", score, model, threshold, now,
                         record_id, owner_id, disease) for record_id, values, score in scored])
        # INSERT ... SELECT keeps the ownership check of append_record_prediction without a query per row.
        db.executemany(f"""INSERT INTO record_predictions(record_id,owner_id,disease,values_json,score,threshold,model,source,created_at)
                           SELECT id,owner_id,disease,?,?,?,?,?,? FROM records {where}""",
                       [(json.dumps(values), score, threshold, model, source, now, record_id, owner_id, disease)
                        for record_id, values, score in scored])


def delete_records(owner_id):
    with connection() as db:
        db.execute("DELETE FROM record_predictions WHERE owner_id=?", (owner_id,))
        db.execute("DELETE FROM records WHERE owner_id=?", (owner_id,))


def save_screening(owner_id, disease, model, score, prediction, threshold, values=None, source="patient", previous_id=None):
    with connection() as db:
        cursor = db.execute("""INSERT INTO screenings(owner_id,disease,model,score,prediction,threshold,created_at,values_json,source,previous_id)
                               VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (owner_id, disease, model, score, prediction, threshold,
                             datetime.now(timezone.utc).isoformat(), json.dumps(values or {}), source, previous_id))
        return cursor.lastrowid


def load_screenings(owner_id):
    with connection() as db:
        return [dict(r) for r in db.execute(
            "SELECT * FROM screenings WHERE owner_id=? ORDER BY created_at DESC,id DESC LIMIT 500", (owner_id,)).fetchall()]


def delete_screenings(owner_id):
    with connection() as db:
        db.execute("DELETE FROM screenings WHERE owner_id=?", (owner_id,))
