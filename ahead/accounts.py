"""Sign-in sessions, sign-in limits, one-time account links and the audit log.

How a sign-in belongs to one browser tab
---------------------------------------
Reloading the page keeps you signed in; closing the tab signs you out; a new tab, or typing the link
again, shows the sign-in page. (AHEAD holds health data, so a shared or family computer must not stay
signed in after the tab is closed.)

* Every tab has its own identity in ``sessionStorage``: a public id and a random secret
  (``ahead/session.py``). The browser keeps sessionStorage through reloads and forgets it when the tab
  closes; a new tab starts with an empty one.
* The sign-in form checks the password and calls ``start_session`` with the tab's secret. The session
  row keeps only SHA-256 hashes, and a one-time handoff code (valid 60 seconds) is returned.
* A small script POSTs the code and the tab id to ``/auth/session`` (``server.py``). That makes the real
  session token and sets it as an HttpOnly, SameSite=Strict cookie named after the tab
  (``ahead_s_<tab id>``), so one tab signing in or out never touches another tab's cookie.
* On a reload, ``session_user`` needs both: the tab's cookie and the tab's secret. A closed tab's cookie
  is useless without the secret, which went with the tab.
* Every rerun re-checks the session: not revoked, not expired, not idle too long, account still active
  and its ``token_version`` unchanged. Deactivating an account or changing its password raises
  ``token_version`` and so signs that person out everywhere at once.
"""

import hashlib
import hmac
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from ahead import db as database
from ahead.config import ROLE_ADMIN, ROLE_DOCTOR, ROLE_PATIENT
from ahead.storage import MIN_PASSWORD, connection, password_hash, user_view

log = logging.getLogger(__name__)

COOKIE_PREFIX = "ahead_s_"        # + the tab id: one cookie per signed-in tab
LEGACY_COOKIE = "ahead_session"    # Phase 1's single cookie; cleared when seen
TAB_ID_LENGTH = 16                 # hex characters
HANDOFF_SECONDS = 60
# A medical record should not stay open on a shared computer: sign out after 2 hours without activity,
# and after 12 hours in any case (the cookie's own lifetime).
IDLE_MINUTES = int(os.environ.get("AHEAD_SESSION_IDLE_MINUTES", "120"))
MAX_HOURS = int(os.environ.get("AHEAD_SESSION_MAX_HOURS", "12"))
SEEN_EVERY = timedelta(minutes=5)  # how often last_seen_at is written (not on every click)

SETUP_DAYS = 3
RESET_MINUTES = 60
VERIFY_HOURS = 24
LINK_LIFE = {"setup": timedelta(days=SETUP_DAYS), "reset": timedelta(minutes=RESET_MINUTES),
             "verify": timedelta(hours=VERIFY_HOURS)}

# Sign-in limits. Only failures count, by email + address and by address alone, so a class sharing
# campus Wi-Fi is not blocked by one person's typos.
FAIL_WINDOW = timedelta(minutes=15)
FAILS_PER_ACCOUNT = 5
FAILS_PER_ADDRESS = 50
SIGNUP_WINDOW = timedelta(hours=1)
SIGNUPS_PER_ADDRESS = 5
# Self-service emails: a reset request or a new confirmation link, per email address and per network.
EMAIL_WINDOW = timedelta(hours=1)
EMAILS_PER_ACCOUNT = 3
EMAILS_PER_ADDRESS = 10


def _now():
    return datetime.now(timezone.utc)


def _stamp(moment=None):
    return (moment or _now()).isoformat()


