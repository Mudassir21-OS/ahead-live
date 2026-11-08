"""The Streamlit side of signing in and out (see ahead/accounts.py for how sessions work)."""

import json
from datetime import datetime

import streamlit as st

from ahead import accounts
from ahead.config import DISEASES, ROLE_ADMIN
from ahead.storage import load_screenings


def request_ip():
    try:
        headers, direct = dict(st.context.headers), st.context.ip_address
    except Exception:  # outside a browser session (tests)
        return ""
    headers = {k: v for k, v in headers.items() if isinstance(k, str) and isinstance(v, str)}
    return accounts.client_ip(headers, direct if isinstance(direct, str) else "")


def _user_agent():
    try:
        agent = st.context.headers.get("User-Agent", "")
    except Exception:
        return ""
    return agent if isinstance(agent, str) else ""


def _cookie_token():
    try:
        token = st.context.cookies.get(accounts.COOKIE_NAME)
    except Exception:
        return None
    return token if isinstance(token, str) else None  # the test harness has no real cookies


def load_into_state(account):
    """Put a signed-in account and its saved settings into this browser tab's state."""
    st.session_state.authenticated = True
    st.session_state.user = account
    preferences = account.get("preferences", {})
    st.session_state.prefs = {**st.session_state.prefs, **preferences.get("toggles", {})}
    st.session_state.appearance_mode = preferences.get("appearance", "Light")
    st.session_state.screening_history = [
        {"time": datetime.fromisoformat(item["created_at"]), "disease": item["disease"],
         "label": DISEASES[item["disease"]]["label"], "model": item["model"],
         "probability": item["score"], "prediction": item["prediction"], "threshold": item["threshold"]}
        for item in load_screenings(account["id"]) if item["disease"] in DISEASES
    ] if account["role"] != ROLE_ADMIN else []


def begin(account):
    """After a correct password: open a session; the next page swaps the code for the cookie."""
    session_id, code = accounts.start_session(account["id"], _user_agent(), request_ip())
    st.session_state.session_id = session_id
    st.session_state.pending_handoff = code
    load_into_state(account)


def restore():
    """Run on every rerun, before any page.

    * Signed in in this tab: re-check the session (instant sign-out after deactivation or a password change).
    * Not signed in, but the browser has a valid session cookie (a reload): sign back in silently.
    """
    if st.session_state.get("authenticated"):
        session_id = st.session_state.get("session_id")
        if session_id:
            fresh = accounts.check_session(session_id)
            if fresh is not None:
                st.session_state.user = {**st.session_state.user, **fresh}  # name, role, email as stored now
        else:  # signed in without a cookie session (automated tests): still check the account is active
            user = st.session_state.get("user") or {}
            fresh = user if user.get("id") and _is_active(user["id"]) else None
        if fresh is None:
            _forget(signed_out_notice=True)
            st.rerun()
        return
    token = _cookie_token()
    if token and not st.session_state.get("cookie_cleared"):
        found = accounts.session_user(token)
        if found:
            session_id, account = found
            st.session_state.session_id = session_id
            load_into_state(account)


def _is_active(user_id):
    from ahead.storage import connection
    with connection() as db:
        row = db.execute("SELECT is_active FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row and row["is_active"])


def _forget(signed_out_notice=False):
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    # The cookie still exists in this browser until the script below clears it; don't restore from it meanwhile.
    st.session_state.cookie_cleared = True
    st.session_state.clear_cookie = True
    if signed_out_notice:
        st.session_state.signed_out_notice = True


def sign_out():
    session_id = st.session_state.get("session_id")
    user = st.session_state.get("user") or {}
    if session_id:
        accounts.end_session(session_id)
        accounts.audit("sign_out", actor_id=user.get("id"), target_user_id=user.get("id"), ip=request_ip())
    _forget()
    st.rerun()


def _script(body):
    st.html(f"<script>{body}</script>", unsafe_allow_javascript=True)


def browser_scripts():
    """Cookie work the server can't do over the websocket: done by a same-site POST from the page."""
    code = st.session_state.pop("pending_handoff", None)
    if code:
        _script("fetch('/auth/session',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},"
                f"body:JSON.stringify({{code:{json.dumps(code)}}})}});")
    if st.session_state.pop("clear_cookie", False):
        _script("fetch('/auth/logout',{method:'POST',credentials:'same-origin'});")
