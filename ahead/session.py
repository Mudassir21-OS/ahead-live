"""The Streamlit side of signing in and out (see ahead/accounts.py for how sessions work).

The tab identity
----------------
A tiny component (no visible output) keeps ``{"id", "secret"}`` in the tab's ``sessionStorage`` and reports
it to Python. It survives a reload; a new tab, a reopened link or a closed and reopened tab starts with
none, so a new identity is made and no earlier sign-in matches it. (Duplicating a tab copies
sessionStorage, so a duplicated tab stays signed in like a reload; that is the browser's choice.)
"""

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


_TAB_JS = """
export default function (component) {
  const { data, setStateValue } = component;
  const KEY = 'ahead_tab';
  const hex = (bytes) => Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
  const b64url = (bytes) => btoa(String.fromCharCode(...bytes)).replace(/\\+/g, '-').replace(/\\//g, '_').replace(/=+$/, '');
  let tab = null;
  try { tab = JSON.parse(window.sessionStorage.getItem(KEY) || 'null'); } catch (e) { tab = null; }
  const good = tab && /^[0-9a-f]{16}$/.test(tab.id || '') && /^[A-Za-z0-9_-]{43}$/.test(tab.secret || '');
  if (!good) {
    tab = { id: hex(crypto.getRandomValues(new Uint8Array(8))), secret: b64url(crypto.getRandomValues(new Uint8Array(32))) };
    try { window.sessionStorage.setItem(KEY, JSON.stringify(tab)); }
    catch (e) { tab = { id: '', secret: '' }; }  // no sessionStorage: sign-in still works, a reload signs out
  }
  if (!data || data.known !== tab.id) setStateValue('tab', tab);
}
"""
_tab_component = None
TAB_KEY = "tab_identity"          # {"id", "secret"} once the tab has reported it
_KEEP = ("ahead_tab_probe", TAB_KEY)  # survive sign-out: the tab is still the same tab


def _component():
    global _tab_component
    if _tab_component is None:
        _tab_component = st.components.v2.component("ahead_tab_identity", js=_TAB_JS)
    return _tab_component


def mount_tab_identity():
    """Ask the browser tab for its identity (first thing on every run). Returns it, or None until known."""
    known = st.session_state.get(TAB_KEY) or {}
    with st.container(key="ahead_tab_box", height=1, border=False):
        try:
            result = _component()(key="ahead_tab_probe", data={"known": known.get("id")}, on_tab_change=lambda: None)
        except Exception:  # test harness without a browser
            result = None
    reported = getattr(result, "tab", None) if result is not None else None
    if isinstance(reported, dict):
        tab = {"id": str(reported.get("id", "")), "secret": str(reported.get("secret", ""))}
        if accounts.valid_tab_id(tab["id"]) and accounts.valid_tab_secret(tab["secret"]):
            st.session_state[TAB_KEY] = tab
        else:
            st.session_state[TAB_KEY] = {"id": "", "secret": ""}  # the browser has no sessionStorage
    return st.session_state.get(TAB_KEY)


def _cookies():
    try:
        jar = dict(st.context.cookies)
    except Exception:
        return {}
    return {k: v for k, v in jar.items() if isinstance(k, str) and isinstance(v, str)}  # the test harness has none


def has_tab_cookie(cookies=None):
    return any(name.startswith(accounts.COOKIE_PREFIX) for name in (cookies if cookies is not None else _cookies()))


def resume(tab, cookies):
    """The (session_id, user) a reloaded tab returns to, or None. Needs this tab's cookie and secret."""
    if not tab or not accounts.valid_tab_id(tab.get("id")):
        return None
    return accounts.session_user(cookies.get(accounts.cookie_name(tab["id"])), tab.get("secret"))


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
    """After a correct password: open a session for this tab; the next page swaps the code for the tab's cookie."""
    tab = st.session_state.get(TAB_KEY) or {}
    session_id, code = accounts.start_session(account["id"], _user_agent(), request_ip(), tab.get("secret"))
    st.session_state.session_id = session_id
    if accounts.valid_tab_id(tab.get("id")):
        st.session_state.pending_handoff = (code, tab["id"])
    load_into_state(account)


def restore(tab):
    """Run on every rerun, before any page. Returns False while the tab hasn't reported its identity yet
    and there is a sign-in cookie it might match (the app then waits a moment instead of flashing the login).

    * Signed in in this tab: re-check the session (instant sign-out after deactivation or a password change).
    * Not signed in, but this tab has a valid session (a reload): sign back in silently.
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
        return True
    cookies = _cookies()
    if tab is None:
        return not has_tab_cookie(cookies)
    if not st.session_state.get("cookie_cleared"):
        found = resume(tab, cookies)
        if found:
            session_id, account = found
            st.session_state.session_id = session_id
            load_into_state(account)
    return True


def _is_active(user_id):
    from ahead.storage import connection
    with connection() as db:
        row = db.execute("SELECT is_active FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(row and row["is_active"])


def _forget(signed_out_notice=False):
    tab = st.session_state.get(TAB_KEY)
    for key in list(st.session_state.keys()):
        if key not in _KEEP:
            del st.session_state[key]
    if tab:
        st.session_state.cookie_tab = tab.get("id", "")
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
    pending = st.session_state.pop("pending_handoff", None)
    if pending:
        code, tab_id = pending
        _script("fetch('/auth/session',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},"
                f"body:JSON.stringify({{code:{json.dumps(code)},tab:{json.dumps(tab_id)}}})}});")
    if st.session_state.pop("clear_cookie", False):
        tab_id = st.session_state.pop("cookie_tab", "")
        _script("fetch('/auth/logout',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},"
                f"body:JSON.stringify({{tab:{json.dumps(tab_id)}}})}});")
