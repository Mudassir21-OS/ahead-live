"""Accounts & Security: the admin's only page.

Admins manage who can sign in. They never see screenings, records, plans or any other health data
(least access: a stolen admin password exposes accounts, not medical information).
"""

import pandas as pd
import streamlit as st

from ahead import accounts, mailer
from ahead.components import page_header
from ahead.config import ROLE_ADMIN, ROLE_DOCTOR, ROLE_PATIENT
from ahead.platform_data import to_local
from ahead.session import request_ip
from ahead.storage import MIN_PASSWORD, change_password
from ahead.visuals import metric

ACTION_LABELS = {
    "sign_in": "Signed in", "sign_out": "Signed out", "sign_in_blocked": "Sign-in blocked (too many failures)",
    "patient_registered": "Patient registered", "account_created": "Account created",
    "account_deactivated": "Account deactivated", "account_reactivated": "Account reactivated",
    "reset_link_created": "Reset link created", "setup_link_created": "Set-up link created",
    "password_set_from_link": "Password set from a link", "password_changed": "Password changed",
    "signed_out_everywhere": "Signed out everywhere", "email_verified": "Email confirmed",
    "reset_requested": "Password reset requested", "email_changed": "Email address changed",
}
EMAIL_LABELS = {"verify": "Confirm email", "reset": "Password reset", "setup": "Account set-up",
                "password_changed": "Password changed notice", "email_changed": "Email changed notice",
                "deactivated": "Account switched off", "reactivated": "Account switched on",
                "plan_updated": "Plan changed", "doctor_review": "Result to review", "doctor_review_more": "More results (follow-up)",
                "doctor_linked": "New linked patient", "weekly": "Weekly summary"}


def _scheduled(row):
    """A follow-up waiting for its hour (queued earlier, never tried, due later than it was queued)."""
    return row["status"] == "pending" and row.get("attempts", 0) == 0 and \
        (row.get("next_attempt_at") or "") > (row["created_at"] or "")


def _when(stamp):
    return f"{to_local(stamp):%d %b %Y, %H:%M}" if stamp else "—"


def _show_link(kind, token):
    st.success(f"{kind} link created. Copy it now: it is shown only once.")
    st.code(f"{accounts.site_url()}/?{'setup' if kind == 'Set-up' else 'reset'}={token}", language=None)
    sent = ("AHEAD has also emailed it to them (see the Emails tab). Pass it on yourself if the email doesn't arrive. "
            if mailer.enabled() else "Send it to the person yourself (emails are not switched on). ")
    st.caption(sent + ("It works once and expires in 3 days." if kind == "Set-up" else "It works once and expires in 60 minutes."))


def render_admin():
    me = st.session_state.user
    page_header("Administration", "Accounts & Security",
                "Add doctors, switch accounts off and on, and see who signed in. No health data is shown here.")
    counts = accounts.account_counts()
    a, b, c, d = st.columns(4)
    with a: metric("Patients", counts[ROLE_PATIENT], "Active patient accounts", "#0E9AA7", "◉")
    with b: metric("Doctors", counts[ROLE_DOCTOR], "Active doctor accounts", "#2563EB", "✚")
    with c: metric("Admins", counts[ROLE_ADMIN], "Active admin accounts", "#8B5CF6", "◆")
    with d: metric("Deactivated", counts["inactive"], "Can't sign in", "#64748B", "◌")

    # A keyed tab set keeps the chosen tab after a button press (so a new set-up link stays on screen),
    # and only the open tab's content runs.
    tabs = st.tabs(["Accounts", "Add a doctor or admin", "Audit log", "Emails", "My password"], key="admin_tabs",
                   on_change="rerun")
    for tab, draw in zip(tabs, (lambda: _accounts(me), lambda: _add(me), _audit, _emails, lambda: _my_password(me))):
        if tab.open:
            with tab:
                draw()


