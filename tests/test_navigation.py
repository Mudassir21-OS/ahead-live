"""Browser back/forward between pages (ahead/navigation.py): how st.session_state.nav follows the address."""

import pytest
import streamlit as st

from ahead import navigation

KEYS = ["overview", "screenings", "medications", "settings"]


@pytest.fixture
def env(monkeypatch):
    state = type("State", (dict,), {"__getattr__": dict.get, "__setattr__": dict.__setitem__})()
    monkeypatch.setattr(st, "session_state", state)
    seen = {"url": "overview", "switched": []}
    monkeypatch.setattr(navigation, "register", lambda keys, home, titles=None: seen["url"])
    monkeypatch.setattr(navigation, "switch", lambda key: seen["switched"].append(key))
    return state, seen


def run(state, seen):
    navigation.sync(KEYS, "overview", {})
    return state["nav"], seen["switched"]


def test_back_or_forward_moves_to_the_page_in_the_address(env):
    state, seen = env
    state["nav"] = "screenings"; seen["url"] = "screenings"
    run(state, seen)
    seen["url"] = "medications"           # the browser went to /medications
    assert run(state, seen) == ("medications", [])


def test_a_reload_opens_the_page_in_the_address(env):
    state, seen = env
    state["nav"] = "overview"; seen["url"] = "settings"   # new connection, address /settings
    assert run(state, seen) == ("settings", [])


def test_a_fresh_sign_in_starts_at_home(env):
    state, seen = env
    navigation.mark_fresh_sign_in()
    state["nav"] = "overview"; seen["url"] = "medications"
    assert run(state, seen) == ("overview", ["overview"])


def test_a_page_this_person_cannot_open_goes_home(env):
    state, seen = env
    state["nav"] = "clinical"; seen["url"] = "overview"
    assert run(state, seen) == ("overview", [])


def test_the_address_follows_a_page_chosen_in_the_app(env):
    state, seen = env
    state["nav"] = "overview"; seen["url"] = "overview"
    run(state, seen)
    state["nav"] = "settings"             # e.g. set by a test or an older code path without go_to
    assert run(state, seen) == ("settings", ["settings"])


def test_back_to_another_page_closes_the_screening_and_the_phone_menu(env):
    state, seen = env
    state.update(nav="screenings", active_screening="diabetes"); seen["url"] = "screenings"
    run(state, seen)
    seen["url"] = "overview"
    run(state, seen)
    assert state["active_screening"] is None and state["close_menu"] == 1


def test_back_to_the_screenings_keeps_the_open_screening(env):
    state, seen = env
    state.update(nav="assistant", active_screening="diabetes"); seen["url"] = "assistant"
    navigation.sync(KEYS + ["assistant"], "overview", {})
    seen["url"] = "screenings"
    navigation.sync(KEYS + ["assistant"], "overview", {})
    assert state["nav"] == "screenings" and state["active_screening"] == "diabetes"


def test_forward_to_the_assistant_returns_to_the_page_you_came_from(env):
    state, seen = env
    state.update(nav="medications", assistant_return="settings"); seen["url"] = "medications"
    navigation.sync(KEYS + ["assistant"], "overview", {})
    seen["url"] = "assistant"
    navigation.sync(KEYS + ["assistant"], "overview", {})
    assert state["assistant_return"] == "medications"


def _hash_check_script():
    import streamlit as st
    from ahead import navigation
    keys = ["overview", "screenings", "medications"]
    pages = [st.Page(navigation._noop, title=k, url_path=k, default=(k == "overview")) for k in keys]
    hashes = [page._script_hash for page in pages]
    again = st.Page(navigation._noop, title="x", url_path="screenings")
    ok = all(isinstance(h, str) and h for h in hashes) and len(set(hashes)) == 3 and again._script_hash == hashes[1]
    st.write("hash-ok" if ok else "hash-broken")


def test_pages_are_told_apart_by_their_address():
    """navigation.register() matches the current page by Streamlit's page hash (a private detail): if a Streamlit
    upgrade changes it, this fails instead of the back button quietly breaking."""
    from streamlit.testing.v1 import AppTest
    at = AppTest.from_function(_hash_check_script).run()
    assert not at.exception and at.markdown[0].value == "hash-ok"