def _parse(stamp):
    moment = datetime.fromisoformat(stamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def valid_tab_id(tab_id):
    return isinstance(tab_id, str) and len(tab_id) == TAB_ID_LENGTH and all(c in "0123456789abcdef" for c in tab_id)


def valid_tab_secret(secret):
    return isinstance(secret, str) and 32 <= len(secret) <= 100


def cookie_name(tab_id):
    return COOKIE_PREFIX + tab_id


# =============================================================================
# CLIENT ADDRESS
# =============================================================================

def client_ip(headers=None, direct=None):
    """The visitor's address for rate limits and the audit log.

    Behind Render (and Cloudflare in front of it) the socket address is a proxy. AHEAD_TRUST_PROXY=N
    trusts N proxy hops, like Express's "trust proxy": the address N entries from the right of
    X-Forwarded-For. With 0 (the default, for local runs) the header is ignored, so it cannot be faked.
    """
    headers = {k.lower(): v for k, v in (headers or {}).items()}
    hops = int(os.environ.get("AHEAD_TRUST_PROXY", "0") or 0)
    if hops > 0:
        chain = [part.strip() for part in headers.get("x-forwarded-for", "").split(",") if part.strip()]
        if chain:
            return chain[max(len(chain) - hops, 0)][:64]
    return (direct or "")[:64]


# =============================================================================
# AUDIT LOG
# =============================================================================

def audit(action, actor_id=None, target_user_id=None, ip="", **details):
    """Record an account event. Never pass health values here."""
    try:
        with connection() as db:
            db.execute("INSERT INTO audit_log(actor_id,action,target_user_id,details_json,ip,created_at) VALUES(?,?,?,?,?,?)",
                       (actor_id, action, target_user_id, json.dumps(details, default=str), ip or "", _stamp()))
    except Exception:  # an audit failure must never block the action itself
        log.exception("audit log write failed for %s", action)


def audit_entries(limit=200, action=None):
    query = """SELECT a.*, actor.name actor_name, actor.email actor_email, target.name target_name, target.email target_email
               FROM audit_log a LEFT JOIN users actor ON actor.id=a.actor_id LEFT JOIN users target ON target.id=a.target_user_id"""
    args = []
    if action:
        query += " WHERE a.action=?"
        args.append(action)
    query += " ORDER BY a.created_at DESC, a.id DESC LIMIT ?"
    args.append(int(limit))
    with connection() as db:
        return [dict(r) for r in db.execute(query, args).fetchall()]


# =============================================================================
# RATE LIMITS
# =============================================================================

def _count(db, kind, subject, since):
    return db.execute("SELECT COUNT(*) FROM rate_events WHERE kind=? AND subject=? AND created_at>=?",
                      (kind, subject, _stamp(since))).fetchone()[0]


def _add_event(db, kind, subject):
    db.execute("INSERT INTO rate_events(kind,subject,created_at) VALUES(?,?,?)", (kind, subject, _stamp()))


def sign_in_blocked(email, ip):
    """True when this account (from this address) or this address has failed too often lately."""
    since = _now() - FAIL_WINDOW
    with connection() as db:
        return (_count(db, "login_fail", f"{(email or '').strip().lower()}|{ip}", since) >= FAILS_PER_ACCOUNT
                or (bool(ip) and _count(db, "login_fail_ip", ip, since) >= FAILS_PER_ADDRESS))


def record_failed_sign_in(email, ip):
    email = (email or "").strip().lower()
    with connection() as db:
        _add_event(db, "login_fail", f"{email}|{ip}")
        if ip:
            _add_event(db, "login_fail_ip", ip)
        blocked_now = _count(db, "login_fail", f"{email}|{ip}", _now() - FAIL_WINDOW) == FAILS_PER_ACCOUNT
    if blocked_now:
        # Logged once per burst, with the email partly hidden.
        name, _, domain = email.partition("@")
        audit("sign_in_blocked", ip=ip, email=f"{name[:2]}***@{domain}")


def sign_up_allowed(ip):
    if not ip:
        return True
    with connection() as db:
        return _count(db, "signup", ip, _now() - SIGNUP_WINDOW) < SIGNUPS_PER_ADDRESS


def record_sign_up(ip):
    if ip:
        with connection() as db:
            _add_event(db, "signup", ip)


def prune(days=30):
    """Drop old rate-limit rows, dead sessions and old outbox rows (called at start-up)."""
    cutoff = _stamp(_now() - timedelta(days=days))
    with connection() as db:
        db.execute("DELETE FROM rate_events WHERE created_at<?", (_stamp(_now() - timedelta(days=2)),))
        db.execute("DELETE FROM sessions WHERE expires_at<? OR (revoked_at IS NOT NULL AND revoked_at<?)", (cutoff, cutoff))
    from ahead import mailer
    mailer.prune(days)


# =============================================================================
# SESSIONS
# =============================================================================

def start_session(user_id, user_agent="", ip="", tab_secret=None):
    """After a correct password: a session row for this tab and the one-time code that turns into its cookie.

    Without a tab secret (no sessionStorage, or automated tests) the session works in this tab until it
    closes or reloads, but can never be resumed from a cookie.
    """
    code = secrets.token_urlsafe(32)
    now = _now()
    tab_hash = _hash(tab_secret) if valid_tab_secret(tab_secret) else None
    with connection() as db:
        version = db.execute("SELECT token_version FROM users WHERE id=?", (user_id,)).fetchone()[0]
        cursor = db.execute("""INSERT INTO sessions(user_id,handoff_hash,handoff_expires_at,token_version,created_at,last_seen_at,expires_at,user_agent,tab_hash)
                               VALUES(?,?,?,?,?,?,?,?,?)""",
                            (user_id, _hash(code), _stamp(now + timedelta(seconds=HANDOFF_SECONDS)), version, _stamp(now),
                             _stamp(now), _stamp(now + timedelta(hours=MAX_HOURS)), (user_agent or "")[:200], tab_hash))
        session_id = cursor.lastrowid
        db.execute("UPDATE users SET last_login_at=? WHERE id=?", (_stamp(now), user_id))
    audit("sign_in", actor_id=user_id, target_user_id=user_id, ip=ip)
    return session_id, code


def complete_handoff(code):
    """Swap a handoff code for the session token (once). Returns (token, max_age_seconds) or None."""
    if not code or len(code) > 200:
        return None
    token = secrets.token_urlsafe(32)
    now = _now()
    with connection() as db:
        row = db.execute("SELECT id,expires_at FROM sessions WHERE handoff_hash=? AND token_hash IS NULL AND revoked_at IS NULL "
                         "AND handoff_expires_at>=?", (_hash(code), _stamp(now))).fetchone()
        if not row:
            return None
        claimed = db.execute("UPDATE sessions SET token_hash=?,handoff_hash=NULL,handoff_expires_at=NULL "
                             "WHERE id=? AND token_hash IS NULL", (_hash(token), row["id"])).rowcount
        if not claimed:
            return None
    return token, max(int((_parse(row["expires_at"]) - now).total_seconds()), 0)


def _valid(row, now):
    if not row or row["revoked_at"] or not row["is_active"] or row["session_version"] != row["token_version"]:
        return False
    if _parse(row["expires_at"]) <= now:
        return False
    return _parse(row["last_seen_at"]) + timedelta(minutes=IDLE_MINUTES) > now


_SESSION_QUERY = """SELECT s.id session_id, s.token_version session_version, s.revoked_at, s.expires_at, s.last_seen_at,
                           s.tab_hash, u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE {}"""


def _touch(db, row, now):
    if _parse(row["last_seen_at"]) + SEEN_EVERY <= now:
        db.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (_stamp(now), row["session_id"]))