def _accounts(me):
    left, right = st.columns([0.3, 0.7])
    role = left.selectbox("Show", ["All", ROLE_PATIENT, ROLE_DOCTOR, ROLE_ADMIN], key="admin_role_filter")
    search = right.text_input("Search name or email", key="admin_search").strip().lower()
    rows = accounts.list_accounts(None if role == "All" else role)
    if search:
        rows = [r for r in rows if search in r["name"].lower() or search in r["email"]]
    if not rows:
        st.info("No accounts match.")
        return
    st.dataframe(pd.DataFrame([{
        "Name": r["name"], "Email": r["email"], "Role": r["role"],
        "Status": "Active" if r["is_active"] else "Deactivated",
        "Created": _when(r["created_at"]), "Last sign-in": _when(r["last_login_at"]),
    } for r in rows]), hide_index=True, width="stretch")

    others = [r for r in rows if r["id"] != me["id"]]
    if not others:
        st.caption("You can't change your own account here; use My password.")
        return
    labels = {r["id"]: f"{r['name']} · {r['email']} · {r['role']}" for r in others}
    chosen = st.selectbox("Manage an account", list(labels), format_func=labels.get, index=None,
                          placeholder="Choose an account", key="admin_pick")
    if chosen is None:
        return
    target = next(r for r in others if r["id"] == chosen)
    ip = request_ip()
    one, two, three = st.columns(3)
    try:
        if target["is_active"]:
            if one.button("Deactivate", key="admin_deactivate", width="stretch"):
                accounts.set_active(me["id"], chosen, False, ip)
                st.session_state.admin_message = f"{target['name']} is deactivated and was signed out everywhere."
                st.rerun()
            if two.button("Create password-reset link", key="admin_reset", width="stretch"):
                st.session_state.admin_link = ("Reset", accounts.admin_reset_link(me["id"], chosen, ip))
            if three.button("Sign out everywhere", key="admin_signout", width="stretch"):
                accounts.admin_sign_out(me["id"], chosen, ip)
                st.session_state.admin_message = f"{target['name']} was signed out on every device."
                st.rerun()
        else:
            if one.button("Reactivate", key="admin_reactivate", width="stretch"):
                accounts.set_active(me["id"], chosen, True, ip)
                st.session_state.admin_message = f"{target['name']} can sign in again."
                st.rerun()
    except ValueError as error:
        st.error(str(error))
    if message := st.session_state.pop("admin_message", None):
        st.success(message)
    if link := st.session_state.pop("admin_link", None):
        _show_link(*link)


def _add(me):
    st.caption("Patients register themselves. Doctors and admins are added here and choose their own password "
               "through a one-time set-up link.")
    with st.form("admin_add", clear_on_submit=True):
        name = st.text_input("Full name", max_chars=120)
        email = st.text_input("Email", max_chars=254)
        role = st.radio("Role", [ROLE_DOCTOR, ROLE_ADMIN], horizontal=True)
        institution = st.text_input("Clinic or institution (optional)", max_chars=160)
        submitted = st.form_submit_button("Create account", type="primary")
    if submitted:
        try:
            _, token = accounts.create_staff_account(me["id"], email, name, role, institution, request_ip())
        except ValueError as error:
            st.error(str(error))
        else:
            _show_link("Set-up", token)


def _audit():
    choices = {"All events": None, **{label: key for key, label in ACTION_LABELS.items()}}
    picked = st.selectbox("Event", list(choices), key="admin_audit_filter")
    entries = accounts.audit_entries(200, choices[picked])
    if not entries:
        st.info("No events yet.")
        return
    st.dataframe(pd.DataFrame([{
        "When": _when(e["created_at"]), "Event": ACTION_LABELS.get(e["action"], e["action"]),
        "By": e["actor_name"] or "—", "Account": e["target_email"] or "—", "Address": e["ip"] or "—",
    } for e in entries]), hide_index=True, width="stretch")
    st.caption("The latest 200 events. The audit log records account activity only, never health information.")


def _emails():
    if not mailer.enabled():
        st.info("Emails are switched off. Add BREVO_API_KEY in Render (Environment) to send confirmation, "
                "password-reset, set-up and account emails.")
        return
    a, b = st.columns(2)
    with a: metric("Sent today", mailer.sent_today(), f"Daily limit {mailer.daily_limit()} (UTC day)", "#0E9AA7", "✉")
    with b: metric("Sender", mailer.sender()[0].split("@")[0], mailer.sender()[0], "#2563EB", "◎")
    rows = mailer.recent(100)
    if not rows:
        st.info("No emails yet.")
        return
    st.dataframe(pd.DataFrame([{
        "Queued": _when(r["created_at"]), "To": r["to_email"], "Email": EMAIL_LABELS.get(r["kind"], r["kind"]),
        "Status": "Scheduled" if _scheduled(r) else r["status"].capitalize(), "Tries": r["attempts"],
        "Sent": f"Due {_when(r['next_attempt_at'])}" if _scheduled(r) else _when(r["sent_at"]),
        "Problem": r["last_error"][:120] or "—",
    } for r in rows]), hide_index=True, width="stretch")
    st.caption("The latest 100 emails. Only who, what kind and whether it went are kept; the text is deleted once sent. "
               "Emails never contain health information.")


def _my_password(me):
    with st.form("admin_password", clear_on_submit=True):
        current = st.text_input("Current password", type="password", autocomplete="current-password")
        new = st.text_input(f"New password ({MIN_PASSWORD}+ characters)", type="password", autocomplete="new-password")
        confirm = st.text_input("Type it again", type="password", autocomplete="new-password")
        submitted = st.form_submit_button("Change password")
    if submitted:
        if len(new) < MIN_PASSWORD:
            st.error(f"Use at least {MIN_PASSWORD} characters.")
        elif new != confirm:
            st.error("The two new passwords don't match.")
        elif not change_password(me["id"], current, new):
            st.error("The current password is wrong.")
        else:
            if st.session_state.get("session_id"):
                accounts.keep_session_after_password_change(st.session_state.session_id, me["id"])
            accounts.audit("password_changed", actor_id=me["id"], target_user_id=me["id"], ip=request_ip())
            accounts.notify(me, "password_changed")
            st.success("Password changed. Your other devices and tabs were signed out.")
