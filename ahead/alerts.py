"""
ahead/alerts.py
===============
Who hears about what (Phase 2b): in-app notices for doctors, and the optional emails.

Emails follow the mailer's privacy rule: never a health value, a medicine name or a patient name. They say
that something happened and link to AHEAD. Each person can switch every one of these emails off in
Settings → Preferences → Emails. In-app notices are always on: they stay inside the signed-in app.

What is NOT emailed, by decision: per-dose reminders (the calendar does that), high-risk results to the
patient themselves, and food-tracker nudges.
"""

import json
import logging
from datetime import timedelta

from ahead import mailer
from ahead.storage import connection, update_preferences

log = logging.getLogger(__name__)

# The optional emails: key -> (who gets it, title, description). Defaults below.
EMAILS = {
    "plan_updates": ("Patient", "Plan changes",
                     "When your doctor changes your medication or prevention plan (at most one email an hour)."),
    "doctor_reviews": ("Doctor", "Results to review",
                       "When a linked patient saves a result AHEAD marks for attention (at most one email an hour)."),
    "doctor_links": ("Doctor", "New linked patients", "When a patient links their AHEAD account to you."),
    "weekly_summary": ("Both", "Weekly summary", "Once a week, how many unread updates are waiting. Off unless you turn it on."),
}
DEFAULTS = {"plan_updates": True, "doctor_reviews": True, "doctor_links": True, "weekly_summary": False}
EVERY_HOUR = timedelta(hours=1)
WEEK_GAP = timedelta(days=6)   # the weekly email is never sent twice in one week, whatever calls it


def emails_for(role):
    """The optional emails a person in this role can choose, in display order."""
    if role not in ("Patient", "Doctor"):
        return []    # admins see no health activity, so there is nothing to tell them about
    return [key for key, (who, _, _) in EMAILS.items() if who in (role, "Both")]


def email_prefs(user_id):
    with connection() as db:
        row = db.execute("SELECT preferences_json FROM users WHERE id=?", (user_id,)).fetchone()
    saved = json.loads((row["preferences_json"] if row else None) or "{}").get("email", {})
    return {key: bool(saved.get(key, default)) for key, default in DEFAULTS.items()}


def set_email_pref(user_id, key, on):
    if key not in DEFAULTS:
        raise ValueError(key)
    update_preferences(user_id, {"email": {**email_prefs(user_id), key: bool(on)}})


def _person(db, user_id):
    row = db.execute("SELECT id,email,name,role,is_active FROM users WHERE id=?", (user_id,)).fetchone()
    return dict(row) if row else None


def _email(user_id, kind, pref, detail=None, every=None):
    """Queue one optional email if emails are on, the person wants it and none of this kind went out recently."""
    if not mailer.enabled():
        return None
    try:
        with connection() as db:
            person = _person(db, user_id)
        if not person or not person["is_active"] or not email_prefs(user_id).get(pref):
            return None
        if every and mailer.recently_queued(user_id, kind, every):
            return None
        from ahead.accounts import site_url
        return mailer.queue(person["email"], kind, user_id, person["name"], site=site_url(), detail=detail)
    except Exception:   # an email must never break the action that triggered it
        log.exception("could not queue the %s email", kind)
        return None


# =============================================================================
# EVENTS
# =============================================================================

def plan_changed(patient_id, plan_kind):
    """A doctor created, edited, paused, resumed or stopped a medication or prevention plan.
    (The in-app notice is written by platform_data, with the details; the email says only that something changed.)"""
    return _email(patient_id, "plan_updated", "plan_updates",
                  detail="prevention" if plan_kind == "prevention" else "medication", every=EVERY_HOUR)


def doctor_linked(patient_id, doctor_id):
    """A patient added this doctor to their care team."""
    from ahead.platform_data import notify, now
    with connection() as db:
        patient = _person(db, patient_id)
    name = patient["name"] if patient else "A patient"
    notify(doctor_id, "care_team", "New linked patient",
           f"{name} added you to their care team. Their saved results now appear in Advanced Analytics.",
           "intelligence", f"linked-{patient_id}-{now()}")
    return _email(doctor_id, "doctor_linked", "doctor_links")


def new_result(patient_id, screening_id, disease, values, score, threshold):
    """A patient saved a screening. When AHEAD marks it for attention (urgent or at risk), each linked doctor
    gets an in-app notice (with the patient's name, inside AHEAD) and, if they want it, an email (without it)."""
    from ahead.config import DISEASES
    from ahead.platform_data import doctors_for, notify
    from ahead.triage import track_text, triage
    try:
        result = triage(disease, values or {}, score, threshold)
    except Exception:
        log.exception("could not sort a new result for the doctor notice")
        return []
    if result["track"] not in ("urgent", "at_risk"):
        return []
    with connection() as db:
        patient = _person(db, patient_id)
    label = track_text(result, "doctor")["label"]
    told = []
    for doctor in doctors_for(patient_id):
        notify(doctor["id"], "review", "New result to review",
               f"{patient['name'] if patient else 'A linked patient'} saved a {DISEASES[disease]['label']} screening: {label}.",
               "intelligence", f"review-{screening_id}")
        _email(doctor["id"], "doctor_review", "doctor_reviews", every=EVERY_HOUR)
        told.append(doctor["id"])
    return told


# =============================================================================
# WEEKLY SUMMARY (POST /tasks/weekly, called by a GitHub Actions schedule)
# =============================================================================

def weekly_summaries():
    """Email everyone who turned the weekly summary on and has unread updates from the last 7 days.
    Safe to call more than once: a person gets at most one weekly email in 6 days. Returns counts."""
    from ahead.platform_data import now
    from datetime import datetime
    if not mailer.enabled():
        return {"emails": "off", "queued": 0, "checked": 0}
    since = (datetime.fromisoformat(now()) - timedelta(days=7)).isoformat()
    with connection() as db:
        people = [dict(r) for r in db.execute(
            "SELECT id,preferences_json FROM users WHERE is_active=1 AND role IN ('Patient','Doctor')").fetchall()]
    queued = checked = 0
    for person in people:
        wants = json.loads(person["preferences_json"] or "{}").get("email", {}).get("weekly_summary", DEFAULTS["weekly_summary"])
        if not wants:
            continue
        checked += 1
        with connection() as db:
            unread = db.execute("SELECT COUNT(*) FROM notifications WHERE patient_id=? AND read_at IS NULL AND created_at>=?",
                                (person["id"], since)).fetchone()[0]
        if unread and _email(person["id"], "weekly", "weekly_summary", detail=unread, every=WEEK_GAP):
            queued += 1
    return {"emails": "on", "queued": queued, "checked": checked}
