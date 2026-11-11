"""Data retention (Phase 3b): what AHEAD deletes, and when.

* In-app notifications: deleted 90 days after they were created. "Time to screen again" notices are kept while
  their plan exists: they mark that the reminder was given, and deleting them would bring it back.
* The account audit log (sign-ins, admin actions, consent): deleted after one year. When an account is erased,
  its entries lose their network address and details, and a patient's lose the link to the account.
* Email records (the admin's Emails list): deleted 30 days after they were queued; the email text itself is
  already removed as soon as it is sent (ahead/mailer.py).
* Accounts switched off by an admin: erased one year after they were switched off, unless reactivated first.
  Patients are removed completely. Doctor and admin accounts lose everything personal (name, email, password,
  their own records and notices) but keep a renamed row, so the plans they wrote for patients still show a
  prescriber; their links to patients end and those plans stop, as when a patient removes a doctor.
* Sign-in sessions, rate-limit counters and used links are tidied by ahead/accounts.prune().

run() is called at start-up (at most once in 20 hours) and by the weekly task; the admin page can run it too.
"""

import json
import logging
import secrets
from datetime import datetime, timedelta, timezone

from ahead.config import ROLE_PATIENT
from ahead.storage import connection

log = logging.getLogger(__name__)

NOTICE_DAYS = 90
AUDIT_DAYS = 365
EMAIL_DAYS = 30
ACCOUNT_DAYS = 365
MIN_GAP = timedelta(hours=20)
STATE_KEY = "retention_last_run"


def _now():
    return datetime.now(timezone.utc)


def _stamp(moment):
    return moment.isoformat()


def erase_on(deactivated_at):
    """The day a switched-off account will be erased (a datetime), or None."""
    if not deactivated_at:
        return None
    moment = datetime.fromisoformat(deactivated_at)
    return (moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)) + timedelta(days=ACCOUNT_DAYS)


def last_run():
    with connection() as db:
        row = db.execute("SELECT value FROM app_state WHERE name=?", (STATE_KEY,)).fetchone()
    return json.loads(row["value"]) if row else None


def _save_run(result):
    with connection() as db:
        db.execute("INSERT INTO app_state(name,value,updated_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE "
                   "SET value=excluded.value,updated_at=excluded.updated_at", (STATE_KEY, json.dumps(result), result["at"]))


def run(force=False):
    """Apply the retention rules. Returns what was removed (or {"skipped": ...} when it ran recently)."""
    now = _now()
    previous = last_run()
    if not force and previous and now - datetime.fromisoformat(previous["at"]) < MIN_GAP:
        return {"skipped": previous["at"]}
    with connection() as db:
        cutoff = _stamp(now - timedelta(days=NOTICE_DAYS))
        notices = db.execute("DELETE FROM notifications WHERE created_at<? AND (dedupe_key IS NULL OR dedupe_key NOT LIKE ?)",
                             (cutoff, "prevention-recheck-%")).rowcount
        # "Time to screen again" notices stay while their plan is still running, then follow the 90-day rule.
        live = {str(r["id"]) for r in db.execute("SELECT id FROM prevention_plans WHERE status IN ('draft','active','paused')")}
        old_rechecks = [r["id"] for r in db.execute("SELECT id,dedupe_key FROM notifications WHERE created_at<? AND dedupe_key LIKE ?",
                                                     (cutoff, "prevention-recheck-%")).fetchall()
                        if r["dedupe_key"].split("-")[2] not in live]
        for notice_id in old_rechecks:
            notices += db.execute("DELETE FROM notifications WHERE id=?", (notice_id,)).rowcount
        audit_rows = db.execute("DELETE FROM audit_log WHERE created_at<?",
                                (_stamp(now - timedelta(days=AUDIT_DAYS)),)).rowcount
    from ahead import mailer
    emails = mailer.prune(EMAIL_DAYS)
    with connection() as db:
        due = [r["id"] for r in db.execute("SELECT id FROM users WHERE is_active=0 AND erased_at IS NULL AND deactivated_at<?",
                                           (_stamp(now - timedelta(days=ACCOUNT_DAYS)),)).fetchall()]
    erased = 0
    for user_id in due:
        try:
            erased += erase_account(user_id)
        except Exception:   # one account must not stop the rest; it is tried again next time
            log.exception("could not erase account %s", user_id)
    result = {"at": _stamp(now), "notices": max(notices, 0), "emails": emails, "accounts": erased,
              "audit": max(audit_rows, 0)}
    _save_run(result)
    if notices or emails or erased:
        log.info("retention: removed %s notices, %s email records, %s accounts", notices, emails, erased)
    return result