def session_user(token, tab_secret=None):
    """(session_id, user) for a tab's cookie token and that tab's secret.

    None when the token is unknown, expired, idle or revoked, or the secret isn't the one the session was
    opened with (another tab, or a tab that was closed and reopened).
    """
    if not isinstance(token, str) or not token or len(token) > 200 or not valid_tab_secret(tab_secret):
        return None
    now = _now()
    with connection() as db:
        row = db.execute(_SESSION_QUERY.format("s.token_hash=?"), (_hash(token),)).fetchone()
        if not _valid(row, now) or not row["tab_hash"] or not hmac.compare_digest(row["tab_hash"], _hash(tab_secret)):
            return None
        _touch(db, row, now)
    return row["session_id"], user_view(row)


def check_session(session_id):
    """The current user for an open browser tab, re-read on every rerun; None once signed out."""
    if not session_id:
        return None
    now = _now()
    with connection() as db:
        row = db.execute(_SESSION_QUERY.format("s.id=?"), (session_id,)).fetchone()
        if not _valid(row, now):
            return None
        _touch(db, row, now)
    return user_view(row)


def end_session(session_id=None, token=None):
    """Sign one browser out."""
    with connection() as db:
        if session_id:
            db.execute("UPDATE sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL", (_stamp(), session_id))
        if token:
            db.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL", (_stamp(), _hash(token)))


def token_is_live(token):
    """True while a cookie token still belongs to a usable session (used to tidy away dead tab cookies)."""
    if not isinstance(token, str) or not token or len(token) > 200:
        return False
    with connection() as db:
        row = db.execute(_SESSION_QUERY.format("s.token_hash=?"), (_hash(token),)).fetchone()
    return _valid(row, _now())


