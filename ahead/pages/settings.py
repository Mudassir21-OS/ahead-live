"""
ahead/pages/settings.py
=======================
Profile · Appearance · Preferences · Account — tabbed settings inspired by
a conventional account-settings layout.
"""

from html import escape

import streamlit as st

from ahead.components import current_user, go_to, logout, markdown_literal, pref, page_header, username_hint
from ahead.theme import APPEARANCE_OPTIONS, appearance_mode
from ahead import accounts, alerts, mailer
from ahead.pages.calendar_panel import render_calendar_panel
from ahead.pages.consent_page import render_privacy_panel
from ahead.session import request_ip
from ahead.storage import change_password, password_ok, delete_records, delete_screenings, update_profile, update_preferences
from ahead.platform_data import doctors_for, grant_doctor, revoke_doctor, ai_consent, plans, prevention_plans


TABS = ["Profile", "Appearance", "Preferences", "Account"]
CARE_TEAM_REQUEST = "settings_care_team_request"   # set by open_care_team(), read once by render_settings
CARE_TEAM_EMAIL = "care_team_email"   # the Add doctor field; its state lives only while Settings is drawn


def open_care_team() -> None:
    """Open Settings on Preferences with Care team access first (the 'Share with your doctor' buttons)."""
    st.session_state[CARE_TEAM_REQUEST] = True
    go_to("settings")


PREFERENCES_REQUEST = "settings_preferences_request"   # set by open_preferences(), read once by render_settings


def open_preferences() -> None:
    """Open Settings straight on the Preferences tab (emails, calendar, care team), e.g. from Notifications."""
    st.session_state[PREFERENCES_REQUEST] = True
    go_to("settings")


def _preferences_requested() -> bool:
    requested = st.session_state.pop(PREFERENCES_REQUEST, False)
    if requested:   # a new tab-set key makes the browser open it fresh on Preferences
        st.session_state.settings_tabs_count = st.session_state.get("settings_tabs_count", 0) + 1
    return requested


def _care_team_focus(user: dict) -> bool:
    """
    True while a patient who asked to share a result stays on Settings. A request also gives the tabs a new key, so
    the browser opens that tab set fresh on Preferences instead of the tab it last showed. Streamlit drops the email
    field's state on any run that does not draw Settings, which ends the focus when the patient leaves.
    """
    requested = st.session_state.pop(CARE_TEAM_REQUEST, False) and not user["is_doctor"]
    if requested:
        st.session_state.settings_tabs_count = st.session_state.get("settings_tabs_count", 0) + 1
    focus = requested or (st.session_state.get("settings_care_focus", False) and CARE_TEAM_EMAIL in st.session_state)
    st.session_state.settings_care_focus = focus
    return focus


def _row(icon: str, title: str, copy: str) -> str:
    return f"""
<div class="settings-row">
    <div class="settings-row-icon">{icon}</div>
    <div>
        <div class="settings-row-title">{escape(title)}</div>
        <div class="settings-row-copy">{escape(copy)}</div>
    </div>
</div>"""


def _panel(title: str, copy: str) -> None:
    st.html(f'<div class="settings-panel"><h2>{escape(title)}</h2><p>{escape(copy)}</p></div>')


def _sync_pref(name: str) -> None:
    st.session_state.prefs[name] = st.session_state[f"pref_widget_{name}"]
    _save_prefs()


def _save_prefs() -> None:
    user = st.session_state.get("user")
    if user:
        update_preferences(user["id"], {
            "toggles": st.session_state.prefs,
            "appearance": st.session_state.get("appearance_mode", "Light"),
        })


def _toggle_row(icon: str, title: str, copy: str, name: str) -> None:
    """A preference row. The toggle is fed from st.session_state.prefs and writes back on change."""
    left, right = st.columns([0.86, 0.14])
    with left:
        st.html(_row(icon, title, copy))
    with right:
        st.toggle(
            title, value=pref(name), key=f"pref_widget_{name}", label_visibility="collapsed",
            on_change=_sync_pref, args=(name,),
        )


# =============================================================================
# TABS
# =============================================================================

