"""Emails: an outbox in the database and a background sender that uses Brevo's HTTPS API.

Why an outbox: a click never waits for (or fails because of) the email service. ``queue`` saves the email;
``drain`` (run by a background thread in server.py, and straight after each new email) sends what is due,
retries failures with growing gaps, and stops for the day before Brevo's free limit.

Settings (Render environment):
  BREVO_API_KEY            turns sending on. Without it, AHEAD behaves as before Phase 2: no emails,
                           sign-up needs no confirmation, and a forgotten password needs the AHEAD team.
  AHEAD_EMAIL_FROM         sender address, verified in Brevo (default aurak.events.noreply@gmail.com)
  AHEAD_EMAIL_FROM_NAME    sender name (default AHEAD)
  AHEAD_EMAIL_DAILY_LIMIT  emails per UTC day (default 250; Brevo's free plan allows 300)
  AHEAD_EMAIL_BACKEND      "brevo" (default when a key is set) or "memory" (tests: nothing leaves the computer)

Privacy rule for every email: no health values, no medicine names, no patient names. Emails say that
something happened and link to AHEAD, where the person signs in to see it.
"""

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html import escape

from ahead.storage import connection

log = logging.getLogger(__name__)

BREVO_URL = "https://api.brevo.com/v3/smtp/email"
RETRY_GAPS = [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=30), timedelta(hours=2), timedelta(hours=6)]
MAX_ATTEMPTS = len(RETRY_GAPS) + 1
PER_RECIPIENT_HOUR = 6     # at most this many emails to one address per hour...
PER_RECIPIENT_DAY = 20     # ...and per day, whatever asks for them
ACCOUNT_RESERVE = 50       # the last 50 of the daily budget are kept for account emails (confirm, reset)
PRIORITY_ACCOUNT = 0       # sign-up confirmation, password reset, set-up: sent first, may use the reserve
PRIORITY_NOTICE = 1        # everything else
STUCK_AFTER = timedelta(minutes=10)
# The optional notices (ahead/alerts.py). One still waiting when its account is switched off is not sent;
# account emails (including "your account was switched off") always are.
OPTIONAL_KINDS = {"plan_updated", "doctor_review", "doctor_review_more", "doctor_linked", "weekly"}

sent_messages = []         # the "memory" backend keeps sent emails here (tests)
_wake = threading.Event()
_drain_lock = threading.Lock()
_thread = None


def _now():
    return datetime.now(timezone.utc)


def _stamp(moment=None):
    return (moment or _now()).isoformat()


def backend():
    chosen = os.environ.get("AHEAD_EMAIL_BACKEND", "").strip().lower()
    if chosen in ("memory", "brevo"):
        return chosen
    return "brevo" if os.environ.get("BREVO_API_KEY", "").strip() else ""


def enabled():
    """True when emails are really sent (or captured, in tests)."""
    return bool(backend())


def daily_limit():
    try:
        return max(int(os.environ.get("AHEAD_EMAIL_DAILY_LIMIT", "250")), 0)
    except ValueError:
        return 250


def sender():
    return (os.environ.get("AHEAD_EMAIL_FROM", "").strip() or "aurak.events.noreply@gmail.com",
            os.environ.get("AHEAD_EMAIL_FROM_NAME", "").strip() or "AHEAD")


# =============================================================================
# WRITING EMAILS
# =============================================================================