def sign_out_everywhere(user_id):
    with connection() as db:
        db.execute("UPDATE users SET token_version=token_version+1 WHERE id=?", (user_id,))
        db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (_stamp(), user_id))


def keep_session_after_password_change(session_id, user_id):
    """Changing the password signs other browsers out; this browser stays signed in."""
    with connection() as db:
        db.execute("UPDATE sessions SET token_version=(SELECT token_version FROM users WHERE id=?) WHERE id=? AND user_id=?",
                   (user_id, session_id, user_id))
        db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND id<>? AND revoked_at IS NULL",
                   (_stamp(), user_id, session_id))


# =============================================================================
# ONE-TIME LINKS (account set-up, password reset)
# =============================================================================

def issue_link(user_id, purpose, created_by=None):
    """A new one-time token for 'setup', 'reset' or 'verify'; earlier unused ones of that kind stop working."""
    if purpose not in LINK_LIFE:
        raise ValueError(purpose)
    token = secrets.token_urlsafe(32)
    now = _now()
    life = LINK_LIFE[purpose]
    with connection() as db:
        db.execute("UPDATE account_tokens SET used_at=? WHERE user_id=? AND purpose=? AND used_at IS NULL",
                   (_stamp(now), user_id, purpose))
        db.execute("INSERT INTO account_tokens(user_id,purpose,token_hash,created_by,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                   (user_id, purpose, _hash(token), created_by, _stamp(now), _stamp(now + life)))
    return token


def link_account(token):
    """The account a still-valid set-up or reset link belongs to: {'purpose', 'user'} or None."""
    if not token or len(token) > 200:
        return None
    with connection() as db:
        row = db.execute("""SELECT t.purpose, u.* FROM account_tokens t JOIN users u ON u.id=t.user_id
                            WHERE t.token_hash=? AND t.used_at IS NULL AND t.expires_at>=? AND t.purpose IN ('setup','reset')""",
                         (_hash(token), _stamp())).fetchone()
    if not row or not row["is_active"]:
        return None
    return {"purpose": row["purpose"], "user": user_view(row)}


def use_link(token, new_password, ip=""):
    """Set the password from a link (once). Signs the account out everywhere. Returns the user or None."""
    if len(new_password or "") < MIN_PASSWORD:
        raise ValueError(f"Use a password of at least {MIN_PASSWORD} characters.")
    now = _stamp()
    with connection() as db:
        row = db.execute("SELECT id,user_id,purpose FROM account_tokens WHERE token_hash=? AND used_at IS NULL AND expires_at>=? "
                         "AND purpose IN ('setup','reset')", (_hash(token or ""), now)).fetchone()
        if not row or not db.execute("UPDATE account_tokens SET used_at=? WHERE id=? AND used_at IS NULL",
                                     (now, row["id"])).rowcount:
            return None
        active = db.execute("SELECT is_active FROM users WHERE id=?", (row["user_id"],)).fetchone()
        if not active or not active["is_active"]:
            return None
        # The link arrived at this address, so the address is confirmed too.
        db.execute("UPDATE users SET password_hash=?,token_version=token_version+1,email_verified_at=COALESCE(email_verified_at,?) "
                   "WHERE id=?", (password_hash(new_password), now, row["user_id"]))
        db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, row["user_id"]))
    audit("password_set_from_link", actor_id=row["user_id"], target_user_id=row["user_id"], ip=ip, purpose=row["purpose"])
    with connection() as db:
        user = user_view(db.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone())
    if row["purpose"] == "reset":
        notify(user, "password_changed")
    return user


# =============================================================================
# EMAIL CONFIRMATION AND SELF-SERVICE RESET
# =============================================================================

