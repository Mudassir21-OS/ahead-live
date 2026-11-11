"""The consent page (Phase 3b): a patient without a current agreement sees it after signing in, before anything else.
Also the short version shown in the sign-up form and the Data & privacy panel in Settings."""

import time
from html import escape

import streamlit as st

from ahead import consent
from ahead.components import page_header
from ahead.platform_data import to_local
from ahead.session import request_ip, sign_out

_CHECKED = "consent_checked_at"   # a current agreement is re-checked at most once a minute (this runs on every click)


def points_html():
    rows = "".join(f"""
<div class="settings-row">
    <div class="settings-row-icon">{n}</div>
    <div>
        <div class="settings-row-title">{escape(title)}</div>
        <div class="settings-row-copy">{escape(copy)}</div>
    </div>
</div>""" for n, (title, copy) in enumerate(consent.POINTS, 1))
    return f'<div class="consent-points">{rows}</div>'


def needs_consent(user_id, is_patient):
    """True when this patient must agree before using AHEAD."""
    if not is_patient:
        return False
    if time.monotonic() - st.session_state.get(_CHECKED, -1e9) < 60:
        return False
    if consent.has_consent(user_id):
        st.session_state[_CHECKED] = time.monotonic()
        return False
    return True


def forget_check():
    """After withdrawing: ask again on the next run."""
    st.session_state.pop(_CHECKED, None)


def render_consent_gate(user_id):
    previous = consent.latest(user_id)
    page_header("Before you continue", consent.TITLE,
                "Please read this once. You can change your mind at any time in Settings.")
    if previous and previous["withdrawn_at"]:
        st.info(f"You withdrew your agreement on {to_local(previous['withdrawn_at']):%d %b %Y}. "
                "Agree again to keep using AHEAD.")
    elif previous:
        st.info("We've updated this text since you last agreed. Please read it again.")
    with st.container(key="consent_box"):
        st.html(points_html())
        with st.form("consent_form", border=False):
            agreed = st.checkbox("I have read this and agree to AHEAD using my information as described.")
            submitted = st.form_submit_button("Agree and continue", type="primary")
        if submitted:
            if not agreed:
                st.error("Tick the box to agree, or sign out below.")
            else:
                consent.give(user_id, request_ip())
                st.session_state[_CHECKED] = time.monotonic()
                st.rerun()
        if st.button("Sign out instead", key="consent_sign_out"):
            sign_out()


def render_privacy_panel(user_id):
    """Settings → Preferences → Data & privacy, for patients: the agreement on record and a way to withdraw it."""
    row = consent.latest(user_id)
    if consent.is_current(row):
        st.caption(f"You agreed to how AHEAD uses your information on {to_local(row['given_at']):%d %b %Y, %H:%M}.")
    with st.expander("Read what you agreed to"):
        st.html(points_html())
    if consent.is_current(row):
        with st.popover("Withdraw my agreement", key="consent_withdraw_menu"):
            st.write("AHEAD will ask you to agree again before you can use it. What's already saved stays until you "
                     "delete it or your account is erased; to stop a doctor seeing it, remove them from your care team.")
            if st.button("Withdraw", key="consent_withdraw", type="primary"):
                consent.withdraw(user_id, request_ip())
                forget_check()
                st.rerun()