def _html(heading, paragraphs, button=None, footer=""):
    parts = "".join(f'<p style="margin:0 0 14px;line-height:1.55">{escape(p)}</p>' for p in paragraphs)
    if button:
        label, url = button
        parts += (f'<p style="margin:22px 0"><a href="{escape(url, quote=True)}" style="background:#0E9AA7;color:#ffffff;'
                  f'text-decoration:none;padding:12px 20px;border-radius:8px;font-weight:600;display:inline-block">'
                  f'{escape(label)}</a></p><p style="margin:0 0 14px;font-size:13px;color:#667085;line-height:1.5">'
                  f'If the button doesn\'t work, copy this link into your browser:<br>'
                  f'<span style="word-break:break-all">{escape(url)}</span></p>')
    if footer:
        parts += f'<p style="margin:18px 0 0;font-size:13px;color:#667085;line-height:1.5">{escape(footer)}</p>'
    return f"""<!doctype html><html><body style="margin:0;background:#F4F7FB;font-family:Arial,Helvetica,sans-serif;color:#344054">
<div style="max-width:560px;margin:0 auto;padding:28px 18px">
<div style="font-size:20px;font-weight:700;color:#0E9AA7;margin-bottom:18px">AHEAD</div>
<div style="background:#ffffff;border:1px solid #E1E8EF;border-radius:12px;padding:26px 24px">
<h1 style="font-size:20px;margin:0 0 16px;color:#101828">{escape(heading)}</h1>{parts}</div>
<p style="font-size:12px;color:#98A2B3;margin:16px 4px;line-height:1.5">AHEAD is a university Senior Design prototype and
not a medical device. This is an automatic email; replies are not read.</p></div></body></html>"""


def _text(heading, paragraphs, button=None, footer=""):
    lines = [heading, ""] + [p for paragraph in paragraphs for p in (paragraph, "")]
    if button:
        lines += [f"{button[0]}: {button[1]}", ""]
    if footer:
        lines += [footer, ""]
    lines += ["AHEAD is a university Senior Design prototype and not a medical device.",
              "This is an automatic email; replies are not read."]
    return "\n".join(lines)


OPT_OUT = "You can turn these emails off in AHEAD under Settings → Preferences → Emails."


