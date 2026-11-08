"""Sign-in sessions, sign-in limits, one-time account links and the audit log.

How a sign-in survives a page reload
------------------------------------
Streamlit forgets ``st.session_state`` when the browser reloads, so the sign-in also lives in an
HttpOnly cookie that page scripts cannot read:

1. The sign-in form checks the password and calls ``start_session``. That saves a ``sessions`` row
   and returns a one-time handoff code (valid 60 seconds).
2. A small script on the next page POSTs the code to ``/auth/session`` (``ahead/server.py``), which
   calls ``complete_handoff``: it makes the real session token, stores only its SHA-256 and sends the
   token back as the cookie.
3. On every page load and every rerun ``session_user`` checks the cookie's session: not revoked,
   not expired, not idle too long, account still active and its ``token_version`` unchanged.
   Deactivating an account or changing its password raises ``token_version`` and so signs that person
   out everywhere at once.
"""

import hashlib
import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone

from ahead import db as database
from ahead.config import ROLE_ADMIN, ROLE_DOCTOR, ROLE_PATIENT
from ahead.storage import MIN_PASSWORD, connection, password_hash, user_view

log = logging.getLogger(__name__)

COOKIE_NAME = "ahead_session"
HANDOFF_SECONDS = 60
# A medical record should not stay open on a shared computer: sign out after 2 hours without activity,
# and after 12 hours in any case (the cookie's own lifetime).
IDLE_MINUTES = int(os.environ.get("AHEAD_SESSION_IDLE_MINUTES", "120"))
MAX_HOURS = int(os.environ.get("AHEAD_SESSION_MAX_HOURS", "12"))
SEEN_EVERY = timedelta(minutes=5)  # how often last_seen_at is written (not on every click)

SETUP_DAYS = 3
RESET_MINUTES = 60

# Sign-in limits. Only failures count, by email + address and by address alone, so a class sharing
# campus Wi-Fi is not blocked by one person's typos.
FAIL_WINDOW = timedelta(minutes=15)
FAILS_PER_ACCOUNT = 5
FAILS_PER_ADDRESS = 50
SIGNUP_WINDOW = timedelta(hours=1)
SIGNUPS_PER_ADDRESS = 5


def _now():
    return datetime.now(timezone.utc)


def _stamp(moment=None):
    return (moment or _now()).isoformat()


def _parse(stamp):
    moment = datetime.fromisoformat(stamp)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


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
    """Drop old rate-limit rows and dead sessions (called at start-up)."""
    cutoff = _stamp(_now() - timedelta(days=days))
    with connection() as db:
        db.execute("DELETE FROM rate_events WHERE created_at<?", (_stamp(_now() - timedelta(days=2)),))
        db.execute("DELETE FROM sessions WHERE expires_at<? OR (revoked_at IS NOT NULL AND revoked_at<?)", (cutoff, cutoff))


# =============================================================================
# SESSIONS
# =============================================================================

def start_session(user_id, user_agent="", ip=""):
    """After a correct password: a session row and the one-time code that turns into the cookie."""
    code = secrets.token_urlsafe(32)
    now = _now()
    with connection() as db:
        version = db.execute("SELECT token_version FROM users WHERE id=?", (user_id,)).fetchone()[0]
        cursor = db.execute("""INSERT INTO sessions(user_id,handoff_hash,handoff_expires_at,token_version,created_at,last_seen_at,expires_at,user_agent)
                               VALUES(?,?,?,?,?,?,?,?)""",
                            (user_id, _hash(code), _stamp(now + timedelta(seconds=HANDOFF_SECONDS)), version, _stamp(now),
                             _stamp(now), _stamp(now + timedelta(hours=MAX_HOURS)), (user_agent or "")[:200]))
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
                           u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE {}"""


def _touch(db, row, now):
    if _parse(row["last_seen_at"]) + SEEN_EVERY <= now:
        db.execute("UPDATE sessions SET last_seen_at=? WHERE id=?", (_stamp(now), row["session_id"]))


def session_user(token):
    """(session_id, user) for a cookie token, or None when it is unknown, expired, idle or revoked."""
    if not isinstance(token, str) or not token or len(token) > 200:
        return None
    now = _now()
    with connection() as db:
        row = db.execute(_SESSION_QUERY.format("s.token_hash=?"), (_hash(token),)).fetchone()
        if not _valid(row, now):
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
    """A new one-time token for 'setup' or 'reset'; earlier unused ones of that kind stop working."""
    if purpose not in ("setup", "reset"):
        raise ValueError(purpose)
    token = secrets.token_urlsafe(32)
    now = _now()
    life = timedelta(days=SETUP_DAYS) if purpose == "setup" else timedelta(minutes=RESET_MINUTES)
    with connection() as db:
        db.execute("UPDATE account_tokens SET used_at=? WHERE user_id=? AND purpose=? AND used_at IS NULL",
                   (_stamp(now), user_id, purpose))
        db.execute("INSERT INTO account_tokens(user_id,purpose,token_hash,created_by,created_at,expires_at) VALUES(?,?,?,?,?,?)",
                   (user_id, purpose, _hash(token), created_by, _stamp(now), _stamp(now + life)))
    return token


def link_account(token):
    """The account a still-valid link belongs to: {'purpose', 'user'} or None."""
    if not token or len(token) > 200:
        return None
    with connection() as db:
        row = db.execute("""SELECT t.purpose, u.* FROM account_tokens t JOIN users u ON u.id=t.user_id
                            WHERE t.token_hash=? AND t.used_at IS NULL AND t.expires_at>=?""",
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
        row = db.execute("SELECT id,user_id,purpose FROM account_tokens WHERE token_hash=? AND used_at IS NULL AND expires_at>=?",
                         (_hash(token or ""), now)).fetchone()
        if not row or not db.execute("UPDATE account_tokens SET used_at=? WHERE id=? AND used_at IS NULL",
                                     (now, row["id"])).rowcount:
            return None
        active = db.execute("SELECT is_active FROM users WHERE id=?", (row["user_id"],)).fetchone()
        if not active or not active["is_active"]:
            return None
        db.execute("UPDATE users SET password_hash=?,token_version=token_version+1 WHERE id=?",
                   (password_hash(new_password), row["user_id"]))
        db.execute("UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL", (now, row["user_id"]))
    audit("password_set_from_link", actor_id=row["user_id"], target_user_id=row["user_id"], ip=ip, purpose=row["purpose"])
    with connection() as db:
        return user_view(db.execute("SELECT * FROM users WHERE id=?", (row["user_id"],)).fetchone())


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
    if not create_user(email, secrets.token_urlsafe(24), role, name, institution, created_by=admin_id):
        raise ValueError("An account with that email already exists.")
    with connection() as db:
        user_id = db.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()[0]
    audit("account_created", actor_id=admin_id, target_user_id=user_id, ip=ip, role=role)
    return user_id, issue_link(user_id, "setup", admin_id)


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
