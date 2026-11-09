"""'Add reminders to my calendar': shown on My plan (when a doctor has prescribed) and in Settings."""
import streamlit as st

from ahead import calendar_feed
from ahead.accounts import site_url


def render_calendar_panel(user_id: int, key: str, compact: bool = False) -> None:
    current = calendar_feed.feed(user_id)
    if not compact:
        st.caption("Your phone or computer reminds you at each dose time, even when AHEAD is closed. "
                   "The calendar follows your doctor's changes by itself (calendar apps check every few hours).")
    if not current:
        if st.button("Make my private calendar link", key=f"{key}_make", type="primary"):
            calendar_feed.new_link(user_id)
            st.rerun()
        st.caption("Or download the reminders once instead (they won't follow later changes):")
        _download(user_id, key, calendar_feed.show_names(user_id))
        return

    site = site_url()
    webcal, https = calendar_feed.urls(current["token"], site)
    st.link_button("Subscribe on this device", webcal, type="primary", width="stretch")
    st.caption("iPhone, iPad, Mac and Outlook open it straight away. **Google Calendar:** on a computer, go to "
               "Other calendars → + → From URL and paste this link:")
    st.code(https, language=None, wrap_lines=True)
    st.caption("Keep this link private: anyone with it can see your dose times.")
    names = st.toggle("Show medicine names in my calendar", value=bool(current["show_names"]), key=f"{key}_names",
                      help="Off: events say only “AHEAD medicine reminder” (safer on a shared calendar or a lock screen).")
    if names != bool(current["show_names"]):
        calendar_feed.set_show_names(user_id, names)
        st.rerun()
    left, right = st.columns(2)
    with left:
        _download(user_id, key, names)
    with right:
        with st.popover("Make a new link", width="stretch"):
            st.write("The current link stops working at once, so calendars that use it stop updating. "
                     "Subscribe again with the new link.")
            if st.button("Make a new link", key=f"{key}_reset", type="primary"):
                calendar_feed.new_link(user_id)
                st.toast("New calendar link made. The old one no longer works.")
                st.rerun()


def _download(user_id: int, key: str, names: bool) -> None:
    st.download_button("Download .ics file", calendar_feed.build_ics(user_id, names, site_url()),
                       file_name="ahead-reminders.ics", mime="text/calendar", key=f"{key}_download", width="stretch")