def compose(kind, name="", link="", site="", detail=None):
    """(subject, text, html) for one kind of email. No health data, medicine names or patient names in any of them.

    detail: the one non-health fact a notice needs (which kind of plan changed; how many updates are waiting)."""
    # No names, not even the reader's own: an inbox preview would pair a person's name with "your doctor updated
    # your plan" (name is still accepted, so callers stay the same).
    hello = "Hello,"
    not_you = "If this wasn't you, you can ignore this email."
    if kind == "verify":
        subject, heading = "Confirm your AHEAD email", "Confirm your email address"
        body = [hello, "Thanks for creating an AHEAD account. Confirm your email address to finish signing up. "
                       "The link works once and expires in 24 hours."]
        button, footer = ("Confirm my email", link), not_you
    elif kind == "reset":
        subject, heading = "Reset your AHEAD password", "Choose a new password"
        body = [hello, "Someone asked to reset the password for this AHEAD account. "
                       "The link works once and expires in 60 minutes."]
        button, footer = ("Choose a new password", link), "If you didn't ask for this, ignore this email: your password stays the same."
    elif kind == "setup":
        subject, heading = "Your AHEAD account is ready", "Set up your AHEAD account"
        body = [hello, "An AHEAD administrator created an account for you. Choose your own password to start. "
                       "The link works once and expires in 3 days."]
        button, footer = ("Set my password", link), "If you weren't expecting this, ignore this email or tell the AHEAD team."
    elif kind == "password_changed":
        subject, heading = "Your AHEAD password was changed", "Your password was changed"
        body = [hello, "The password for your AHEAD account was just changed, and your other devices were signed out."]
        button, footer = ("Open AHEAD", site) if site else None, "If you didn't do this, reset your password from the sign-in page now and tell the AHEAD team."
    elif kind == "email_changed":
        subject, heading = "Your AHEAD email address was changed", "Your email address was changed"
        body = [hello, "The email address on your AHEAD account was changed, so AHEAD emails will now go to the new address."]
        button, footer = None, "If you didn't do this, contact the AHEAD team."
    elif kind == "email_change":
        subject, heading = "Confirm your new AHEAD email", "Confirm your new email address"
        body = [hello, "Someone asked to use this address for an AHEAD account. Confirm it to make the change: until "
                       "then, AHEAD keeps using the old address. The link works once and expires in 24 hours."]
        button, footer = ("Confirm this address", link), "If you didn't ask for this, ignore this email: nothing changes."
    elif kind == "signup_exists":
        subject, heading = "Someone tried to register with your email", "You already have an AHEAD account"
        body = [hello, "Someone tried to create a new AHEAD account with this email address, which already has one. "
                       "Nothing was changed. If it was you, sign in instead, or reset your password from the sign-in page."]
        button, footer = ("Sign in", site) if site else None, "If it wasn't you, you can ignore this email."
    elif kind == "deactivated":
        subject, heading = "Your AHEAD account was switched off", "Your account was switched off"
        body = [hello, "An AHEAD administrator switched off your account, so you can't sign in for now. "
                       "Your saved information has not been deleted. If the account stays switched off "
                       "for a year, it and its saved information are deleted."]
        button, footer = None, "If you think this is a mistake, contact the AHEAD team."
    elif kind == "reactivated":
        subject, heading = "Your AHEAD account is active again", "Your account is active again"
        body = [hello, "An AHEAD administrator switched your account back on. You can sign in again."]
        button, footer = ("Sign in", site) if site else None, ""
    elif kind == "plan_updated":
        plan = "prevention plan" if detail == "prevention" else "medication plan"
        subject, heading = f"Your doctor updated your {plan}", f"Your {plan} was updated"
        body = [hello, f"Your doctor made a change to your {plan} in AHEAD. Sign in to see what changed."]
        button, footer = ("Open AHEAD", site) if site else None, OPT_OUT
    elif kind == "doctor_review":
        subject, heading = "A linked patient has a new result to review", "New result to review"
        saved = ("saved a screening result that AHEAD marked for attention" if detail == "attention"
                 else "saved a new screening result")
        body = [hello, f"A patient who linked their AHEAD account to you {saved}. "
                       "Sign in to see it under Advanced Analytics & Patient Intelligence."]
        button, footer = ("Open AHEAD", site) if site else None, OPT_OUT
    elif kind == "doctor_review_more":
        subject, heading = "More results to review in AHEAD", "More results to review"
        body = [hello, "Linked patients saved more screening results since AHEAD last emailed you. "
                       "Sign in to see them under Advanced Analytics & Patient Intelligence."]
        button, footer = ("Open AHEAD", site) if site else None, OPT_OUT
    elif kind == "doctor_linked":
        subject, heading = "A patient added you as their doctor", "A patient added you as their doctor"
        body = [hello, "A patient added your AHEAD account to their care team, so you can now see the results they "
                       "choose to save. Sign in to see who it is."]
        button, footer = ("Open AHEAD", site) if site else None, OPT_OUT
    elif kind == "weekly":
        count = int(detail or 0)
        updates = f"{count} update{'s' if count != 1 else ''}"
        subject, heading = f"You have {updates} in AHEAD", "Your week in AHEAD"
        body = [hello, f"You have {updates} waiting in AHEAD from the last 7 days. Sign in to read them."]
        button, footer = ("Open AHEAD", site) if site else None, OPT_OUT
    else:
        raise ValueError(f"unknown email kind {kind}")
    return subject, _text(heading, body, button, footer), _html(heading, body, button, footer)


# =============================================================================
# THE OUTBOX
# =============================================================================

def queue(to_email, kind, user_id=None, name="", link="", site="", priority=None, detail=None, send_at=None):
    """Save an email for sending. Returns its outbox id, or None when emails are off or the recipient's cap is reached.

    send_at: a later time (UTC datetime) to send it, for a catch-up email; default now."""
    to_email = (to_email or "").strip().lower()
    if not enabled() or "@" not in to_email:
        return None
    if priority is None:
        priority = PRIORITY_ACCOUNT if kind in ("verify", "reset", "setup", "email_change") else PRIORITY_NOTICE
    now = _now()
    subject, text, html = compose(kind, name, link, site, detail)
    with connection() as db:
        hour = db.execute("SELECT COUNT(*) FROM email_outbox WHERE to_email=? AND created_at>=?",
                          (to_email, _stamp(now - timedelta(hours=1)))).fetchone()[0]
        day = db.execute("SELECT COUNT(*) FROM email_outbox WHERE to_email=? AND created_at>=?",
                         (to_email, _stamp(now - timedelta(days=1)))).fetchone()[0]
        if hour >= PER_RECIPIENT_HOUR or day >= PER_RECIPIENT_DAY:
            log.warning("email to one recipient skipped: cap reached (%s)", kind)
            return None
        cursor = db.execute("""INSERT INTO email_outbox(user_id,to_email,kind,subject,body_text,body_html,priority,next_attempt_at,created_at)
                               VALUES(?,?,?,?,?,?,?,?,?)""",
                            (user_id, to_email, kind, subject, text, html, priority, _stamp(max(send_at or now, now)), _stamp(now)))
        email_id = cursor.lastrowid
    if backend() == "memory":
        drain()                      # tests: send at once, no thread
    else:
        start_background_sender()    # normally already running (server.py); covers `streamlit run app.py`
        _wake.set()
    return email_id