def is_verified(user_id):
    with connection() as db:
        row = db.execute("SELECT email_verified_at FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row and row["email_verified_at"])


def mark_verified(user_id):
    with connection() as db:
        db.execute("UPDATE users SET email_verified_at=COALESCE(email_verified_at,?) WHERE id=?", (_stamp(), user_id))


def needs_verification(user_id):
    """Sign-in waits for a confirmed email only while emails are switched on (otherwise nobody could confirm)."""
    from ahead import mailer
    return mailer.enabled() and not is_verified(user_id)


def send_verification(user_id, ip=""):
    """Email a new confirmation link. False when the limit for this account or network is reached."""
    from ahead import mailer
    with connection() as db:
        row = db.execute("SELECT id,email,name,is_active,email_verified_at FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not row["is_active"] or row["email_verified_at"] or not mailer.enabled():
        return False
    if not _email_allowed(f"verify|{user_id}", ip):
        return False
    token = issue_link(user_id, "verify")
    return mailer.queue(row["email"], "verify", user_id, row["name"], link=f"{site_url()}/?verify={token}") is not None


def verify_email(token, ip=""):
    """Open a confirmation link (once). Returns the user, or None for an unknown, used or expired link."""
    if not token or len(token) > 200:
        return None
    now = _stamp()
    with connection() as db:
        row = db.execute("SELECT id,user_id FROM account_tokens WHERE token_hash=? AND purpose='verify' AND used_at IS NULL "
                         "AND expires_at>=?", (_hash(token), now)).fetchone()
        if not row or not db.execute("UPDATE account_tokens SET used_at=? WHERE id=? AND used_at IS NULL",
                                     (now, row["id"])).rowcount:
            return None
        user = db.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone()
        if not user or not user["is_active"]:
            return None
        db.execute("UPDATE users SET email_verified_at=COALESCE(email_verified_at,?) WHERE id=?", (now, row["user_id"]))
    audit("email_verified", actor_id=row["user_id"], target_user_id=row["user_id"], ip=ip)
    return user_view(user)


def _email_allowed(subject, ip):
    """Limits for emails a visitor can trigger: 3 an hour per account, 10 an hour per network."""
    since = _now() - EMAIL_WINDOW
    with connection() as db:
        if _count(db, "email_request", subject, since) >= EMAILS_PER_ACCOUNT:
            return False
        if ip and _count(db, "email_request_ip", ip, since) >= EMAILS_PER_ADDRESS:
            return False
        _add_event(db, "email_request", subject)
        if ip:
            _add_event(db, "email_request_ip", ip)
    return True


def request_password_reset(email, ip=""):
    """'Forgot password' on the sign-in page. The visitor always sees the same answer, whether or not the
    account exists, so the form can't be used to find out who has an AHEAD account."""
    from ahead import mailer
    email = (email or "").strip().lower()
    if not mailer.enabled() or "@" not in email or len(email) > 254:
        return
    with connection() as db:
        row = db.execute("SELECT id,email,name,is_active FROM users WHERE email=?", (email,)).fetchone()
    if not row or not row["is_active"]:
        _email_allowed(f"reset|{email}", ip)  # count it anyway: same limits for unknown addresses
        return
    if not _email_allowed(f"reset|{email}", ip):
        return
    token = issue_link(row["id"], "reset")
    mailer.queue(row["email"], "reset", row["id"], row["name"], link=f"{site_url()}/?reset={token}")
    audit("reset_requested", actor_id=row["id"], target_user_id=row["id"], ip=ip)


def notify(user, kind):
    """A security notice (password changed, account switched off or on). Silently nothing when emails are off."""
    from ahead import mailer
    try:
        mailer.queue(user["email"], kind, user["id"], user.get("name", ""), site=site_url())
    except Exception:  # a notice must never block the action it reports
        log.exception("could not queue the %s email", kind)


def site_url_for(request):
    """site_url() for a plain HTTP route (no Streamlit page): AHEAD_PUBLIC_URL, else the request's own address."""
    configured = os.environ.get("AHEAD_PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        return configured
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    return f"{proto}://{request.headers.get('host', request.url.netloc)}"


def site_url():
    """The public address used in emailed links (AHEAD_PUBLIC_URL; the browser's address as a fallback)."""
    configured = os.environ.get("AHEAD_PUBLIC_URL", "").strip().rstrip("/")
    if configured:
        return configured
    try:
        from urllib.parse import urlsplit
        import streamlit as st
        parts = urlsplit(st.context.url)
        return f"{parts.scheme}://{parts.netloc}"
    except Exception:
        return ""


# =============================================================================
# ADMIN: ACCOUNT MANAGEMENT (no health data)
# =============================================================================

def list_accounts(role=None):
    """Account details only: no screenings, records or plans."""
    query = ("SELECT id,email,role,name,institution,is_active,created_at,last_login_at FROM users"
             + (" WHERE role=?" if role else "") + " ORDER BY role, name, id")
    with connection() as db:
        return [dict(r) for r in db.execute(query, (role,) if role else ()).fetchall()]


def _active_admins(db):
    return db.execute("SELECT COUNT(*) FROM users WHERE role=? AND is_active=1", (ROLE_ADMIN,)).fetchone()[0]


def create_staff_account(admin_id, email, name, role, institution="", ip=""):
    """An admin adds a doctor (or another admin). Returns the one-time set-up token, or raises ValueError.

    The account gets a random password nobody knows; the person chooses their own through the set-up link.
    """
    from ahead.storage import create_user  # local import: storage owns user creation
    if role not in (ROLE_DOCTOR, ROLE_ADMIN):
        raise ValueError("Admins add doctor and admin accounts; patients register themselves.")
    email = (email or "").strip().lower()
    if "@" not in email or "." not in email.split("@")[-1] or not (name or "").strip():
        raise ValueError("Enter a name and a valid email address.")
    if not create_user(email, secrets.token_urlsafe(24), role, name, institution, created_by=admin_id, verified=False):
        raise ValueError("An account with that email already exists.")
    with connection() as db:
        user_id = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    audit("account_created", actor_id=admin_id, target_user_id=user_id, ip=ip, role=role)
    token = issue_link(user_id, "setup", admin_id)
    from ahead import mailer
    mailer.queue(email, "setup", user_id, name.strip(), link=f"{site_url()}/?setup={token}")
    return user_id, token


def set_active(admin_id, user_id, active, ip=""):
    """Deactivate (signs out everywhere at once) or reactivate an account."""
    if admin_id == user_id:
        raise ValueError("You can't change your own account here.")
    with connection() as db:
        if is_postgres_lock_needed():
            db.execute("SELECT pg_advisory_xact_lock(4207002)")  # two admins at once: only one wins the last-admin check
        row = db.execute("SELECT role,is_active FROM users WHERE id=?", (user_id,)).fetchone()
        if not row:
            raise ValueError("Account not found.")
        if not active and row["role"] == ROLE_ADMIN and row["is_active"] and _active_admins(db) <= 1:
            raise ValueError("This is the last active admin, so it can't be deactivated.")
        db.execute("UPDATE users SET is_active=?,token_version=token_version+? WHERE id=?",
                   (1 if active else 0, 0 if active else 1, user_id))
        if not active:
            db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (_stamp(), user_id))
            db.execute("UPDATE account_tokens SET used_at=? WHERE user_id=? AND used_at IS NULL", (_stamp(), user_id))
    audit("account_reactivated" if active else "account_deactivated", actor_id=admin_id, target_user_id=user_id, ip=ip)
    with connection() as db:
        person = db.execute("SELECT id,email,name,is_active FROM users WHERE id=?", (user_id,)).fetchone()
    if person and bool(person["is_active"]) != bool(row["is_active"]):  # only when something really changed
        notify(dict(person), "reactivated" if active else "deactivated")


def admin_reset_link(admin_id, user_id, ip=""):
    """A password-reset link for someone who forgot theirs (until reset emails exist)."""
    if admin_id == user_id:
        raise ValueError("Change your own password in Settings.")
    with connection() as db:
        row = db.execute("SELECT is_active,role FROM users WHERE id=?", (user_id,)).fetchone()
    if not row or not row["is_active"]:
        raise ValueError("Reactivate the account first.")
    token = issue_link(user_id, "reset", admin_id)
    audit("reset_link_created", actor_id=admin_id, target_user_id=user_id, ip=ip)
    with connection() as db:
        person = db.execute("SELECT email,name FROM users WHERE id=?", (user_id,)).fetchone()
    from ahead import mailer
    mailer.queue(person["email"], "reset", user_id, person["name"], link=f"{site_url()}/?reset={token}")
    return token


def admin_sign_out(admin_id, user_id, ip=""):
    sign_out_everywhere(user_id)
    audit("signed_out_everywhere", actor_id=admin_id, target_user_id=user_id, ip=ip)


def is_postgres_lock_needed():
    return database.is_postgres()


def account_counts():
    with connection() as db:
        rows = db.execute("SELECT role,is_active,COUNT(*) n FROM users GROUP BY role,is_active").fetchall()
    counts = {ROLE_PATIENT: 0, ROLE_DOCTOR: 0, ROLE_ADMIN: 0, "inactive": 0}
    for row in rows:
        if row["is_active"]:
            counts[row["role"]] = counts.get(row["role"], 0) + row["n"]
        else:
            counts["inactive"] += row["n"]
    return counts