def _profile_tab(user: dict) -> None:
    _panel("Account information", "Review and update the information associated with your AHEAD profile.")
    st.html(
        f"""
<div class="settings-profile-banner">
    <div class="settings-avatar">{escape(user['initials'])}</div>
    <div>
        <div class="settings-person-name">{escape(user['name'])}</div>
        <div class="settings-person-role">{user['role_label']}</div>
        <div class="settings-person-email">{escape(user['email'])}</div>
    </div>
</div>
"""
    )
    pending = st.session_state.get("settings_email_pending")
    if pending:
        st.info(f"We emailed a confirmation link to {markdown_literal(pending)}. Your email changes once you open it "
                "(within 24 hours); until then AHEAD keeps using your current address.")
    with st.form("settings_profile_form"):
        c1, c2 = st.columns(2)
        with c1:
            full_name = st.text_input("Full name", value=user["name"])
            st.text_input("Role", value=user["role_label"], disabled=True)
        with c2:
            email = st.text_input("Email", value=user["email"])
            institution = st.text_input(
                "Institution", value=user["institution"], disabled=not user["is_doctor"],
                placeholder="Not applicable for patient accounts" if not user["is_doctor"] else "",
            )
        current_pw = st.text_input("Current password (only needed to change your email)", type="password",
                                   autocomplete="current-password", key="settings_profile_password")
        if st.form_submit_button("Save changes", type="primary"):
            new_email = email.strip().lower()
            changing = new_email != user["email"].strip().lower()
            if not full_name.strip():
                st.error("Please enter your name.")
            elif "@" not in email or "." not in email.split("@")[-1]:
                st.error("Please enter a valid email address.")
            elif changing and not password_ok(st.session_state.user["id"], current_pw):
                # A tab left open must not be enough to move the account (and its password resets) to another inbox.
                st.error("To change your email, type your current password.")
            else:
                _save_profile(user, full_name, new_email, institution, changing)


def _save_profile(user, full_name, new_email, institution, changing):
    me = st.session_state.user["id"]
    old = {"id": me, "email": user["email"], "name": user["name"]}
    confirm_first = changing and mailer.enabled()
    if confirm_first:
        outcome = accounts.request_email_change(me, new_email, request_ip())
        if outcome == "limit":
            st.error("Too many email changes lately. Try again in an hour.")
            return
    try:
        # With emails on, the address itself only changes once the link sent to the new one is opened.
        changed = update_profile(me, full_name, user["email"] if confirm_first else new_email, institution)
    except Exception:
        st.error("Unable to save this profile. The email may already be used.")
        return
    if changed:  # emails off: changed at once; tell the old address anyway, in case someone else did it
        accounts.notify(old, "email_changed")
        accounts.audit("email_changed", actor_id=me, target_user_id=me, ip=request_ip())
    st.session_state.user.update({"name": full_name.strip(), "institution": institution.strip(),
                                  **({} if confirm_first else {"email": new_email})})
    if confirm_first:
        st.session_state["settings_email_pending"] = new_email
    st.toast("Profile updated.", icon="✅")
    st.rerun()


def _appearance_tab() -> None:
    _panel("Display mode", "Choose between a bright clinical interface and a darker low-light interface.")
    current = appearance_mode()
    columns = st.columns(len(APPEARANCE_OPTIONS))
    for column, (mode, (css, copy)) in zip(columns, APPEARANCE_OPTIONS.items()):
        active = mode == current
        with column:
            st.html(
                f"""
<div class="appearance-choice {'active' if active else ''}">
    <div class="appearance-preview {css}"></div>
    <strong>{mode}{'<span class="check">✓</span>' if active else ''}</strong>
    <span>{copy}</span>
</div>
"""
            )
            if st.button(
                "Selected" if active else f"Use {mode}", key=f"appearance_{mode}", width="stretch",
                type="primary" if active else "secondary", disabled=active,
            ):
                st.session_state.appearance_mode = mode
                _save_prefs()
                st.rerun()