def recently_queued(user_id, kind, within):
    """True when an email of this kind went into the outbox for this person within the given timedelta."""
    with connection() as db:
        return db.execute("SELECT 1 FROM email_outbox WHERE user_id=? AND kind=? AND created_at>=? LIMIT 1",
                          (user_id, kind, _stamp(_now() - within))).fetchone() is not None


def last_email(user_id, kinds, within):
    """When the newest email of these kinds to this person went out (or was queued, if it hasn't gone yet), within
    the given timedelta; None if there was none. Scheduled-but-unsent emails are left out (see scheduled)."""
    marks = ",".join("?" * len(kinds))
    since = _stamp(_now() - within)
    with connection() as db:
        row = db.execute(f"SELECT MAX(COALESCE(sent_at,created_at)) FROM email_outbox WHERE user_id=? AND kind IN ({marks}) "
                         "AND status!='cancelled' AND NOT (status='pending' AND next_attempt_at>created_at AND attempts=0) "
                         "AND COALESCE(sent_at,created_at)>=?", (user_id, *kinds, since)).fetchone()
    return datetime.fromisoformat(row[0]) if row and row[0] else None


def scheduled(user_id, kind):
    """True when an email of this kind is waiting to go to this person later (a catch-up email)."""
    with connection() as db:
        return db.execute("SELECT 1 FROM email_outbox WHERE user_id=? AND kind=? AND status='pending' AND attempts=0 "
                          "AND next_attempt_at>created_at LIMIT 1", (user_id, kind)).fetchone() is not None


def cancel_scheduled(user_id, kinds):
    """Drop emails of these kinds still waiting for this person (they switched the email off). Returns how many."""
    marks = ",".join("?" * len(kinds))
    with connection() as db:
        return db.execute(f"UPDATE email_outbox SET status='cancelled',body_text='',body_html='' WHERE user_id=? "
                          f"AND kind IN ({marks}) AND status='pending'", (user_id, *kinds)).rowcount


def sent_today():
    start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    with connection() as db:
        return db.execute("SELECT COUNT(*) FROM email_outbox WHERE status='sent' AND sent_at>=?", (_stamp(start),)).fetchone()[0]


