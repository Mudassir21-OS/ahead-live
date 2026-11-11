"""Patient consent to how AHEAD uses their information (Phase 3b).

A patient agrees when they create their account, and anyone without a current agreement (accounts from before
3b, or after withdrawing) sees the consent page after signing in before anything else. Each agreement is a row
in ``consents`` with the text version, the time and the network address; withdrawing sets ``withdrawn_at``.
Changing ``VERSION`` asks every patient again on their next sign-in.
"""

from datetime import datetime, timezone

from ahead.storage import connection

VERSION = "2026-11b"   # b: Gemini free-tier use and review, re-check reminders, AI summaries on withdrawal

TITLE = "How AHEAD uses your information"
POINTS = [
    ("What AHEAD is", "A university Senior Design prototype for early-awareness screening. It is not a medical device: "
     "results are estimates, not diagnoses, and must not be used to start, stop or change any treatment."),
    ("Test data only", "This version is for testing. Please don't enter real personal health information: AHEAD is "
     "not yet approved for real patients and its servers are outside the UAE (Frankfurt, Germany)."),
    ("What is saved", "Your account details and what you enter or receive in AHEAD: screening answers and results, "
     "lifestyle check-ins, food logs, goals, and medicine or prevention plans your doctors add. Health values are "
     "stored encrypted."),
    ("Who can see it", "You, and the doctors you add to your care team (remove them at any time in Settings). "
     "AHEAD admins manage accounts only and cannot see health information."),
    ("AI features", "The assistant, result explanations, food photo estimates and doctor AI summaries you allow send "
     "the information they need to Google's Gemini service to produce an answer. This version uses Gemini's free "
     "tier, where Google may use what is sent to improve its services and people at Google may read it. Using "
     "these features is optional."),
    ("Emails", "Emails never contain results, medicine names or other health details. They only say that something "
     "changed and link to AHEAD."),
    ("How long it is kept", "Notifications are deleted after 90 days (reminders to screen again stay while their plan runs) and email records after 30 days. If your account "
     "is switched off, everything in it is erased after one year. You can delete your screening history in "
     "Settings at any time."),
    ("Your choice", "You can withdraw this agreement in Settings → Preferences → Data & privacy. AHEAD then asks again "
     "before you can use it and switches off doctors' AI summaries; what's already saved stays until you delete it "
     "or the account is erased."),
]


def _now():
    return datetime.now(timezone.utc).isoformat()


def latest(user_id):
    """The patient's most recent consent row (current or not), or None."""
    with connection() as db:
        row = db.execute("SELECT id,version,given_at,withdrawn_at FROM consents WHERE user_id=? ORDER BY id DESC LIMIT 1",
                         (user_id,)).fetchone()
    return dict(row) if row else None


def is_current(row):
    return bool(row) and row["withdrawn_at"] is None and row["version"] == VERSION


def has_consent(user_id):
    return is_current(latest(user_id))


def give(user_id, ip=""):
    """Record the patient's agreement to the current text (nothing new if it is already current)."""
    from ahead.accounts import audit
    with connection() as db:
        row = db.execute("SELECT version,withdrawn_at FROM consents WHERE user_id=? ORDER BY id DESC LIMIT 1",
                         (user_id,)).fetchone()
        if row and is_current(row):
            return False
        db.execute("INSERT INTO consents(user_id,version,given_at,ip) VALUES(?,?,?,?)", (user_id, VERSION, _now(), ip or ""))
    audit("consent_given", actor_id=user_id, target_user_id=user_id, ip=ip, version=VERSION)
    return True


def withdraw(user_id, ip=""):
    """Withdraw the current agreement. True if there was one."""
    from ahead.accounts import audit
    with connection() as db:
        changed = db.execute("UPDATE consents SET withdrawn_at=? WHERE user_id=? AND withdrawn_at IS NULL",
                             (_now(), user_id)).rowcount
        # Sending data to Gemini rests on the agreement: withdrawing switches off every doctor's AI summaries,
        # and agreeing again doesn't switch them back on by itself.
        db.execute("UPDATE care_access SET ai_allowed=0 WHERE patient_id=?", (user_id,))
    if changed:
        audit("consent_withdrawn", actor_id=user_id, target_user_id=user_id, ip=ip)
    return bool(changed)


def status_by_user():
    """{user_id: (state, when)} for the admin's account list; state is 'given', 'withdrawn' or 'old version'."""
    with connection() as db:
        rows = db.execute("SELECT c.user_id,c.version,c.given_at,c.withdrawn_at FROM consents c "
                          "WHERE c.id=(SELECT MAX(id) FROM consents d WHERE d.user_id=c.user_id)").fetchall()
    result = {}
    for row in rows:
        if row["withdrawn_at"]:
            result[row["user_id"]] = ("withdrawn", row["withdrawn_at"])
        elif row["version"] != VERSION:
            result[row["user_id"]] = ("old version", row["given_at"])
        else:
            result[row["user_id"]] = ("given", row["given_at"])
    return result
