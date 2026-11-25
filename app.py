"""
AHEAD — Analytical Health Early Awareness and Detection
Clinical Screening Portal · Senior Design Project

Entry point: page configuration, routing and the sidebar.  Everything else
lives in the ``ahead`` package:

    ahead/config.py      static configuration (diseases, form layout, labels, nav)
    ahead/resources.py   cached models, metadata, datasets, images, Gemini
    ahead/theme.py       colour tokens, global CSS, Plotly styling
    ahead/components.py  shared UI pieces and session-state helpers
    ahead/screening.py   the guided screening flow
    ahead/pages/         one module per page
"""

import time
from html import escape
from pathlib import Path

import streamlit as st

st.set_page_config(
    page_title="AHEAD | Clinical Screening",
    page_icon=str(Path(__file__).resolve().parent / "assets" / "favicon.svg"),
    layout="wide",
    initial_sidebar_state="expanded",
)

from ahead.browser import close_phone_menu  # noqa: E402
from ahead.components import current_user, go_to, init_state, open_assistant  # noqa: E402
from ahead.session import browser_scripts, mount_tab_identity, restore, sign_out  # noqa: E402
from ahead.config import NAV_ITEMS  # noqa: E402
from ahead import navigation  # noqa: E402
from ahead.resources import image_data_uri  # noqa: E402
from ahead.pages.analytics import render_analytics  # noqa: E402
from ahead.pages.assistant import render_assistant  # noqa: E402
from ahead.pages.clinical import render_clinical_dashboard  # noqa: E402
from ahead.pages.overview import render_overview  # noqa: E402
from ahead.pages.public import (PUBLIC_CONTENT, render_account_link, render_login, render_public_page,  # noqa: E402
                                render_tab_wait, render_verify_link)
from ahead.pages.admin import render_admin  # noqa: E402
from ahead.pages.screenings import render_screenings  # noqa: E402
from ahead.pages.settings import render_settings  # noqa: E402
from ahead.theme import apply_theme, inject_app_css, inject_global_css, sidebar_dynamic_css  # noqa: E402
from ahead.storage import init_db  # noqa: E402
from ahead.platform_data import notifications, startup_platform  # noqa: E402
from ahead.pages.wellness import render_wellness  # noqa: E402
from ahead.pages.food import render_food  # noqa: E402
from ahead.pages.medications import render_medications  # noqa: E402
from ahead.pages.notifications import render_notifications  # noqa: E402
from ahead.pages.notifications import refresh_patient_notifications  # noqa: E402
from ahead.pages.doctor_intelligence import render_doctor_intelligence  # noqa: E402
from ahead.pages.consent_page import needs_consent, render_consent_gate  # noqa: E402

init_db()
startup_platform()
init_state()
tab = mount_tab_identity()   # this browser tab's own identity (sessionStorage): sign-ins belong to one tab
ready = restore(tab)         # a reload of a signed-in tab signs back in; deactivated accounts are signed out
browser_scripts()            # sets or clears this tab's cookie after signing in or out
inject_global_css()

if not st.session_state.authenticated:
    navigation.register_signed_out()   # keep a page's address through the sign-in screens (ahead/navigation.py)

if not ready and not st.session_state.get("skip_tab_wait"):
    apply_theme(force_light=True)
    render_tab_wait()        # for a moment after a reload, until the tab reports which sign-in is its own
    st.stop()

# =============================================================================
# PUBLIC ROUTING  (?page=login|about|contact|help)
# =============================================================================

requested = st.query_params.get("page", "login")
if requested not in PUBLIC_CONTENT:
    requested = "login"

verify_token = st.query_params.get("verify")
if verify_token and not st.session_state.authenticated:
    apply_theme(force_light=True)
    render_verify_link(verify_token)
    st.stop()

link_token = st.query_params.get("setup") or st.query_params.get("reset")
if link_token and not st.session_state.authenticated:
    apply_theme(force_light=True)
    render_account_link(link_token)
    st.stop()

if not st.session_state.authenticated:
    apply_theme(force_light=True)
    if requested == "login":
        render_login()
    else:
        render_public_page(requested)
    st.stop()

# Signed in: public query params are meaningless, drop them.
if "page" in st.query_params:
    st.query_params.clear()

apply_theme()
inject_app_css()
user = current_user()

# A patient without a current agreement to how AHEAD uses their information sees only the consent page.
if needs_consent(st.session_state.user["id"], not (user["is_doctor"] or user["is_admin"])):
    navigation.register_signed_out()
    render_consent_gate(st.session_state.user["id"])
    st.stop()