def _care_team(user: dict, focus: bool = False) -> None:
    _panel('Care team access','You decide which doctor accounts may view your saved history and assign medication. Add as many doctors as you like, and remove access at any time.')
    if focus:
        st.info("Add your doctor's AHEAD account email to share your saved screening results with them.")
    with st.form('grant_doctor',clear_on_submit=True):
        address=st.text_input('Doctor account email',key=CARE_TEAM_EMAIL)
        if st.form_submit_button('Add doctor'):
            if grant_doctor(user['id'],address):st.toast('Doctor linked.');st.rerun()  # st.success would vanish in the rerun
            else:st.error('No doctor account found with this email.')
    for doctor in doctors_for(user['id']):
        name=markdown_literal(doctor['name'])   # names and plan names are typed by doctors: shown literally, never as Markdown
        st.write(f"{name} · {markdown_literal(doctor['email'])}")
        st.checkbox('Allow sending my linked records to Gemini for optional AI summaries',value=bool(doctor['ai_allowed']),
             key=f"ai_consent_{doctor['id']}",on_change=lambda did=doctor['id']:ai_consent(user['id'],did,st.session_state[f'ai_consent_{did}']))
        ongoing=[p['name'] for p in plans(user['id']) if p['doctor_id']==doctor['id'] and p['status']!='stopped']
        prevention=any(p['doctor_id']==doctor['id'] for p in prevention_plans(user['id']))
        with st.popover('Remove access',key=f"revoke_menu_{doctor['id']}"):
            st.write(f"{name} will no longer see your records or manage your medication.")
            if ongoing:st.warning('These plans will stop and cannot be restarted, even if you link this doctor again: '+', '.join(map(markdown_literal,ongoing))+'. Talk to a clinician before you stop a medicine.')
            if prevention:st.warning(f'Your prevention plan with {name} will stop; your tracker goals stay as they are.')
            if st.button('Remove access',key=f"revoke_{doctor['id']}",type='primary'):
                revoke_doctor(user['id'],doctor['id']);st.toast('Access removed.');st.rerun()


def _review_scope(user: dict) -> None:
    """Doctors: which saved results they hear about, by notice and (if on) email. Every result by default."""
    scopes = list(alerts.REVIEW_SCOPES)
    current = alerts.review_scope(user["id"])
    st.radio("Tell me about", scopes, index=scopes.index(current), format_func=alerts.REVIEW_SCOPES.get,
             key="review_scope", horizontal=True,
             help="Every new result: lower-risk results too, so you can act early. Results that need attention "
                  "(urgent or at risk) are always marked ⚠️ in Notifications.",
             on_change=lambda: alerts.set_review_scope(user["id"], st.session_state["review_scope"]))


def _emails_panel(user: dict) -> None:
    """Which optional emails this person gets (ahead/alerts.py). Account emails (confirm, reset, security notices)
    can't be switched off. In-app notifications are always on."""
    _panel("Emails", "Choose which optional emails AHEAD sends you. Emails never include results, medicine names "
                     "or patient names: they say that something changed and link to AHEAD.")
    if not mailer.enabled():
        st.caption("Emails are switched off on this server right now. Your choices are saved for when they're switched on.")
    saved = alerts.email_prefs(user["id"])
    with st.container(key="settings_rows"):
        for name in alerts.emails_for(user["role"]):
            _, title, copy = alerts.EMAILS[name]
            left, right = st.columns([0.86, 0.14])
            with left:
                st.html(_row("✉", title, copy))
            with right:
                st.toggle(title, value=saved[name], key=f"email_pref_{name}", label_visibility="collapsed",
                          on_change=lambda n=name: alerts.set_email_pref(user["id"], n, st.session_state[f"email_pref_{n}"]))
            if name == "doctor_reviews":
                _review_scope(user)
    st.caption("Sign-up confirmation, password reset and security notices (password or email changed) are always sent.")