def _deliver(row):
    """Send one email. Returns (ok, provider_id or error, retry_makes_sense)."""
    chosen = backend()
    if chosen == "memory":
        sent_messages.append({"to": row["to_email"], "kind": row["kind"], "subject": row["subject"],
                              "text": row["body_text"], "html": row["body_html"]})
        return True, f"memory-{len(sent_messages)}", False
    if chosen != "brevo":
        return False, "email sending is not configured", False
    address, name = sender()
    payload = {"sender": {"email": address, "name": name}, "to": [{"email": row["to_email"]}],
               "subject": row["subject"], "htmlContent": row["body_html"], "textContent": row["body_text"],
               "tags": [f"ahead-{row['kind']}"]}
    request = urllib.request.Request(BREVO_URL, data=json.dumps(payload).encode(), method="POST", headers={
        "api-key": os.environ.get("BREVO_API_KEY", "").strip(), "accept": "application/json",
        "content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read() or b"{}")
        return True, str(body.get("messageId", ""))[:200], False
    except urllib.error.HTTPError as error:
        detail = error.read()[:300].decode("utf-8", "replace")
        retry = error.code == 429 or error.code >= 500  # 4xx (bad key, bad sender, bad address) won't fix itself
        return False, f"HTTP {error.code}: {detail}", retry
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return False, f"network: {error}"[:300], True


def _active(user_id):
    with connection() as db:
        row = db.execute("SELECT is_active FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row and row["is_active"])


def drain(limit=20):
    """Send the emails that are due, within today's budget. Returns how many were sent."""
    if not enabled() or not _drain_lock.acquire(blocking=False):
        return 0
    try:
        now = _now()
        with connection() as db:  # a crash mid-send leaves 'sending' rows: try them again later
            db.execute("UPDATE email_outbox SET status='pending' WHERE status='sending' AND next_attempt_at<?",
                       (_stamp(now - STUCK_AFTER),))
            due = db.execute("SELECT * FROM email_outbox WHERE status='pending' AND next_attempt_at<=? "
                             "ORDER BY priority, id LIMIT ?", (_stamp(now), int(limit))).fetchall()
        sent, budget = 0, daily_limit() - sent_today()
        for row in due:
            allowed = budget if row["priority"] == PRIORITY_ACCOUNT else budget - ACCOUNT_RESERVE
            if allowed <= 0:
                continue  # waits for tomorrow's budget
            if row["kind"] in OPTIONAL_KINDS and row["user_id"] and not _active(row["user_id"]):
                with connection() as db:  # a notice queued earlier (a catch-up) for an account switched off since
                    db.execute("UPDATE email_outbox SET status='cancelled',body_text='',body_html='',last_error=? "
                               "WHERE id=? AND status='pending'", ("account switched off", row["id"]))
                continue
            with connection() as db:
                if not db.execute("UPDATE email_outbox SET status='sending',next_attempt_at=? WHERE id=? AND status='pending'",
                                  (_stamp(), row["id"])).rowcount:
                    continue  # another sender took it
            ok, detail, retry = _deliver(row)
            attempts = row["attempts"] + 1
            with connection() as db:
                if ok:
                    db.execute("UPDATE email_outbox SET status='sent',attempts=?,sent_at=?,provider_id=?,last_error='',"
                               "body_text='',body_html='' WHERE id=?", (attempts, _stamp(), detail, row["id"]))
                elif retry and attempts < MAX_ATTEMPTS:
                    db.execute("UPDATE email_outbox SET status='pending',attempts=?,last_error=?,next_attempt_at=? WHERE id=?",
                               (attempts, detail, _stamp(_now() + RETRY_GAPS[attempts - 1]), row["id"]))
                else:
                    db.execute("UPDATE email_outbox SET status='failed',attempts=?,last_error=?,body_text='',body_html='' "
                               "WHERE id=?", (attempts, detail, row["id"]))
            if ok:
                sent += 1
                budget -= 1
            else:
                log.warning("email %s (%s) not sent: %s", row["id"], row["kind"], detail)
        return sent
    finally:
        _drain_lock.release()


def recent(limit=100):
    """The outbox for the admin page: who, what kind and whether it went. Never the body."""
    with connection() as db:
        return [dict(r) for r in db.execute(
            "SELECT id,to_email,kind,status,attempts,last_error,created_at,sent_at,next_attempt_at FROM email_outbox ORDER BY id DESC LIMIT ?",
            (int(limit),)).fetchall()]


def prune(days=30):
    """Delete the records of finished emails older than `days` (ahead/retention.py). Returns how many."""
    with connection() as db:
        return max(db.execute("DELETE FROM email_outbox WHERE status IN ('sent','failed','cancelled') AND created_at<?",
                              (_stamp(_now() - timedelta(days=days)),)).rowcount, 0)


# =============================================================================
# BACKGROUND SENDER
# =============================================================================

def _loop():
    while True:
        _wake.wait(timeout=30)
        _wake.clear()
        try:
            drain()
        except Exception:
            log.exception("email sender failed; will try again")


def start_background_sender():
    """Start the sender thread once per process (server.py calls this)."""
    global _thread
    if _thread is None and enabled():
        _thread = threading.Thread(target=_loop, name="ahead-email", daemon=True)
        _thread.start()
        _wake.set()