# =============================================================================
# SIDEBAR
# =============================================================================

patient_only = {'wellness','food','medications'}   # Notifications: patients and, since Phase 2b, doctors
if user["is_admin"]:
    # Admins manage accounts only: no screenings, records or patient pages (least access).
    visible_nav = [("admin", "Accounts & Security", "settings.svg", False)]
else:
    visible_nav = [item for item in NAV_ITEMS if (not item[3] or user["is_doctor"])
                   and (not user["is_doctor"] or item[0] not in patient_only)]
nav_keys = {item[0] for item in visible_nav}
# One address per page (/screenings, /medications, ...): the browser's back and forward buttons move between
# AHEAD's pages. This also corrects st.session_state.nav if it names a page this person can't open.
navigation.sync([item[0] for item in visible_nav], "admin" if user["is_admin"] else "overview",
                {key: f"{label} · AHEAD" for key, label, _icon, _doctor in visible_nav})
active = st.session_state.nav
st.set_page_config(page_title=f"{dict((k, l) for k, l, _i, _d in visible_nav).get(active, 'AHEAD')} · AHEAD")

with st.sidebar:
    st.html(
        f"""
<div class="sidebar-brand">
    <img class="brand-symbol" src="{image_data_uri('logo.svg')}" alt="AHEAD logo">
    <div>
        <h2>AHEAD</h2>
        <p>Clinical Screening &amp;<br>Early Awareness Platform</p>
    </div>
</div>
<div class="sidebar-divider"></div>
"""
    )

    with st.container(key="sidebar_nav"):
        unread = 0
        if user['is_doctor']:
            unread=sum(not n['read_at'] for n in notifications(st.session_state.user['id']))
        elif not user['is_admin']:
            # Reminders are checked at most once a minute here (each check is ~10 queries, and this runs on
            # every click); the Notifications and My plan pages still check each time they open.
            now = time.monotonic()
            if now - st.session_state.get("reminders_checked_at", -1e9) >= 60:
                refresh_patient_notifications(st.session_state.user['id'])
                st.session_state.reminders_checked_at = now
            unread=sum(not n['read_at'] for n in notifications(st.session_state.user['id']))
        previous_group = None
        for key, label, _icon, _doctor_only in visible_nav:
            group = ('Administration' if key == 'admin' else 'Care & insights' if key in ('clinical','intelligence','analytics') else
                     'My health' if key in ('wellness','food','medications') else
                     'Support & account' if key in ('notifications','assistant','settings') else None)
            if group != previous_group:
                st.html(f'<div class="sidebar-section-label">{group}</div>')
                previous_group = group
            text = label + (f" ({unread})" if key == "notifications" and unread else "")
            if st.button(text, key=f"nav_{key}", width="stretch"):
                if key == "assistant":
                    open_assistant(active)
                go_to(key)

    st.html(sidebar_dynamic_css(active))

    if st.session_state.pop("close_menu", 0):
        # After choosing a page on a phone-sized screen, fold the menu away as if the person tapped outside it
        # (Streamlit leaves it open over the page). Each request has its own id, so it runs every time.
        close_phone_menu()

    with st.container(key="sidebar_bottom"):
        st.html(
            f"""
<div class="sidebar-user-profile">
    <div class="sidebar-user-avatar">{escape(user['initials'])}</div>
    <div class="sidebar-user-info">
        <div class="sidebar-user-name">{escape(user['name'])}</div>
        <div class="sidebar-user-role">{user['role_label']}</div>
    </div>
</div>
"""
        )
        if st.button("Log Out", key="sidebar_logout", width="stretch"):
            sign_out()

# =============================================================================
# FLOATING ASSISTANT BUTTON
# =============================================================================

if active not in ("assistant", "admin"):
    with st.container(key="open_assistant"):
        if st.button("Open AHEAD Assistant", key="open_assistant_button"):
            open_assistant(active)

# =============================================================================
# PAGES
# =============================================================================

PAGES = {
    "overview": render_overview,
    "screenings": render_screenings,
    "wellness":render_wellness,
    "food":render_food,
    "medications":render_medications,
    "notifications":render_notifications,
    "analytics": render_analytics,
    "clinical": render_clinical_dashboard,
    "intelligence":render_doctor_intelligence,
    "assistant": render_assistant,
    "settings": render_settings,
    "admin": render_admin,
}

if active not in nav_keys or (active == 'admin') != user['is_admin']:
    st.error("You don't have access to this page.")
elif (active in ('clinical','intelligence') and not user['is_doctor']) or (active in patient_only and user['is_doctor']):
    st.error("Doctor access required.")
else:
    PAGES[active]()
