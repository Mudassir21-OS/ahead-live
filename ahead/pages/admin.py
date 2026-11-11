"""Accounts & Security: the admin's only page.

Admins manage who can sign in. They never see screenings, records, plans or any other health data
(least access: a stolen admin password exposes accounts, not medical information).
"""

import pandas as pd
import streamlit as st

from ahead import accounts, consent, fieldcrypt, mailer, retention
from ahead.components import markdown_literal, page_header
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
    "consent_given": "Consent given", "consent_withdrawn": "Consent withdrawn", "account_erased": "Account erased (retention)",
}
EMAIL_LABELS = {"verify": "Confirm email", "reset": "Password reset", "setup": "Account set-up",
                "password_changed": "Password changed notice", "email_changed": "Email changed notice",
                "deactivated": "Account switched off", "reactivated": "Account switched on",
                "email_change": "Confirm new email", "signup_exists": "Sign-up attempt notice"}
# Care emails (plan changed, result to review, new linked patient, weekly summary) are shown only as "Care notice":
# which one went to whom would tell an admin that a patient has a plan or a new result.
CARE_NOTICE = "Care notice"


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
    tabs = st.tabs(["Accounts", "Add a doctor or admin", "Audit log", "Emails", "Data protection", "My password"],
                   key="admin_tabs", on_change="rerun")
    for tab, draw in zip(tabs, (lambda: _accounts(me), lambda: _add(me), _audit, _emails, _data_protection,
                                lambda: _my_password(me))):
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
    consents = consent.status_by_user()
    st.dataframe(pd.DataFrame([{
        "Name": r["name"], "Email": r["email"], "Role": r["role"], "Status": _status(r),
        "Consent": _consent_text(r, consents), "Created": _when(r["created_at"]), "Last sign-in": _when(r["last_login_at"]),
    } for r in rows]), hide_index=True, width="stretch")
    st.caption("Accounts switched off for a year are erased automatically (Data protection tab). Reactivating one "
               "before then cancels that.")

    others = [r for r in rows if r["id"] != me["id"] and not r.get("erased_at")]
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
                st.session_state.admin_message = f"{markdown_literal(target['name'])} is deactivated and was signed out everywhere."
                st.rerun()
            if two.button("Email a password-reset link" if mailer.enabled() else "Create password-reset link",
                          key="admin_reset", width="stretch"):
                token = accounts.admin_reset_link(me["id"], chosen, ip)
                if mailer.enabled():
                    # Emailed to the account's own address only: an admin who could open the link could sign in as
                    # that patient (or as a doctor, and see their patients), and admins see no health data.
                    st.session_state.admin_message = (f"A password-reset link was emailed to {markdown_literal(target['name'])}. "
                                                      "It works once and expires in 60 minutes.")
                    st.rerun()
                st.session_state.admin_link = ("Reset", token)
            if three.button("Sign out everywhere", key="admin_signout", width="stretch"):
                accounts.admin_sign_out(me["id"], chosen, ip)
                st.session_state.admin_message = f"{markdown_literal(target['name'])} was signed out on every device."
                st.rerun()
        else:
            if one.button("Reactivate", key="admin_reactivate", width="stretch"):
                accounts.set_active(me["id"], chosen, True, ip)
                st.session_state.admin_message = f"{markdown_literal(target['name'])} can sign in again."
                st.rerun()
    except ValueError as error:
        st.error(str(error))
    if message := st.session_state.pop("admin_message", None):
        st.success(message)
    if link := st.session_state.pop("admin_link", None):
        _show_link(*link)


def _status(row):
    if row.get("erased_at"):
        return "Erased"
    if row["is_active"]:
        return "Active"
    when = retention.erase_on(row.get("deactivated_at"))
    return f"Deactivated · erased {to_local(when.isoformat()):%d %b %Y}" if when else "Deactivated"


def _consent_text(row, consents):
    """Patients only: whether they agreed to how AHEAD uses their information (not health data)."""
    if row["role"] != ROLE_PATIENT or row.get("erased_at"):
        return "—"
    state, when = consents.get(row["id"], (None, None))
    if state is None:
        return "Not yet (asked at next sign-in)"
    return {"given": "Given", "withdrawn": "Withdrawn", "old version": "Older text (asked again)"}[state] + f" · {_when(when)[:11]}"


def _data_protection():
    st.markdown("**Encryption of health values**")
    if not fieldcrypt.enabled():
        st.warning("Health values are not encrypted: AHEAD_FIELD_KEY is not set in Render → Environment. "
                   "Add it (and save a copy in a password manager), then Save and deploy.")
    else:
        waiting = fieldcrypt.pending()
        keys = fieldcrypt.keys()
        if waiting:
            st.info(f"Encryption is on (key {fieldcrypt.current_key_id()}). {sum(waiting.values())} older values are "
                    "still being encrypted in the background; refresh in a minute.")
        else:
            st.success(f"Encryption is on (key {fieldcrypt.current_key_id()}). Every saved health value is encrypted.")
        if len(keys) > 1 and not waiting:
            st.info("Nothing uses the older key in AHEAD_FIELD_KEY any more: you can remove it (everything after the comma).")
        st.caption("Admins never see the values themselves. Losing the key loses the encrypted data, so keep it in a "
                   "password manager.")
    st.markdown("**Retention**")
    st.markdown(f"- Notifications: deleted after **{retention.NOTICE_DAYS} days**\n"
                f"- Email records: deleted after **{retention.EMAIL_DAYS} days** (the email text is deleted once sent)\n"
                f"- Account audit log: deleted after **{retention.AUDIT_DAYS // 365} year**\n"
                f"- Accounts switched off: erased after **{retention.ACCOUNT_DAYS // 365} year**, unless reactivated")
    last = retention.last_run()
    if last:
        st.caption(f"Last clean-up {_when(last['at'])}: {last['notices']} notifications, {last['emails']} email records "
                   f"and {last['accounts']} accounts removed{_audit_part(last)}. It runs at start-up (at most daily) and weekly.")
    if st.button("Run the clean-up now", key="admin_retention_run"):
        done = retention.run(force=True)
        st.success(f"Done: {done['notices']} notifications, {done['emails']} email records and {done['accounts']} "
                   f"accounts removed{_audit_part(done)}.")


def _audit_part(result):
    return f" (and {result['audit']} audit entries over a year old)" if result.get("audit") else ""


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
        "Queued": _when(r["created_at"]), "To": r["to_email"], "Email": EMAIL_LABELS.get(r["kind"], CARE_NOTICE),
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
