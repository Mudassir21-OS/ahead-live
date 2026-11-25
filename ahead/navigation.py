"""Browser back/forward between AHEAD's pages.

Each page has its own address (/screenings, /medications, ...) registered with st.navigation, so Streamlit
itself records every page change in the browser history and handles the back and forward buttons. It stays on
the same connection, so the sign-in (per tab, ahead/session.py), an open screening and the assistant's
"back to" page all carry over. The home page (Overview, or Accounts & Security for admins) lives at "/".

``st.session_state.nav`` stays the one place the app reads the current page from; this module keeps it and the
address in step:

* go_to() (ahead/components.py) sets ``nav`` and switches the address with st.switch_page.
* Back/forward, or a typed address, changes the address: ``nav`` follows it (if the page is allowed).
* A reload starts a new connection: the tab signs back in (ahead/session.py) and opens the page in the address.
* A fresh sign-in always opens the home page, whatever address the sign-in form was on.

Streamlit's own page menu is hidden (position="hidden"); AHEAD draws its own sidebar.
"""

import streamlit as st

from ahead.config import NAV_ITEMS

PAGES_KEY = "_nav_pages"      # {nav key: st.Page} registered on this run, for go_to()
SEEN_KEY = "_nav_url"         # the page in the address on the previous signed-in run
FRESH_KEY = "_nav_fresh_sign_in"
ENTRY_KEY = "_nav_entry"      # the page in the address when this connection opened (first signed-out run)
ALL_KEYS = [item[0] for item in NAV_ITEMS] + ["admin"]


def _noop():  # pages are drawn by app.py, not by st.Page.run()
    pass


def register(keys, home, titles=None):
    """Register one page per key with st.navigation (call once per run). Returns the key in the address."""
    titles = titles or {}
    pages = {key: st.Page(_noop, title=titles.get(key, key.title()), url_path=key, default=(key == home))
             for key in keys}
    current = st.navigation(list(pages.values()), position="hidden")
    st.session_state[PAGES_KEY] = pages
    for key, page in pages.items():
        if page._script_hash == current._script_hash:
            return key
    return home


def register_signed_out():
    """Signed-out runs (sign-in, About, Help, the tab wait) register every page too, so the address of a page
    survives a reload or an expired sign-in instead of being reset to "/".

    The first run's page is kept: on a reload, the rerun the tab-identity component asks for can arrive with
    the home page instead of the page in the address, so the signed-in run that follows relies on this."""
    st.session_state.setdefault(ENTRY_KEY, register(ALL_KEYS, "overview"))


def sync(keys, home, titles):
    """Signed-in runs: register the pages this person may open and settle which one is shown.
    Sets ``st.session_state.nav`` and, if the address doesn't match it yet, switches the address (reruns)."""
    url_key = register(keys, home, titles)
    entry = st.session_state.pop(ENTRY_KEY, None)
    seen = st.session_state.get(SEEN_KEY)
    st.session_state[SEEN_KEY] = url_key
    if st.session_state.pop(FRESH_KEY, False):
        st.session_state.nav = home                      # just signed in: start at home
    elif seen is not None and url_key != seen:
        st.session_state.nav = url_key                   # back/forward or a typed address
        _arrived(url_key, seen)
    elif seen is None and (entry or url_key) != home:
        st.session_state.nav = entry or url_key          # a reload on a page's address
    if st.session_state.get("nav") not in keys:
        st.session_state.nav = home
    if st.session_state.nav != url_key:
        switch(st.session_state.nav)


def _arrived(key, came_from):
    """The tidy-up go_to() does (ahead/components.py), for a page reached with back/forward."""
    if key == "assistant":
        st.session_state.assistant_return = came_from    # the assistant's "← Back" returns where you came from
    elif key != "screenings":
        st.session_state.active_screening = None         # leaving the screenings: a later visit starts fresh
    st.session_state.close_menu = st.session_state.get("menu_seq", 0) + 1   # fold the phone menu away
    st.session_state.menu_seq = st.session_state.close_menu


def mark_fresh_sign_in():
    st.session_state[FRESH_KEY] = True


def switch(key):
    """Change the address to this page (a new browser-history entry) and rerun. Falls back to a plain rerun
    when the page isn't registered (e.g. the automated tests call a page directly)."""
    page = (st.session_state.get(PAGES_KEY) or {}).get(key)
    if page is None:
        st.rerun()
        return
    st.session_state[SEEN_KEY] = key
    st.switch_page(page)