def _preferences_tab(care_first: bool = False) -> None:
    user=st.session_state.get('user') or {}
    patient=user.get('role')=='Patient'
    if patient:   # Care team access always leads the tab: it is how a patient shares results with a doctor
        _care_team(user, focus=care_first)
    if user.get("role") in ("Patient", "Doctor"):
        _emails_panel(user)
    if patient:
        _panel("Reminders in my calendar", "Medicine reminders in your phone or computer calendar, even when AHEAD is closed.")
        render_calendar_panel(user["id"], "settings_calendar", compact=True)

    _panel("Data & privacy", "Your saved health values are stored encrypted. Notifications are deleted after 90 days, and "
                             "an account that is switched off is erased after one year.")
    if patient:
        render_privacy_panel(user["id"])
    with st.container(key="settings_rows_privacy"):
        _toggle_row("◈", "Keep screening history", "Save future results and show them on the Overview page.", "keep_history")
    history = st.session_state.get("screening_history", [])
    c1, c2 = st.columns([0.3, 0.7])
    with c1:
        if st.button(f"Delete my saved screening history ({len(history)})", disabled=not history, width="stretch"):
            st.session_state.screening_history = []
            st.session_state.last_results = {}
            delete_screenings(st.session_state.user["id"])
            st.toast("Your saved screening history was deleted.")
            st.rerun()

    if user := st.session_state.get("user"):
        if user["role"] == "Doctor" and st.button("Delete my saved clinical records", type="secondary"):
            delete_records(user["id"])
            st.toast("Your clinical review records were deleted.")
            st.rerun()


def _account_tab(user: dict) -> None:
    _panel("Security & account", "Manage how you sign in to AHEAD and end your session.")

    with st.expander("🔒  Change password — update your password regularly"):
        with st.form("settings_password_form"):
            username_hint(user["email"])
            current_pw = st.text_input("Current password", type="password", autocomplete="current-password")
            new_pw = st.text_input("New password", type="password", autocomplete="new-password")
            confirm_pw = st.text_input("Confirm new password", type="password", autocomplete="new-password")
            if st.form_submit_button("Update password", type="primary"):
                if not current_pw or not new_pw:
                    st.error("Please fill in every field.")
                elif len(new_pw) < 12:
                    st.error("Use at least 12 characters.")
                elif new_pw != confirm_pw:
                    st.error("The new passwords do not match.")
                elif not change_password(st.session_state.user["id"], current_pw, new_pw):
                    st.error("The current password is incorrect.")
                else:
                    if st.session_state.get("session_id"):
                        accounts.keep_session_after_password_change(st.session_state.session_id, user["id"])
                    accounts.audit("password_changed", actor_id=user["id"], target_user_id=user["id"], ip=request_ip())
                    accounts.notify(st.session_state.user, "password_changed")
                    st.success("Password updated. Your other devices and tabs were signed out.")

    with st.expander("🖥  Manage sessions — view and manage active sessions"):
        st.html(_row("●", "This tab", f"Signed in as {user['email']} · active now"))
        st.caption("Your sign-in belongs to this browser tab: reloading keeps you signed in, closing the tab signs "
                   "you out, and a new tab asks you to sign in again. AHEAD also signs you out after 2 hours "
                   "without activity, and after 12 hours in any case.")
        if st.button("Sign out on all my devices", key="settings_signout_all"):
            accounts.sign_out_everywhere(user["id"])
            accounts.audit("signed_out_everywhere", actor_id=user["id"], target_user_id=user["id"], ip=request_ip())
            logout()

    st.html(
        """
<div class="settings-danger">
    <strong>Log out</strong>
    <p>Sign out of AHEAD in this tab. Your saved history stays in your account.</p>
</div>
"""
    )
    if st.button("Log out", key="settings_logout", type="primary"):
        logout()

    st.html(
        """
<div class="settings-panel">
    <h2>About AHEAD</h2>
    <p>Analytical Health Early Awareness and Detection — a Senior Design machine-learning screening
    prototype for diabetes, cardiovascular disease and chronic kidney disease. It provides educational
    screening estimates and does not diagnose disease.</p>
</div>
"""
    )


# =============================================================================
# PAGE
# =============================================================================

def render_settings() -> None:
    user = current_user()
    page_header('Your account','Settings','Manage your profile, appearance, data and sign-in.' if user['is_doctor']
                else 'Personalize your experience, privacy and care team access.')
    focus = _care_team_focus(user)
    to_preferences = _preferences_requested()
    count = st.session_state.get("settings_tabs_count")
    with st.container(key="settings_tabs"):
        profile, appearance, preferences, account = st.tabs(
            TABS, default="Preferences" if focus or to_preferences else None,
            key=f"settings_tab_set_{count}" if count else None)
    with profile:
        _profile_tab(user)
    with appearance:
        _appearance_tab()
    with preferences:
        _preferences_tab(care_first=focus)
    with account:
        _account_tab(user)