def erase_account(user_id):
    """Erase a switched-off account (see the module notes). Returns 1 if it was erased."""
    from ahead.accounts import audit
    from ahead.platform_data import doctors_for, linked_patients, revoke_doctor
    with connection() as db:
        row = db.execute("SELECT id,email,role,is_active,erased_at FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or row["is_active"] or row["erased_at"]:
        return 0
    email, role = row["email"], row["role"]
    # Links first, through the usual path: it stops the plans that depended on them and tells the patient.
    for patient in linked_patients(user_id):
        revoke_doctor(patient["id"], user_id)
    for doctor in doctors_for(user_id):
        revoke_doctor(user_id, doctor["id"])
    with connection() as db:
        # Doctors' notices about this person name them and their results ("<name> saved a ... screening").
        db.execute("DELETE FROM notifications WHERE dedupe_key LIKE ? OR dedupe_key IN "
                   "(SELECT 'review-' || CAST(id AS TEXT) FROM screenings WHERE owner_id=?)", (f"linked-{user_id}-%", user_id))
        db.execute("DELETE FROM screenings WHERE owner_id=?", (user_id,))
        db.execute("DELETE FROM activity_events WHERE patient_id=?", (user_id,))
        if role == ROLE_PATIENT:
            for table, column in (("dose_logs", "patient_id"), ("lifestyle", "patient_id"), ("lifestyle_goals", "patient_id"),
                                  ("challenge_days", "patient_id"), ("food_entries", "patient_id")):
                db.execute(f"DELETE FROM {table} WHERE {column}=?", (user_id,))
            db.execute("DELETE FROM medication_audit WHERE plan_id IN (SELECT id FROM medication_plans WHERE patient_id=?)", (user_id,))
            db.execute("DELETE FROM medication_plans WHERE patient_id=?", (user_id,))
            db.execute("DELETE FROM prevention_audit WHERE plan_id IN (SELECT id FROM prevention_plans WHERE patient_id=?)", (user_id,))
            db.execute("DELETE FROM prevention_plans WHERE patient_id=?", (user_id,))
        db.execute("DELETE FROM record_predictions WHERE owner_id=?", (user_id,))
        db.execute("DELETE FROM records WHERE owner_id=?", (user_id,))
        db.execute("DELETE FROM notifications WHERE patient_id=?", (user_id,))
        db.execute("DELETE FROM care_access WHERE patient_id=? OR doctor_id=?", (user_id, user_id))
        for table in ("calendar_feeds", "sessions", "account_tokens", "consents"):
            db.execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        db.execute("DELETE FROM email_outbox WHERE user_id=? OR to_email=?", (user_id, email))
        db.execute("DELETE FROM rate_events WHERE subject=? OR substr(subject,1,?)=?", (email, len(email) + 1, email + "|"))
        # The audit trail keeps that something happened, not from where; a removed patient's rows lose the link too.
        db.execute("UPDATE audit_log SET ip='',details_json='{}' WHERE actor_id=? OR target_user_id=?", (user_id, user_id))
        if role == ROLE_PATIENT:
            db.execute("UPDATE audit_log SET actor_id=NULL WHERE actor_id=?", (user_id,))
            db.execute("UPDATE audit_log SET target_user_id=NULL WHERE target_user_id=?", (user_id,))
        if role == ROLE_PATIENT:
            db.execute("DELETE FROM users WHERE id=?", (user_id,))
        else:
            # Kept, renamed, so plans they wrote still have a prescriber. The password can never match.
            db.execute("UPDATE users SET email=?,name='Removed account',institution='',password_hash=?,preferences_json='{}',"
                       "email_verified_at=NULL,last_login_at=NULL,token_version=token_version+1,erased_at=? WHERE id=?",
                       (f"erased-{user_id}@erased.invalid", "erased:" + secrets.token_hex(16), _stamp(_now()), user_id))
    # A removed patient's id can be given to a new account (SQLite), so their erasure row points at no one.
    audit("account_erased", target_user_id=None if role == ROLE_PATIENT else user_id, role=role)
    return 1
